"""
Preview Bridge Extended - Module-Level Caches

Provides all module-level caches for persistence across executions.
These will be replaced by LayerCache in Phase 1.
"""

import time
from typing import Dict, Any
import torch


# Module-level caches for persistence across executions
# We maintain our own caches separate from Impact Pack to keep packages independent
# (underscore prefix to avoid conflicts if both packages installed)
_preview_bridge_cache: Dict[str, tuple] = {}  # unique_id -> (images_tensor, ui_images)
_preview_bridge_last_mask_cache: Dict[str, torch.Tensor] = {}  # unique_id -> combined final mask
_preview_bridge_editor_mask_cache: Dict[str, torch.Tensor] = {}  # unique_id -> editor-only mask (clipspace, orange layer)
_preview_bridge_input_override_cache: Dict[str, torch.Tensor] = {}  # unique_id -> input override mask (red layer edits)
_preview_bridge_context_cache: Dict[str, Dict[str, Any]] = {}  # unique_id -> {images, input_mask, mask_output, editor_target} for API refresh

# Layer delta system caches - for proper two-layer mask visualization
# The original input mask is stored IMMUTABLY and used as reference for delta computation
_preview_bridge_original_input_cache: Dict[str, torch.Tensor] = {}  # unique_id -> original mask_opt (immutable reference)
_preview_bridge_additions_cache: Dict[str, torch.Tensor] = {}  # unique_id -> user additions (areas user drew that weren't in original)
_preview_bridge_subtractions_cache: Dict[str, torch.Tensor] = {}  # unique_id -> user subtractions (areas erased from original)

# Latent decode cache — stores decoded IMAGE + latent fingerprint to avoid re-decoding
_latent_decode_cache: Dict[str, Dict[str, Any]] = {}  # unique_id -> {fingerprint, decoded_images}

# Preview bridge registration system for clipspace integration
_pb_id_cnt = time.time()  # Counter for generating unique preview bridge IDs
_preview_bridge_image_id_map: Dict[str, tuple] = {}  # pb_id/path -> (file_path, ui_item)
_preview_bridge_image_name_map: Dict[tuple, tuple] = {}  # (unique_id, path) -> (pb_id, ui_item)


def set_previewbridge_image(unique_id: str, file_path: str, ui_item: dict) -> str:
    """
    Register an image in the preview bridge system.

    Args:
        unique_id: Node unique ID
        file_path: Path to the saved image file
        ui_item: UI item dict with filename, subfolder, type

    Returns:
        Generated preview bridge ID
    """
    global _pb_id_cnt, _preview_bridge_image_id_map, _preview_bridge_image_name_map

    _pb_id_cnt += 1
    pb_id = f"pbe_{_pb_id_cnt}"

    _preview_bridge_image_id_map[pb_id] = (file_path, ui_item)
    _preview_bridge_image_name_map[(unique_id, file_path)] = (pb_id, ui_item)

    return pb_id


def get_cache(unique_id: str) -> tuple:
    """Get cached (images_tensor, ui_images) for a node."""
    return _preview_bridge_cache.get(unique_id)


def set_cache(unique_id: str, images: torch.Tensor, ui_images: list):
    """Set cached (images_tensor, ui_images) for a node."""
    _preview_bridge_cache[unique_id] = (images, ui_images)


def get_last_mask_cache(unique_id: str) -> torch.Tensor:
    """Get cached final mask for a node."""
    return _preview_bridge_last_mask_cache.get(unique_id)


def set_last_mask_cache(unique_id: str, mask: torch.Tensor):
    """Set cached final mask for a node."""
    _preview_bridge_last_mask_cache[unique_id] = mask


def delete_last_mask_cache(unique_id: str):
    """Delete cached final mask for a node."""
    if unique_id in _preview_bridge_last_mask_cache:
        del _preview_bridge_last_mask_cache[unique_id]


