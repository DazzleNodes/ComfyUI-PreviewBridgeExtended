"""
Preview Bridge Extended - Preview Image Generation

Provides functions for generating preview images with mask overlays.
"""

import logging
import torch
from typing import Tuple, Optional
import nodes

# Use named logger so PBE_DEBUG environment variable works
logger = logging.getLogger("PreviewBridgeExtended")

from .mask_ops import is_mask_empty, resize_mask


def save_preview_images(
    images: torch.Tensor,
    input_mask: Optional[torch.Tensor],
    editor_mask: Optional[torch.Tensor],
    editor_target: str = "mask_editor",
    unique_id: str = "",
    original_mask: Optional[torch.Tensor] = None,
    prompt=None,
    extra_pnginfo=None
) -> list:
    """
    Save preview images with mask overlays to temp folder.

    Shows two distinct colors:
    - Reddish tint: input_mask layer (upstream - subtractions)
    - Orange tint: editor_mask layer (additions)

    Args:
        images: Input images [B, H, W, C]
        input_mask: Input mask tensor [B, H, W] or None
        editor_mask: Editor mask tensor [B, H, W] or None
        editor_target: Which layer is editable ("mask_editor", "input_mask", "combined")
        unique_id: Node unique ID
        original_mask: Original mask_opt (used for for_editing mode, not display)
        prompt: ComfyUI prompt data
        extra_pnginfo: Extra PNG info

    Returns:
        List of image info dicts for UI
    """
    input_empty = is_mask_empty(input_mask)
    editor_empty = is_mask_empty(editor_mask)

    if input_empty and editor_empty:
        # No masks - save plain images
        res = nodes.PreviewImage().save_images(
            images,
            filename_prefix="PreviewBridgeExt/PBE-",
            prompt=prompt,
            extra_pnginfo=extra_pnginfo
        )
        return res['ui']['images']

    # Has mask(s) - create images with colored overlays
    masked_images = apply_mask_overlays(
        images, input_mask, editor_mask, editor_target,
        original_mask=original_mask
    )

    res = nodes.PreviewImage().save_images(
        masked_images,
        filename_prefix="PreviewBridgeExt/PBE-",
        prompt=prompt,
        extra_pnginfo=extra_pnginfo
    )
    return res['ui']['images']


