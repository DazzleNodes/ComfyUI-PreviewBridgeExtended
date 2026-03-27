"""
Preview Bridge Extended - Shared Node Logic

Common mask orchestration functions used by both PreviewBridgeExtended (IMAGE)
and PreviewBridgeExtendedLatent (LATENT+VAE) node variants.

These functions handle the LayerCache pipeline:
- Image change detection
- LayerCache validation and restore_mask behavior
- Upstream mask processing
- Clipspace loading and decomposition
- Output mask generation
- Block decision logic
"""

import logging
import torch
from typing import Tuple, Optional, Dict, Any
from dataclasses import dataclass

# Use named logger so PBE_DEBUG environment variable works
logger = logging.getLogger("PreviewBridgeExtended")

from .utils import is_clipspace_path, load_mask_from_clipspace, register_clipspace_image
from .mask_ops import is_mask_empty, resize_mask, process_input_mask, compute_tensor_fingerprint
from .caches import (
    get_cache, set_cache,
    get_original_input_cache, set_original_input_cache, delete_original_input_cache,
    _preview_bridge_image_id_map, _preview_bridge_image_name_map
)
from .layer_cache import get_layer_cache, decompose_and_store, get_output_mask, get_preview_masks
from .preview import generate_info


# ============================================================================
# Shared widget definitions (both node variants use the same widgets)
# ============================================================================

MASK_OUTPUT_WIDGET = (
    ["combined", "mask_editor", "input_mask"],
    {
        "default": "combined",
        "tooltip": (
            "Controls what goes to the OUTPUT mask slot.\n"
            "combined: OR combine input_mask + mask_editor drawings\n"
            "mask_editor: Only output the editor mask layer\n"
            "input_mask: Only output the input mask layer\n\n"
            "Preview displays the selected output mode (WYSIWYG)."
        )
    }
)

EDITOR_TARGET_WIDGET = (
    ["combined", "mask_editor", "input_mask"],
    {
        "default": "combined",
        "tooltip": (
            "Controls WHAT is editable in MaskEditor.\n"
            "Display always shows: red=input mask, orange=editor mask.\n\n"
            "combined: Edit both input + editor masks together (default)\n"
            "mask_editor: Only edit editor mask (input mask shown as red, locked)\n"
            "input_mask: Only edit input mask (editor mask shown as orange, locked)"
        )
    }
)

RESTORE_MASK_WIDGET = (
    ["never", "always", "if_same_size"],
    {
        "default": "never",
        "tooltip": (
            "if_same_size: Restore cached mask if new image has same dimensions\n"
            "always: Always restore cached mask (resized if needed)\n"
            "never: Do not restore cached masks\n"
            "Note: restore_mask has higher priority than block"
        )
    }
)

BLOCK_WIDGET = (
    ["never", "if_empty_mask", "if_empty_editor", "always"],
    {
        "default": "never",
        "tooltip": (
            "never: Never block execution\n"
            "if_empty_mask: Block if the OUTPUT mask is empty\n"
            "if_empty_editor: Block if user hasn't drawn in MaskEditor\n"
            "always: Always block execution (debugging backstop)"
        )
    }
)

DAZZLE_SIGNAL_WIDGET = ("DAZZLE_SIGNAL", {
    "tooltip": "Orchestration signal from Dazzle Command node. Controls block behavior based on workflow state (playing/paused). Optional -- no effect without connection."
})


# ============================================================================
# Result dataclass for shared process output
# ============================================================================

@dataclass
class MaskProcessResult:
    """Result from process_masks() shared logic."""
    final_mask: torch.Tensor
    is_empty: bool
    editor_has_content: bool
    info: str
    upstream_input_mask: Optional[torch.Tensor]
    upstream_input_valid: bool
    images_changed: bool
    preview_input_mask: Optional[torch.Tensor]
    preview_editor_mask: Optional[torch.Tensor]
    layer_cache: Any  # LayerCache instance


# ============================================================================
# Shared processing functions
# ============================================================================