def get_editor_mask_cache(unique_id: str) -> torch.Tensor:
    """Get cached editor mask for a node."""
    return _preview_bridge_editor_mask_cache.get(unique_id)


def set_editor_mask_cache(unique_id: str, mask: torch.Tensor):
    """Set cached editor mask for a node."""
    _preview_bridge_editor_mask_cache[unique_id] = mask


def delete_editor_mask_cache(unique_id: str):
    """Delete cached editor mask for a node."""
    if unique_id in _preview_bridge_editor_mask_cache:
        del _preview_bridge_editor_mask_cache[unique_id]


def get_input_override_cache(unique_id: str) -> torch.Tensor:
    """Get cached input override mask for a node."""
    return _preview_bridge_input_override_cache.get(unique_id)


def set_input_override_cache(unique_id: str, mask: torch.Tensor):
    """Set cached input override mask for a node."""
    _preview_bridge_input_override_cache[unique_id] = mask


def delete_input_override_cache(unique_id: str):
    """Delete cached input override mask for a node."""
    if unique_id in _preview_bridge_input_override_cache:
        del _preview_bridge_input_override_cache[unique_id]


def get_context_cache(unique_id: str) -> Dict[str, Any]:
    """Get cached context for a node."""
    return _preview_bridge_context_cache.get(unique_id)


def set_context_cache(unique_id: str, context: Dict[str, Any]):
    """Set cached context for a node."""
    _preview_bridge_context_cache[unique_id] = context


def get_original_input_cache(unique_id: str) -> torch.Tensor:
    """Get cached original input mask for a node."""
    return _preview_bridge_original_input_cache.get(unique_id)


def set_original_input_cache(unique_id: str, mask: torch.Tensor):
    """Set cached original input mask for a node."""
    _preview_bridge_original_input_cache[unique_id] = mask


def delete_original_input_cache(unique_id: str):
    """Delete cached original input mask for a node."""
    if unique_id in _preview_bridge_original_input_cache:
        del _preview_bridge_original_input_cache[unique_id]


def get_additions_cache(unique_id: str) -> torch.Tensor:
    """Get cached additions for a node."""
    return _preview_bridge_additions_cache.get(unique_id)


def set_additions_cache(unique_id: str, mask: torch.Tensor):
    """Set cached additions for a node."""
    _preview_bridge_additions_cache[unique_id] = mask


def delete_additions_cache(unique_id: str):
    """Delete cached additions for a node."""
    if unique_id in _preview_bridge_additions_cache:
        del _preview_bridge_additions_cache[unique_id]


def get_subtractions_cache(unique_id: str) -> torch.Tensor:
    """Get cached subtractions for a node."""
    return _preview_bridge_subtractions_cache.get(unique_id)


def set_subtractions_cache(unique_id: str, mask: torch.Tensor):
    """Set cached subtractions for a node."""
    _preview_bridge_subtractions_cache[unique_id] = mask


def delete_subtractions_cache(unique_id: str):
    """Delete cached subtractions for a node."""
    if unique_id in _preview_bridge_subtractions_cache:
        del _preview_bridge_subtractions_cache[unique_id]


def clear_delta_caches(unique_id: str):
    """Clear all delta caches for a node."""
    delete_additions_cache(unique_id)
    delete_subtractions_cache(unique_id)


def clear_all_caches(unique_id: str):
    """Clear all caches for a node (except context cache)."""
    delete_last_mask_cache(unique_id)
    delete_editor_mask_cache(unique_id)
    delete_input_override_cache(unique_id)
    clear_delta_caches(unique_id)


def get_latent_decode_cache(unique_id: str) -> Dict[str, Any]:
    """Get cached decoded image for a latent node."""
    return _latent_decode_cache.get(unique_id)


def set_latent_decode_cache(unique_id: str, fingerprint: str, decoded_images: torch.Tensor):
    """Set cached decoded image for a latent node."""
    _latent_decode_cache[unique_id] = {
        'fingerprint': fingerprint,
        'decoded_images': decoded_images,
    }