def apply_mask_overlays(
    images: torch.Tensor,
    input_mask: Optional[torch.Tensor],
    editor_mask: Optional[torch.Tensor],
    editor_target: str = "mask_editor",
    original_mask: Optional[torch.Tensor] = None,
    for_editing: bool = False
) -> torch.Tensor:
    """
    Apply mask overlays with distinct colors for visual feedback.

    Two-layer system (LayerCache architecture):
    - input_mask: Red tint, alpha=1 (opaque) - current input state
    - editor_mask: Orange tint, alpha=0 (transparent) - additions layer

    for_editing mode:
    When True, puts the editable content into alpha for MaskEditor.
    Non-editable layers are baked as RGB tint.
    Used by prepare-for-edit API before opening MaskEditor.

    Args:
        images: RGB images [B, H, W, 3]
        input_mask: Input mask tensor [B, H, W] or None (get_input_mask())
        editor_mask: Editor mask tensor [B, H, W] or None (additions)
        editor_target: Controls which layer goes in alpha for editing mode
        original_mask: Original mask_opt (for for_editing fallbacks)
        for_editing: If True, prepare image for MaskEditor

    Returns:
        RGBA images [B, H, W, 4] with appropriate colors and alpha
    """
    batch, height, width, channels = images.shape
    target_size = (height, width)

    # Prepare masks - resize and batch-match
    def prepare_mask(mask: Optional[torch.Tensor]) -> Optional[torch.Tensor]:
        if mask is None or is_mask_empty(mask):
            return None

        # Ensure 3D tensor
        if len(mask.shape) == 2:
            mask = mask.unsqueeze(0)

        # Resize if needed
        if mask.shape[1] != height or mask.shape[2] != width:
            mask = resize_mask(mask, target_size)

        # Match batch size
        if mask.shape[0] != batch:
            if mask.shape[0] == 1:
                mask = mask.expand(batch, -1, -1)
            else:
                mask = mask[:batch]

        return mask

    input_m = prepare_mask(input_mask)
    editor_m = prepare_mask(editor_mask)
    original_m = prepare_mask(original_mask)

    # Start with original image as RGBA
    rgba = torch.zeros((batch, height, width, 4), dtype=images.dtype, device=images.device)
    rgba[:, :, :, :3] = images.clone()

    # for_editing mode: Bake NON-editable masks as RGB, put EDITABLE mask in alpha
    # With LayerCache, masks are already properly separated - no delta calculation needed.
    if for_editing:
        # Start with fully opaque alpha
        alpha = torch.ones((batch, height, width), dtype=images.dtype, device=images.device)

        if editor_target == "mask_editor":
            # Only editor additions are editable (NOT preserved input areas)
            # Bake input mask as RED RGB (visible but not editable)
            # IMPORTANT: Use input_m (current state with subtractions), not original_m
            input_to_bake = input_m if input_m is not None else original_m
            if input_to_bake is not None:
                blend = input_to_bake * 0.5
                rgba[:, :, :, 0] = rgba[:, :, :, 0] * (1 - blend) + 1.0 * blend
                rgba[:, :, :, 1] = rgba[:, :, :, 1] * (1 - blend) + 0.2 * blend
                rgba[:, :, :, 2] = rgba[:, :, :, 2] * (1 - blend) + 0.2 * blend

            # Put editor_m (additions) in alpha - api.py already passes layer_cache.additions
            if editor_m is not None:
                alpha = 1.0 - editor_m
            # If no editor mask yet, alpha stays 1.0 (user draws new content)

        elif editor_target == "input_mask":
            # Only input mask is editable (the current state of the input layer)
            # Bake editor mask (additions) as ORANGE RGB (visible but not editable)
            if editor_m is not None:
                blend = editor_m * 0.5
                rgba[:, :, :, 0] = rgba[:, :, :, 0] * (1 - blend) + 1.0 * blend
                rgba[:, :, :, 1] = rgba[:, :, :, 1] * (1 - blend) + 0.5 * blend
                rgba[:, :, :, 2] = rgba[:, :, :, 2] * (1 - blend) + 0.0 * blend

            # Put input_m in alpha - this is the CURRENT state of the input layer
            # (the intersection computed by prepare_for_editing, or original if no edits)
            # IMPORTANT: Prioritize input_m since it contains the correct intersection
            if input_m is not None:
                alpha = 1.0 - input_m
                logger.debug(f"[PreviewBridgeExtended] input_mask for_editing: using input_m, sum={input_m.sum().item():.2f}")
            elif original_m is not None:
                # Fallback to original if no input_m provided
                alpha = 1.0 - original_m
                logger.debug(f"[PreviewBridgeExtended] input_mask for_editing: fallback to original_m")

        elif editor_target == "combined":
            # When editor_target=combined, editor_m IS the complete combined state
            # (user can add AND erase), not just additions to overlay on original.
            # Do NOT OR with original - that would restore erased areas.

            # Debug logging for combined mode
            logger.debug(f"[PreviewBridgeExtended] for_editing=combined: "
                        f"original_m={original_m is not None}, input_m={input_m is not None}, "
                        f"editor_m={editor_m is not None}")

            if editor_m is not None:
                # Use editor_m directly - it already has the complete combined state
                alpha = 1.0 - editor_m
                logger.debug(f"[PreviewBridgeExtended] Using editor_m directly: sum={editor_m.sum().item():.2f}")
            elif original_m is not None:
                # No edits yet - use original as starting point
                alpha = 1.0 - original_m
                logger.debug(f"[PreviewBridgeExtended] No editor_m, using original_m: sum={original_m.sum().item():.2f}")
            elif input_m is not None:
                alpha = 1.0 - input_m
                logger.debug(f"[PreviewBridgeExtended] No editor_m/original_m, using input_m: sum={input_m.sum().item():.2f}")
            else:
                logger.debug(f"[PreviewBridgeExtended] No masks available for combined mode")

            logger.debug(f"[PreviewBridgeExtended] Final alpha: min={alpha.min().item():.4f}, "
                        f"max={alpha.max().item():.4f}, transparent_pixels={(alpha < 1.0).sum().item()}")

        rgba[:, :, :, 3] = alpha
        return rgba

    # DISPLAY MODE: Apply RGB tinting for visual distinction
    # With LayerCache, masks are already properly separated:
    #   - input_m = current input state (upstream - subtractions) -> RED
    #   - editor_m = additions layer -> ORANGE

    # Apply input mask (reddish tint: R=1.0, G=0.2, B=0.2)
    if input_m is not None:
        blend_input = input_m * 0.5  # 50% blend in masked areas
        rgba[:, :, :, 0] = rgba[:, :, :, 0] * (1 - blend_input) + 1.0 * blend_input
        rgba[:, :, :, 1] = rgba[:, :, :, 1] * (1 - blend_input) + 0.2 * blend_input
        rgba[:, :, :, 2] = rgba[:, :, :, 2] * (1 - blend_input) + 0.2 * blend_input

    # Apply editor mask (orange tint: R=1.0, G=0.5, B=0.0)
    if editor_m is not None:
        blend_editor = editor_m * 0.5  # 50% blend in masked areas
        rgba[:, :, :, 0] = rgba[:, :, :, 0] * (1 - blend_editor) + 1.0 * blend_editor
        rgba[:, :, :, 1] = rgba[:, :, :, 1] * (1 - blend_editor) + 0.5 * blend_editor
        rgba[:, :, :, 2] = rgba[:, :, :, 2] * (1 - blend_editor) + 0.0 * blend_editor

    # Alpha: input_mask is opaque (for red display), editor_mask is transparent
    alpha = torch.ones((batch, height, width), dtype=images.dtype, device=images.device)
    if editor_m is not None:
        alpha = 1.0 - editor_m

    rgba[:, :, :, 3] = alpha

    return rgba