def detect_images_changed(images: torch.Tensor, unique_id: str) -> bool:
    """
    Detect if input images have changed from cached version.

    Uses content-based fingerprinting instead of object identity to handle
    dynamically generated images (e.g., outputs from upstream nodes) that
    have new tensor objects each execution but same content.
    """
    cached = get_cache(unique_id)
    if cached is None:
        return True

    cached_images, _ = cached
    cached_fp = compute_tensor_fingerprint(cached_images)
    current_fp = compute_tensor_fingerprint(images)

    changed = cached_fp != current_fp
    if changed:
        logger.debug(f"[PreviewBridgeExtended] Images content changed: "
                     f"old={cached_fp[:8] if cached_fp else 'None'}... "
                     f"new={current_fp[:8] if current_fp else 'None'}...")
    return changed


def validate_layer_cache(
    layer_cache,
    images: torch.Tensor,
    images_changed: bool,
    restore_mask: str,
    height: int,
    width: int
) -> None:
    """Handle LayerCache validation based on image changes and restore_mask setting."""
    if images_changed:
        if restore_mask == "always":
            layer_cache.validate_image(images, preserve_layers=True)
        elif restore_mask == "if_same_size":
            current_mask = layer_cache.get_combined()
            if current_mask is not None:
                mask_h, mask_w = current_mask.shape[-2], current_mask.shape[-1]
                sizes_match = (mask_h == height and mask_w == width)
                if sizes_match:
                    logger.debug(f"[PreviewBridgeExtended] restore_mask=if_same_size: sizes match "
                                 f"({mask_w}x{mask_h} == {width}x{height}), preserving layers")
                else:
                    logger.debug(f"[PreviewBridgeExtended] restore_mask=if_same_size: sizes differ "
                                 f"({mask_w}x{mask_h} != {width}x{height}), clearing layers")
            else:
                sizes_match = False
            layer_cache.validate_image(images, preserve_layers=sizes_match)
        else:  # never
            layer_cache.validate_image(images, preserve_layers=False)
            layer_cache.clear()
            logger.debug(f"[PreviewBridgeExtended] Image changed, LayerCache cleared (restore_mask=never)")
    elif restore_mask == "never":
        if layer_cache.additions is not None or layer_cache.subtractions is not None:
            layer_cache.additions = None
            layer_cache.subtractions = None
            logger.debug(f"[PreviewBridgeExtended] restore_mask=never, cleared user edits (additions/subtractions)")


def load_clipspace_mask(
    unique_id: str,
    images_changed: bool,
    restore_mask: str,
    target_size: Tuple[int, int],
    clipspace_path: str = ""
) -> Optional[torch.Tensor]:
    """
    Load mask from clipspace file or LayerCache fallback.

    Priority:
    1. Skip if clipspace already consumed (prevents mode-switch clobbering)
    2. Clipspace file (user's most recent MaskEditor edit)
    3. LayerCache fallback (cross-image restoration only)
    """
    target_height, target_width = target_size

    # If images haven't changed and the clipspace has already been consumed
    # by a previous process() run, skip re-loading.
    if not images_changed:
        layer_cache = get_layer_cache(unique_id)
        if layer_cache.clipspace_consumed:
            logger.debug(f"[PreviewBridgeExtended] Clipspace already consumed, skipping re-decomposition")
            return None

    # Try clipspace file
    if is_clipspace_path(clipspace_path):
        clipspace_mask = load_mask_from_clipspace(clipspace_path)
        if clipspace_mask is not None:
            if is_mask_empty(clipspace_mask):
                return None

            mask_height = clipspace_mask.shape[1] if len(clipspace_mask.shape) == 3 else clipspace_mask.shape[0]
            mask_width = clipspace_mask.shape[2] if len(clipspace_mask.shape) == 3 else clipspace_mask.shape[1]
            sizes_match = (mask_height == target_height and mask_width == target_width)

            if sizes_match:
                return clipspace_mask
            else:
                if restore_mask == "never":
                    logger.debug(f"[PreviewBridgeExtended] Clipspace size {mask_width}x{mask_height} differs from "
                                 f"image {target_width}x{target_height}, restore_mask=never - clearing")
                    return None
                elif restore_mask == "if_same_size":
                    logger.debug(f"[PreviewBridgeExtended] Clipspace size {mask_width}x{mask_height} differs from "
                                 f"image {target_width}x{target_height}, restore_mask=if_same_size - clearing")
                    return None
                elif restore_mask == "always":
                    logger.debug(f"[PreviewBridgeExtended] Clipspace size {mask_width}x{mask_height} differs from "
                                 f"image {target_width}x{target_height}, restore_mask=always - resizing")
                    return resize_mask(clipspace_mask, target_size)

    # No clipspace — check LayerCache fallback
    if restore_mask == "never":
        return None

    if not images_changed:
        return None

    if restore_mask not in ["always", "if_same_size"]:
        return None

    layer_cache = get_layer_cache(unique_id)
    mask = layer_cache.get_combined()

    if mask is not None and not is_mask_empty(mask):
        mask_height = mask.shape[-2]
        mask_width = mask.shape[-1]

        if restore_mask == "if_same_size":
            if mask_height == target_height and mask_width == target_width:
                return mask
        elif restore_mask == "always":
            if mask_height != target_height or mask_width != target_width:
                return resize_mask(mask, target_size)
            return mask

    return None


