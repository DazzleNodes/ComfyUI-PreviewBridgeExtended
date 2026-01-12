"""
Preview Bridge Extended - API Handler Functions

Provides API functions for JS-Python communication:
- generate_preview_for_api: Refresh colored preview after MaskEditor save
- prepare_for_editing: Prepare image with editable alpha for MaskEditor

Uses LayerCache as the single source of truth for all layer state.
"""

import base64
import logging
import numpy as np
import torch
from io import BytesIO
from PIL import Image
from typing import Optional, Dict, Any

from .utils import load_mask_from_clipspace
from .mask_ops import is_mask_empty
from .caches import get_context_cache, _preview_bridge_context_cache
from .preview import apply_mask_overlays
from .layer_cache import get_layer_cache, decompose_and_store, get_preview_masks


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
    original_input_mask = context.get('original_input_mask')

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
                 f"mask_output={mask_output}, editor_target={editor_target}")

    clipspace_valid = clipspace_mask is not None and not is_mask_empty(clipspace_mask)

    if clipspace_mask is not None:
        logging.info(f"[PreviewBridgeExtended API] clipspace stats: sum={clipspace_mask.sum().item():.2f}, "
                     f"shape={clipspace_mask.shape}")

    # =====================================================
    # LAYERCACHE: Decompose clipspace into canonical layers
    # This is the ONLY layer logic needed
    # =====================================================
    if clipspace_valid:
        layer_cache = decompose_and_store(
            node_id=node_id,
            clipspace=clipspace_mask,
            upstream=upstream_input_mask,
            editor_target=editor_target,
            target_size=target_size
        )
        logging.info(f"[PreviewBridgeExtended API] LayerCache updated: {layer_cache.debug_info()}")

        # Update context cache with LayerCache
        if node_id in _preview_bridge_context_cache:
            _preview_bridge_context_cache[node_id]['layer_cache'] = layer_cache
            _preview_bridge_context_cache[node_id]['editor_target'] = editor_target
    else:
        # Clipspace is empty - user clicked Clear button or erased all mask content
        # Clear the appropriate layer(s) based on editor_target mode
        layer_cache = get_layer_cache(node_id)
        logging.info(f"[PreviewBridgeExtended API] Empty clipspace received, mode={editor_target}")

        if editor_target == "combined":
            # User cleared the combined view - fully subtract upstream to get empty result
            # Setting subtractions = upstream means get_input_mask() returns zeros
            if layer_cache.upstream is not None:
                layer_cache.subtractions = layer_cache.upstream.clone()
                logging.info(f"[PreviewBridgeExtended API] Cleared via full subtraction (combined mode), "
                            f"subtractions_sum={layer_cache.subtractions.sum().item():.2f}")
            else:
                layer_cache.subtractions = None
                logging.info("[PreviewBridgeExtended API] Cleared (combined mode, no upstream)")
            layer_cache.additions = None
        elif editor_target == "mask_editor":
            # User cleared the additions layer only
            layer_cache.additions = None
            logging.info("[PreviewBridgeExtended API] Cleared additions (mask_editor mode)")
        elif editor_target == "input_mask":
            # User cleared the input mask - this means full subtraction of upstream
            if layer_cache.upstream is not None:
                layer_cache.subtractions = layer_cache.upstream.clone()
                logging.info(f"[PreviewBridgeExtended API] Set full subtractions (input_mask mode), "
                            f"sum={layer_cache.subtractions.sum().item():.2f}")
            else:
                layer_cache.subtractions = None
                logging.info("[PreviewBridgeExtended API] No upstream to subtract (input_mask mode)")

        # Update context cache
        if node_id in _preview_bridge_context_cache:
            _preview_bridge_context_cache[node_id]['layer_cache'] = layer_cache
            _preview_bridge_context_cache[node_id]['editor_target'] = editor_target

    # =====================================================
    # GET PREVIEW MASKS FROM LAYERCACHE
    # Simple, unified preview selection
    # =====================================================
    preview_input_mask, preview_editor_mask = get_preview_masks(node_id, mask_output)

    # Check if we have any masks to overlay
    input_empty = is_mask_empty(preview_input_mask)
    editor_empty = is_mask_empty(preview_editor_mask)

    if input_empty and editor_empty:
        # No masks - just return the original image as base64
        img_tensor = images[0]
    else:
        # Generate colored preview with overlays
        masked_images = apply_mask_overlays(
            images, preview_input_mask, preview_editor_mask, editor_target,
            original_mask=original_input_mask
        )
        img_tensor = masked_images[0]

    # Convert tensor to PIL Image
    if img_tensor.shape[-1] == 4:
        img_np = (img_tensor.cpu().numpy() * 255).astype(np.uint8)
        pil_img = Image.fromarray(img_np, mode='RGBA')
    else:
        img_np = (img_tensor.cpu().numpy() * 255).astype(np.uint8)
        pil_img = Image.fromarray(img_np, mode='RGB')

    # Save to bytes buffer as PNG
    buffer = BytesIO()
    pil_img.save(buffer, format='PNG')
    buffer.seek(0)

    # Convert to base64 data URI
    img_base64 = base64.b64encode(buffer.getvalue()).decode('utf-8')
    data_uri = f"data:image/png;base64,{img_base64}"

    logging.info(f"[PreviewBridgeExtended API] Successfully generated preview: "
                 f"has_input={not input_empty}, has_editor={not editor_empty}")

    return {
        'success': True,
        'image_data': data_uri,
        'has_input_mask': not input_empty,
        'has_editor_mask': not editor_empty,
        'editor_target': editor_target,
    }


