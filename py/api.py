"""
Preview Bridge Extended - API Handler Functions

Provides API functions for JS-Python communication:
- generate_preview_for_api: Refresh colored preview after MaskEditor save
- prepare_for_editing: Prepare image with editable alpha for MaskEditor
"""

import base64
import logging
import numpy as np
import torch
from io import BytesIO
from PIL import Image
from typing import Optional, Dict, Any

from .utils import load_mask_from_clipspace
from .mask_ops import is_mask_empty, combine_masks_and, combine_masks_or, compute_mask_delta
from .caches import (
    get_context_cache,
    get_editor_mask_cache, set_editor_mask_cache, delete_editor_mask_cache,
    get_input_override_cache, set_input_override_cache, delete_input_override_cache,
    _preview_bridge_context_cache
)
from .preview import apply_mask_overlays


def generate_preview_for_api(
    node_id: str,
    clipspace_path: str,
    mask_output_override: str = None,
    editor_target_override: str = None
) -> Optional[Dict[str, Any]]:
    """
    Generate a colored preview image for a given node using cached context.

    Called by the API endpoint to refresh preview after MaskEditor save
    without re-running the entire workflow.

    Args:
        node_id: The unique_id of the node
        clipspace_path: Path to the clipspace file with updated editor mask
        mask_output_override: Current mask_output from JS widget (overrides cached value)
        editor_target_override: Current editor_target from JS widget (overrides cached value)

    Returns:
        Dict with 'success', 'image_path', 'image_data' (base64) or 'error'
    """
    # Get cached context for this node
    context = get_context_cache(node_id)
    if context is None:
        return {
            'success': False,
            'error': f'No cached context for node {node_id}. Run workflow first.'
        }

    images = context.get('images')
    upstream_input_mask = context.get('upstream_input_mask')
    original_input_mask = context.get('original_input_mask')  # Immutable reference for delta
    cached_input_override = context.get('input_override')
    cached_editor_mask = context.get('editor_mask')
    # Use overrides from JS if provided, otherwise fall back to cached values
    mask_output = mask_output_override if mask_output_override else context.get('mask_output', 'combined')
    editor_target = editor_target_override if editor_target_override else context.get('editor_target', 'mask_editor')

    if images is None:
        return {
            'success': False,
            'error': 'No cached images for node'
        }

    # Load the new clipspace mask from MaskEditor
    clipspace_mask = load_mask_from_clipspace(clipspace_path)

    # Get image dimensions for mask operations
    batch, height, width, channels = images.shape
    target_size = (height, width)

    logging.info(f"[PreviewBridgeExtended API] refresh-preview called: node_id={node_id}, "
                 f"mask_output={mask_output} (override={mask_output_override is not None}), "
                 f"editor_target={editor_target} (override={editor_target_override is not None})")
    logging.info(f"[PreviewBridgeExtended API] clipspace_path={clipspace_path}, "
                 f"clipspace_loaded={clipspace_mask is not None}")

    clipspace_valid = clipspace_mask is not None and not is_mask_empty(clipspace_mask)
    logging.info(f"[PreviewBridgeExtended API] clipspace_valid={clipspace_valid}")

    if clipspace_mask is not None:
        mask_sum = clipspace_mask.sum().item()
        mask_max = clipspace_mask.max().item()
        mask_min = clipspace_mask.min().item()
        logging.info(f"[PreviewBridgeExtended API] clipspace stats: sum={mask_sum:.2f}, "
                     f"min={mask_min:.4f}, max={mask_max:.4f}, shape={clipspace_mask.shape}")

    # MODE SWITCH DECOMPOSITION
    # When user changes editor_target from combined to mask_editor or input_mask,
    # we need to decompose the combined clipspace into separate layers
    cached_editor_target = context.get('editor_target')
    upstream_input_valid = upstream_input_mask is not None and not is_mask_empty(upstream_input_mask)

    if (cached_editor_target == "combined" and
        editor_target != "combined" and
        clipspace_valid and
        upstream_input_valid):

        logging.info(f"[PreviewBridgeExtended API] MODE SWITCH: combined -> {editor_target}, decomposing...")

        # The clipspace contains the combined state from previous combined mode edits
        # Decompose into: additions (new mask areas) and subtractions (erased upstream areas)
        combined_state = clipspace_mask
        _, additions, subtractions = compute_mask_delta(
            upstream_input_mask, combined_state, target_size
        )

        if editor_target == "mask_editor":
            # User now wants to edit only the orange (additions) layer
            # - Set clipspace to just the additions for the new editor_mask
            # - Preserve subtractions as input_override (what was erased from upstream)
            if additions is not None and not is_mask_empty(additions):
                clipspace_mask = additions
                logging.info(f"[PreviewBridgeExtended API] Decomposed additions: sum={additions.sum().item():.2f}")
            else:
                clipspace_mask = None
                logging.info("[PreviewBridgeExtended API] No additions after decomposition")

            # Calculate modified input (upstream minus subtractions)
            if subtractions is not None and not is_mask_empty(subtractions):
                modified_input = torch.clamp(upstream_input_mask - subtractions, 0, 1)
                set_input_override_cache(node_id, modified_input)
                logging.info(f"[PreviewBridgeExtended API] Preserved input subtractions: sum={modified_input.sum().item():.2f}")

        elif editor_target == "input_mask":
            # User now wants to edit only the red (input) layer
            # - Set clipspace to the intersection (upstream AND combined)
            # - Preserve additions as editor_mask
            modified_input = combine_masks_and(upstream_input_mask, combined_state, target_size)
            if modified_input is not None and not is_mask_empty(modified_input):
                clipspace_mask = modified_input
                logging.info(f"[PreviewBridgeExtended API] Decomposed input: sum={modified_input.sum().item():.2f}")
            else:
                clipspace_mask = upstream_input_mask
                logging.info("[PreviewBridgeExtended API] Using upstream as input (no intersection)")

            # Preserve additions in editor mask cache
            if additions is not None and not is_mask_empty(additions):
                set_editor_mask_cache(node_id, additions)
                logging.info(f"[PreviewBridgeExtended API] Preserved additions: sum={additions.sum().item():.2f}")

        # Update clipspace_valid after decomposition
        clipspace_valid = clipspace_mask is not None and not is_mask_empty(clipspace_mask)

    # Route clipspace edits based on editor_target (same logic as process method)
    editor_mask = None
    input_override = None

    if editor_target == "mask_editor":
        # MaskEditor edits go to orange layer only
        editor_mask = clipspace_mask
        input_override = cached_input_override  # Preserve existing input override
    elif editor_target == "input_mask":
        # MaskEditor edits override the input (red) layer only
        input_override = clipspace_mask
        editor_mask = cached_editor_mask  # Preserve existing editor mask
    elif editor_target == "combined":
        # MaskEditor edits affect both layers
        editor_mask = clipspace_mask
        input_override = clipspace_mask

    # Determine which masks to show based on mask_output
    # IMPORTANT: Use upstream_input_mask for red layer to preserve correct colors
    preview_input_mask = None
    preview_editor_mask = None
    upstream_valid = upstream_input_mask is not None and not is_mask_empty(upstream_input_mask)

    if mask_output == "combined":
        preview_input_mask = upstream_input_mask if upstream_valid else None
        preview_editor_mask = editor_mask
    elif mask_output == "input_mask":
        # Preview should match what OUTPUT will produce
        # Must account for editor_target to show correct modified input
        if editor_target == "combined" and upstream_valid and input_override is not None:
            # When editor_target=combined, erasures from input are tracked via intersection
            # Output will be: min(upstream, clipspace) - areas in BOTH
            preview_input_mask = combine_masks_and(upstream_input_mask, input_override, target_size)
            if preview_input_mask is None:
                preview_input_mask = upstream_input_mask
            logging.info(f"[PreviewBridgeExtended API] input_mask preview using AND intersection: "
                        f"sum={preview_input_mask.sum().item():.2f}")
        elif editor_target == "input_mask" and input_override is not None and not is_mask_empty(input_override):
            # When editor_target=input_mask, user directly edited input layer
            preview_input_mask = input_override
            logging.info(f"[PreviewBridgeExtended API] input_mask preview using input_override: "
                        f"sum={preview_input_mask.sum().item():.2f}")
        else:
            # editor_target=mask_editor means input wasn't edited, use upstream
            input_mask = upstream_input_mask if upstream_valid else None
            preview_input_mask = upstream_input_mask if upstream_valid else input_mask
            logging.info(f"[PreviewBridgeExtended API] input_mask preview using upstream: "
                        f"sum={preview_input_mask.sum().item() if preview_input_mask is not None else 0:.2f}")
    elif mask_output == "mask_editor":
        preview_editor_mask = editor_mask

    # Check if we have any masks to overlay
    input_empty = is_mask_empty(preview_input_mask)
    editor_empty = is_mask_empty(preview_editor_mask)

    if input_empty and editor_empty:
        # No masks - just return the original image as base64
        img_tensor = images[0]  # First image in batch
    else:
        # Generate colored preview with overlays
        # Pass original_input_mask for delta-based coloring (red=preserved, orange=additions)
        masked_images = apply_mask_overlays(
            images, preview_input_mask, preview_editor_mask, editor_target,
            original_mask=original_input_mask
        )
        img_tensor = masked_images[0]  # First image in batch

    # Convert tensor to PIL Image
    # Handle both RGB (3 channels) and RGBA (4 channels)
    if img_tensor.shape[-1] == 4:
        # RGBA - convert to PIL with alpha
        img_np = (img_tensor.cpu().numpy() * 255).astype(np.uint8)
        pil_img = Image.fromarray(img_np, mode='RGBA')
    else:
        # RGB - convert to PIL
        img_np = (img_tensor.cpu().numpy() * 255).astype(np.uint8)
        pil_img = Image.fromarray(img_np, mode='RGB')

    # Save to bytes buffer as PNG
    buffer = BytesIO()
    pil_img.save(buffer, format='PNG')
    buffer.seek(0)

    # Convert to base64 data URI
    img_base64 = base64.b64encode(buffer.getvalue()).decode('utf-8')
    data_uri = f"data:image/png;base64,{img_base64}"

    # Update caches based on editor_target
    clipspace_valid = clipspace_mask is not None and not is_mask_empty(clipspace_mask)

    if editor_target == "mask_editor" or editor_target == "combined":
        if clipspace_valid:
            set_editor_mask_cache(node_id, clipspace_mask)
        else:
            delete_editor_mask_cache(node_id)

    if editor_target == "input_mask" or editor_target == "combined":
        if clipspace_valid:
            set_input_override_cache(node_id, clipspace_mask)
        else:
            delete_input_override_cache(node_id)

    # CRITICAL: Also update context cache so prepare_for_editing has latest masks
    # Include editor_target so mode switch decomposition works correctly
    if node_id in _preview_bridge_context_cache:
        _preview_bridge_context_cache[node_id]['editor_mask'] = editor_mask
        _preview_bridge_context_cache[node_id]['input_override'] = input_override
        _preview_bridge_context_cache[node_id]['editor_target'] = editor_target

    logging.info(f"[PreviewBridgeExtended API] Successfully generated preview: "
                 f"has_input={not input_empty}, has_editor={not editor_empty}, "
                 f"image_size={len(img_base64)} bytes")

    return {
        'success': True,
        'image_data': data_uri,
        'has_input_mask': not input_empty,
        'has_editor_mask': not editor_empty,
        'editor_target': editor_target,
    }