def process_masks(
    unique_id: str,
    images: torch.Tensor,
    image: str,
    mask_opt: Optional[torch.Tensor],
    mask_output: str,
    editor_target: str,
    restore_mask: str,
    block: str,
) -> MaskProcessResult:
    """
    Shared mask orchestration pipeline used by both IMAGE and LATENT variants.

    Handles: image change detection, LayerCache validation, upstream mask processing,
    clipspace loading, decomposition, output mask generation, and block decision.

    Args:
        unique_id: Node unique ID
        images: Display image tensor [B, H, W, C] (direct for IMAGE, decoded for LATENT)
        image: Clipspace path from widget
        mask_opt: Optional upstream mask (external mask_opt, or composited noise_mask+mask_opt)
        mask_output: Output mode widget value
        editor_target: Editor target widget value
        restore_mask: Restore mask widget value
        block: Block mode widget value

    Returns:
        MaskProcessResult with all computed values
    """
    batch, height, width, channels = images.shape
    target_size = (height, width)

    # Detect if images have changed
    images_changed = detect_images_changed(images, unique_id)

    # Get LayerCache for this node
    layer_cache = get_layer_cache(unique_id)

    # Handle image change - validate LayerCache
    validate_layer_cache(layer_cache, images, images_changed, restore_mask, height, width)

    # Handle clipspace registration when images haven't changed
    if not images_changed and image and image not in _preview_bridge_image_id_map:
        if is_clipspace_path(image):
            register_clipspace_image(image, unique_id)

    # Process input mask from upstream (mask_opt)
    upstream_input_mask = process_input_mask(mask_opt, target_size)
    upstream_input_valid = not is_mask_empty(upstream_input_mask)

    # Update LayerCache with upstream (handles change detection internally)
    layer_cache.on_upstream_change(upstream_input_mask)
    logger.debug(f"[PreviewBridgeExtended] After on_upstream_change: {layer_cache.debug_info()}")

    # Store original input mask for preview coloring
    if upstream_input_valid:
        set_original_input_cache(unique_id, upstream_input_mask.clone())
    else:
        delete_original_input_cache(unique_id)

    # Load clipspace mask (raw user edits from MaskEditor)
    clipspace_mask = load_clipspace_mask(
        unique_id=unique_id,
        images_changed=images_changed,
        restore_mask=restore_mask,
        target_size=target_size,
        clipspace_path=image
    )
    clipspace_mask_valid = not is_mask_empty(clipspace_mask)
    logger.debug(f"[PreviewBridgeExtended] clipspace_mask: "
                  f"{'None/empty' if not clipspace_mask_valid else f'shape={clipspace_mask.shape}, sum={clipspace_mask.sum().item():.2f}'}")

    # Decompose clipspace into canonical layers
    if clipspace_mask_valid:
        decompose_mode = layer_cache.last_editor_target or editor_target
        logger.debug(f"[PreviewBridgeExtended] Decomposing clipspace with mode='{decompose_mode}' "
                      f"(last_editor_target='{layer_cache.last_editor_target}', widget='{editor_target}')")
        layer_cache = decompose_and_store(
            node_id=unique_id,
            clipspace=clipspace_mask,
            upstream=upstream_input_mask,
            editor_target=decompose_mode,
            target_size=target_size
        )
        layer_cache.clipspace_consumed = True
        logger.debug(f"[PreviewBridgeExtended] LayerCache updated: {layer_cache.debug_info()}")
    else:
        layer_cache.last_editor_target = editor_target

    # Get output mask from LayerCache
    final_mask = get_output_mask(unique_id, mask_output)

    if final_mask is not None:
        logger.debug(f"[PreviewBridgeExtended] FINAL MASK (LayerCache): shape={final_mask.shape}, "
                      f"sum={final_mask.sum().item():.2f}")
    else:
        logger.debug(f"[PreviewBridgeExtended] FINAL MASK (LayerCache): None")

    # Create empty mask if none available
    if final_mask is None:
        final_mask = torch.zeros((1, height, width), dtype=torch.float32)

    # Ensure mask has batch dimension
    if len(final_mask.shape) == 2:
        final_mask = final_mask.unsqueeze(0)

    # Check states for blocking decision
    is_empty = is_mask_empty(final_mask)
    editor_has_content = layer_cache.additions is not None and not is_mask_empty(layer_cache.additions)

    # Generate info string
    info = generate_info(
        input_mask_valid=upstream_input_valid,
        restored_mask_valid=editor_has_content,
        mask_output=mask_output,
        restore_mask=restore_mask,
        block=block,
        images_changed=images_changed,
        final_empty=is_empty,
        image_size=(width, height)
    )

    # Get preview masks
    preview_input_mask, preview_editor_mask = get_preview_masks(unique_id, mask_output)

    return MaskProcessResult(
        final_mask=final_mask,
        is_empty=is_empty,
        editor_has_content=editor_has_content,
        info=info,
        upstream_input_mask=upstream_input_mask,
        upstream_input_valid=upstream_input_valid,
        images_changed=images_changed,
        preview_input_mask=preview_input_mask,
        preview_editor_mask=preview_editor_mask,
        layer_cache=layer_cache,
    )