def get_preview_for_api(
    node_id: str,
    mask_output_override: str = None,
    editor_target_override: str = None
) -> Optional[Dict[str, Any]]:
    """
    Get current preview from LayerCache state without clipspace decomposition.

    Used by Cancel handler to restore correct preview after MaskEditor closes
    without saving. Unlike generate_preview_for_api, this doesn't decompose
    a new clipspace - it just renders the existing LayerCache state.

    Args:
        node_id: The unique_id of the node
        mask_output_override: Current mask_output from JS widget
        editor_target_override: Current editor_target from JS widget

    Returns:
        Dict with 'success', 'image_data' (base64) or 'error'
    """
    # Get cached context for this node
    context = get_context_cache(node_id)
    if context is None:
        return {
            'success': False,
            'error': f'No cached context for node {node_id}. Run workflow first.'
        }

    images = context.get('images')
    original_input_mask = context.get('original_input_mask')

    # Use overrides from JS if provided, otherwise fall back to cached values
    mask_output = mask_output_override if mask_output_override else context.get('mask_output', 'combined')
    editor_target = editor_target_override if editor_target_override else context.get('editor_target', 'mask_editor')

    if images is None:
        return {
            'success': False,
            'error': 'No cached images for node'
        }

    logging.info(f"[PreviewBridgeExtended API] get-preview called: node_id={node_id}, "
                 f"mask_output={mask_output}, editor_target={editor_target}")

    # Get preview masks from existing LayerCache state (no decomposition)
    preview_input_mask, preview_editor_mask = get_preview_masks(node_id, mask_output)

    # Check if we have any masks to overlay
    input_empty = is_mask_empty(preview_input_mask)
    editor_empty = is_mask_empty(preview_editor_mask)

    if input_empty and editor_empty:
        # No masks - just return the original image as base64
        img_tensor = images[0]
    else:
        # Generate colored preview with overlays
        masked_images = apply_mask_overlays(
            images, preview_input_mask, preview_editor_mask, editor_target,
            original_mask=original_input_mask
        )
        img_tensor = masked_images[0]

    # Convert tensor to PIL Image
    if img_tensor.shape[-1] == 4:
        img_np = (img_tensor.cpu().numpy() * 255).astype(np.uint8)
        pil_img = Image.fromarray(img_np, mode='RGBA')
    else:
        img_np = (img_tensor.cpu().numpy() * 255).astype(np.uint8)
        pil_img = Image.fromarray(img_np, mode='RGB')

    # Save to bytes buffer as PNG
    buffer = BytesIO()
    pil_img.save(buffer, format='PNG')
    buffer.seek(0)

    # Convert to base64 data URI
    img_base64 = base64.b64encode(buffer.getvalue()).decode('utf-8')
    data_uri = f"data:image/png;base64,{img_base64}"

    logging.info(f"[PreviewBridgeExtended API] get-preview success: "
                 f"has_input={not input_empty}, has_editor={not editor_empty}")

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

    Called by JS before opening MaskEditor. Uses LayerCache to determine
    which mask to put in the alpha channel for editing.

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
    original_input_mask = context.get('original_input_mask')
    cached_editor_target = context.get('editor_target', 'combined')

    # Use override from JS if provided, otherwise fall back to cached value
    editor_target = editor_target_override if editor_target_override else cached_editor_target

    if images is None:
        return {
            'success': False,
            'error': 'No cached images for node'
        }

    batch, height, width, channels = images.shape

    # Get LayerCache for this node
    layer_cache = get_layer_cache(node_id)

    logging.info(f"[PreviewBridgeExtended] LayerCache state: {layer_cache.debug_info()}")
    logging.info(f"[PreviewBridgeExtended] Mode: cached={cached_editor_target}, target={editor_target}")

    # =====================================================
    # DETERMINE WHAT TO PUT IN ALPHA FOR EDITING
    # Based on editor_target, different content goes into alpha
    # =====================================================
    if editor_target == "combined":
        # For combined editing, put the full combined state in alpha
        editor_mask_for_display = layer_cache.get_combined()
        input_mask = layer_cache.get_input_mask()  # Show current input state (with subtractions)
        logging.info(f"[PreviewBridgeExtended] combined mode: using get_combined(), "
                    f"sum={editor_mask_for_display.sum().item() if editor_mask_for_display is not None else 0:.2f}")

        # CRITICAL FIX: If combined mask is empty (fully cleared), prevent fallback to original_m
        if is_mask_empty(editor_mask_for_display):
            original_input_mask = None
            logging.info("[PreviewBridgeExtended] combined empty, preventing original_m fallback")

    elif editor_target == "mask_editor":
        # For mask_editor, put only additions in alpha
        editor_mask_for_display = layer_cache.additions
        input_mask = layer_cache.get_input_mask()  # Show current input state (with subtractions)
        logging.info(f"[PreviewBridgeExtended] mask_editor mode: using additions, "
                    f"sum={editor_mask_for_display.sum().item() if editor_mask_for_display is not None else 0:.2f}")

    elif editor_target == "input_mask":
        # For input_mask, put the modified input (upstream - subtractions) in alpha
        # IMPORTANT: input_mask must be get_input_mask() so apply_mask_overlays puts
        # the correct mask (with subtractions) into the alpha channel
        editor_mask_for_display = layer_cache.get_input_mask()
        input_mask = layer_cache.get_input_mask()  # Same - this goes into alpha
        logging.info(f"[PreviewBridgeExtended] input_mask mode: using get_input_mask(), "
                    f"sum={editor_mask_for_display.sum().item() if editor_mask_for_display is not None else 0:.2f}")

        # CRITICAL FIX: If input mask is empty (fully subtracted), prevent fallback to original_m
        # This fixes the "mask reappearing on third open" bug. An empty result from get_input_mask()
        # means "user explicitly cleared everything" not "no edits yet".
        if is_mask_empty(editor_mask_for_display):
            original_input_mask = None
            logging.info("[PreviewBridgeExtended] input_mask empty, preventing original_m fallback")

    else:
        editor_mask_for_display = None
        input_mask = layer_cache.get_input_mask()  # Show current input state (with subtractions)

    # Generate image with for_editing=True
    # This puts the appropriate masks in alpha based on editor_target
    masked_images = apply_mask_overlays(
        images, input_mask, editor_mask_for_display, editor_target,
        original_mask=original_input_mask,
        for_editing=True
    )
    img_tensor = masked_images[0]

    # Log alpha channel stats to verify mask is in alpha
    alpha_channel = img_tensor[:, :, 3]
    alpha_min = alpha_channel.min().item()
    alpha_max = alpha_channel.max().item()
    transparent_pixels = (alpha_channel < 1.0).sum().item()
    total_pixels = alpha_channel.numel()
    logging.info(f"[PreviewBridgeExtended] Alpha stats: min={alpha_min:.3f}, max={alpha_max:.3f}, "
                 f"transparent_pixels={transparent_pixels}/{total_pixels}")

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

    # Update context cache with the editor_target used for this edit session
    if node_id in _preview_bridge_context_cache:
        _preview_bridge_context_cache[node_id]['editor_target'] = editor_target

    return {
        'success': True,
        'image_data': data_uri,
        'editor_target': editor_target,
    }
