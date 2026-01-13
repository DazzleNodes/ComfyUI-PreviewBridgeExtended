"""
Preview Bridge Extended - Utility Functions

Provides utility functions for clipspace file handling and path detection.
"""

import os
import logging
import torch
import numpy as np
from PIL import Image, ImageOps
from typing import Optional
import folder_paths

# Use named logger so PBE_DEBUG environment variable works
logger = logging.getLogger("PreviewBridgeExtended")


def is_clipspace_path(path: str) -> bool:
    """Check if a path looks like a clipspace file path."""
    if not path:
        return False
    return "clipspace" in path.lower() or "[input]" in path


def load_mask_from_clipspace(clipspace_path: str) -> Optional[torch.Tensor]:
    """
    Load mask from a clipspace file directly from disk.

    This bypasses the preview_bridge_image_id_map lookup, needed when
    restoring a mask from clipspace but the path isn't registered yet.
    Necessary because ComfyUI v1.34+ broke the JS widget.value setter
    that used to register clipspace paths via API.

    Based on Impact Pack PR #1172.

    Args:
        clipspace_path: Path to clipspace file (may include [input] suffix)

    Returns:
        Mask tensor [1, H, W] or None if not found/invalid
    """
    # Remove [input] suffix if present
    clean_path = clipspace_path.replace(" [input]", "").replace("[input]", "")

    # Try to find the actual clipspace file
    input_dir = folder_paths.get_input_directory()
    potential_paths = [
        clean_path,
        os.path.join(input_dir, clean_path),
        os.path.join(input_dir, "clipspace", os.path.basename(clean_path)),
    ]

    actual_file = None
    for path in potential_paths:
        if os.path.isfile(path):
            actual_file = path
            break

    if actual_file is None:
        return None

    try:
        i = Image.open(actual_file)
        i = ImageOps.exif_transpose(i)

        if 'A' in i.getbands():
            # Extract alpha channel as mask
            # In ComfyUI convention: mask=1 means masked area
            # Alpha: 255=opaque, 0=transparent
            # MaskEditor: draws on alpha, low alpha = masked
            mask = np.array(i.getchannel('A')).astype(np.float32) / 255.0
            mask = 1. - torch.from_numpy(mask)  # Invert: alpha=0 -> mask=1
            return mask.unsqueeze(0)
        else:
            return None
    except Exception as e:
        logger.warning(f"[PreviewBridgeExtended] Error loading mask from clipspace: {e}")
        return None


def register_clipspace_image(clipspace_path: str, unique_id: str) -> bool:
    """
    Register a clipspace image file in the preview bridge system.

    Handles the case where ComfyUI's mask editor creates clipspace files
    that need to be integrated with the preview bridge system.

    Based on Impact Pack PR #1009.

    Args:
        clipspace_path: Path to clipspace file
        unique_id: Node unique ID

    Returns:
        True if registration successful, False otherwise
    """
    # Import here to avoid circular imports
    from .caches import _preview_bridge_image_id_map, set_previewbridge_image

    # Remove [input] suffix if present
    clean_path = clipspace_path.replace(" [input]", "").replace("[input]", "")

    # Try to find the actual clipspace file
    input_dir = folder_paths.get_input_directory()
    potential_paths = [
        clean_path,
        os.path.join(input_dir, clean_path),
        os.path.join(input_dir, "clipspace", os.path.basename(clean_path)),
        os.path.abspath(clean_path),
    ]

    actual_file = None
    for path in potential_paths:
        if os.path.isfile(path):
            actual_file = path
            break

    if not actual_file:
        return False

    # Create ui_item for the clipspace file
    ui_item = {
        'filename': os.path.basename(actual_file),
        'subfolder': 'clipspace',
        'type': 'input'
    }

    # Register using the preview bridge system
    set_previewbridge_image(unique_id, actual_file, ui_item)
    # Also register under the original clipspace path for compatibility
    _preview_bridge_image_id_map[clipspace_path] = (actual_file, ui_item)

    return True