def apply_dazzle_signal(
    dazzle_signal,
    block: str,
    editor_has_content: bool,
    is_empty: bool
) -> str:
    """Apply DAZZLE_SIGNAL override to block mode. Returns updated block value.

    The signal dict contains BOTH play and pause configs (cache-stable).
    The active state is read from sys._dazzle_command_state side-channel.
    """
    import sys
    if not dazzle_signal or not isinstance(dazzle_signal, dict):
        return block

    # Read active state from side-channel (written by DazzleCommand IS_CHANGED)
    cmd_state = getattr(sys, '_dazzle_command_state', None)
    state = cmd_state.get('state', 'paused') if cmd_state else 'paused'

    # Pick the right config from the signal based on active state
    if state == 'playing':
        gate_intent = dazzle_signal.get('play_gate_intent', 'open')
        gate_mode = dazzle_signal.get('play_gate_mode', 'never')
    else:
        gate_intent = dazzle_signal.get('pause_gate_intent', 'block')
        gate_mode = dazzle_signal.get('pause_gate_mode', 'auto')

    if gate_intent == 'open':
        block = gate_mode if gate_mode != 'auto' else 'never'
        logger.debug(f"Signal: gate_intent='open' -> block='{block}'")
    elif gate_intent == 'block':
        if gate_mode == 'auto':
            if not editor_has_content and not is_empty:
                block = 'if_empty_editor'
            elif is_empty:
                block = 'if_empty_mask'
            else:
                block = 'always'
            logger.debug(f"Signal: gate_intent='block', auto -> block='{block}'")
        else:
            block = gate_mode
            logger.debug(f"Signal: gate_intent='block' -> block='{block}'")

    logger.debug(f"Signal: state={state}, gate_intent={gate_intent}, block={block}")

    return block


def should_block(block: str, is_empty: bool, editor_has_content: bool) -> bool:
    """Determine if execution should be blocked."""
    return (
        block == "always" or
        (block == "if_empty_mask" and is_empty) or
        (block == "if_empty_editor" and not editor_has_content)
    )