def generate_info(
    input_mask_valid: bool,
    restored_mask_valid: bool,
    mask_output: str,
    restore_mask: str,
    block: str,
    images_changed: bool,
    final_empty: bool,
    image_size: Tuple[int, int]
) -> str:
    """Generate informative string about mask processing."""
    width, height = image_size

    # Determine blocking status
    # restored_mask_valid here refers to editor_mask_valid from the caller
    will_block = (
        block == "always" or
        (block == "if_empty_mask" and final_empty) or
        (block == "if_empty_editor" and not restored_mask_valid)
    )
    block_status = "BLOCKING" if will_block else "passing"

    # Determine which masks are shown in preview based on mask_output
    preview_display = []
    if mask_output == "combined":
        if input_mask_valid:
            preview_display.append("input(red)")
        if restored_mask_valid:
            preview_display.append("editor(orange)")
    elif mask_output == "input_mask" and input_mask_valid:
        preview_display.append("input(red)")
    elif mask_output == "mask_editor" and restored_mask_valid:
        preview_display.append("editor(orange)")

    preview_str = " + ".join(preview_display) if preview_display else "none"

    info_lines = [
        "== Preview Bridge Extended ==",
        f"Image: {width}x{height}",
        f"Output mode: {mask_output}",
        f"Restore: {restore_mask}",
        f"Block: {block}",
        f"Images changed: {images_changed}",
        f"Input mask (red): {'valid' if input_mask_valid else 'empty/none'}",
        f"Editor mask (orange): {'valid' if restored_mask_valid else 'empty/none'}",
        f"Preview showing: {preview_str}",
        f"Output mask: {'EMPTY' if final_empty else 'has content'}",
        f"Status: {block_status}",
    ]

    return "\n".join(info_lines)