def prepare_for_editing(node_id: str, editor_target_override: str = None) -> Optional[Dict[str, Any]]:
    """
    Prepare an image for MaskEditor by putting the editable mask(s) in alpha.

    Called by JS before opening MaskEditor. This converts the display image
    (red=input, orange=additions, alpha=additions only) to an editable image
    where the alpha channel contains ALL masks that should be editable based
    on editor_target.

    Args:
        node_id: The node's unique ID
        editor_target_override: Current editor_target from JS widget (overrides cached value)

    Returns:
        Dict with 'success', 'image_data' (base64 PNG), or 'error'
    """
    logging.info(f"[PreviewBridgeExtended] prepare_for_editing called for node {node_id}, override={editor_target_override}")

    # Get cached context for this node
    context = get_context_cache(node_id)
    if context is None:
        logging.warning(f"[PreviewBridgeExtended] No cached context for node {node_id}")
        return {
            'success': False,
            'error': f'No cached context for node {node_id}. Run workflow first.'
        }

    images = context.get('images')
    upstream_input_mask = context.get('upstream_input_mask')
    original_input_mask = context.get('original_input_mask')
    cached_editor_mask = context.get('editor_mask')
    cached_input_override = context.get('input_override')
    # Use override from JS if provided, otherwise fall back to cached value
    editor_target = editor_target_override if editor_target_override else context.get('editor_target', 'combined')

    logging.info(f"[PreviewBridgeExtended] Context: editor_target={editor_target}, "
                 f"has_images={images is not None}, "
                 f"has_upstream={upstream_input_mask is not None}, "
                 f"has_original={original_input_mask is not None}, "
                 f"has_editor={cached_editor_mask is not None}, "
                 f"has_input_override={cached_input_override is not None}")

    if images is None:
        return {
            'success': False,
            'error': 'No cached images for node'
        }

    # For editing, we need to put the editable content in alpha
    # Get image dimensions for mask operations
    batch, height, width, channels = images.shape
    target_size = (height, width)

    # Determine the correct input_mask based on editor_target
    if editor_target == "input_mask" and cached_input_override is not None:
        # When editing the input layer, show the CURRENT state (with any subtractions)
        # Compute intersection of upstream and input_override to get current input state
        upstream_valid = upstream_input_mask is not None and not is_mask_empty(upstream_input_mask)
        override_valid = not is_mask_empty(cached_input_override)

        if upstream_valid and override_valid:
            intersection = combine_masks_and(upstream_input_mask, cached_input_override, target_size)
            if intersection is not None and not is_mask_empty(intersection):
                input_mask = intersection
                logging.info(f"[PreviewBridgeExtended] input_mask for editing: using intersection, "
                            f"sum={intersection.sum().item():.2f}")
            else:
                input_mask = original_input_mask if original_input_mask is not None else upstream_input_mask
        else:
            input_mask = original_input_mask if original_input_mask is not None else upstream_input_mask
    else:
        # For other modes, use the original input mask (immutable) or fall back to upstream
        input_mask = original_input_mask if original_input_mask is not None else upstream_input_mask

    # Log mask info with detailed stats for debugging
    input_empty = is_mask_empty(input_mask)
    editor_empty = is_mask_empty(cached_editor_mask)
    original_empty = is_mask_empty(original_input_mask)
    logging.info(f"[PreviewBridgeExtended] Masks: input_empty={input_empty}, "
                 f"editor_empty={editor_empty}, original_empty={original_empty}")

    # Detailed mask stats for debugging editor_target=combined issue
    def log_mask_stats(name: str, mask):
        if mask is None:
            logging.info(f"[PreviewBridgeExtended] {name}: None")
        else:
            logging.info(f"[PreviewBridgeExtended] {name}: shape={mask.shape}, "
                        f"sum={mask.sum().item():.2f}, min={mask.min().item():.4f}, "
                        f"max={mask.max().item():.4f}")

    log_mask_stats("input_mask", input_mask)
    log_mask_stats("cached_editor_mask", cached_editor_mask)
    log_mask_stats("original_input_mask", original_input_mask)
    log_mask_stats("upstream_input_mask", upstream_input_mask)

    # Also log cached context keys for debugging
    cached_mask_output = context.get('mask_output', 'N/A')
    cached_editor_target = context.get('editor_target', 'N/A')
    logging.info(f"[PreviewBridgeExtended] Context cache: mask_output={cached_mask_output}, "
                 f"editor_target(cached)={cached_editor_target}, editor_target(override)={editor_target_override}")

    # RE-COMPOSITION: When switching TO combined mode from a decomposed mode,
    # we need to reconstruct the combined state from the separate layers
    editor_mask_for_display = cached_editor_mask
    if editor_target == "combined" and cached_editor_target in ("mask_editor", "input_mask"):
        # We're switching from a decomposed mode back to combined
        # Reconstruct: combined = input_layer OR additions_layer
        #
        # CRITICAL: Use cached_input_override if available - it contains the user's
        # edits to the input layer (subtractions from upstream). Without this,
        # subtractions made in input_mask mode are lost when switching to combined.
        if cached_input_override is not None and not is_mask_empty(cached_input_override):
            input_layer = cached_input_override
            logging.info(f"[PreviewBridgeExtended] RE-COMPOSITION: using cached_input_override, "
                        f"sum={cached_input_override.sum().item():.2f}")
        else:
            input_layer = input_mask if input_mask is not None else upstream_input_mask
            logging.info(f"[PreviewBridgeExtended] RE-COMPOSITION: using fallback input_mask/upstream")
        additions_layer = cached_editor_mask

        if input_layer is not None and additions_layer is not None:
            # OR combine to get full combined state
            combined_state = combine_masks_or(input_layer, additions_layer, target_size)
            if combined_state is not None:
                editor_mask_for_display = combined_state
                logging.info(f"[PreviewBridgeExtended] RE-COMPOSITION: reconstructed combined state, "
                            f"input_sum={input_layer.sum().item():.2f}, "
                            f"additions_sum={additions_layer.sum().item():.2f}, "
                            f"combined_sum={combined_state.sum().item():.2f}")
        elif input_layer is not None:
            # No additions, just use input layer
            editor_mask_for_display = input_layer
            logging.info(f"[PreviewBridgeExtended] RE-COMPOSITION: using input layer only, "
                        f"sum={input_layer.sum().item():.2f}")

    # Generate image with for_editing=True
    # This puts the appropriate masks in alpha based on editor_target
    masked_images = apply_mask_overlays(
        images, input_mask, editor_mask_for_display, editor_target,
        original_mask=original_input_mask,
        for_editing=True
    )
    img_tensor = masked_images[0]  # First image in batch

    # Log alpha channel stats to verify mask is in alpha
    alpha_channel = img_tensor[:, :, 3]
    alpha_min = alpha_channel.min().item()
    alpha_max = alpha_channel.max().item()
    alpha_mean = alpha_channel.mean().item()
    transparent_pixels = (alpha_channel < 1.0).sum().item()
    total_pixels = alpha_channel.numel()
    logging.info(f"[PreviewBridgeExtended] Alpha stats: min={alpha_min:.3f}, max={alpha_max:.3f}, "
                 f"mean={alpha_mean:.3f}, transparent_pixels={transparent_pixels}/{total_pixels}")

    # Convert tensor to PIL Image (always RGBA for editing)
    img_np = (img_tensor.cpu().numpy() * 255).astype(np.uint8)
    pil_img = Image.fromarray(img_np, mode='RGBA')

    # Save to bytes buffer as PNG
    buffer = BytesIO()
    pil_img.save(buffer, format='PNG')
    buffer.seek(0)

    # Encode as base64 data URI
    b64_data = base64.b64encode(buffer.read()).decode('utf-8')
    data_uri = f"data:image/png;base64,{b64_data}"

    logging.info(f"[PreviewBridgeExtended] Generated editable image, size={len(b64_data)} bytes")

    # CRITICAL: Update context cache with the editor_target used for this edit session
    # This prevents refresh-preview from incorrectly triggering mode switch decomposition
    # when the clipspace was already prepared for the current mode
    if node_id in _preview_bridge_context_cache:
        _preview_bridge_context_cache[node_id]['editor_target'] = editor_target
        logging.info(f"[PreviewBridgeExtended] Updated context cache editor_target to: {editor_target}")

    return {
        'success': True,
        'image_data': data_uri,
        'editor_target': editor_target,
    }
