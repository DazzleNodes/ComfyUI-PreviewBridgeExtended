"""
Preview Bridge Extended - Mask Operations

Provides tensor operations for mask manipulation including:
- Resize, combine (OR/AND), compute delta
- Empty mask detection
"""

import torch
import torch.nn.functional as F
from typing import Tuple, Optional


def is_mask_empty(mask: Optional[torch.Tensor]) -> bool:
    """
    Check if mask is None, empty, or all zeros.

    Also detects placeholder masks (1, 64, 64) from Preview nodes.

    Args:
        mask: Mask tensor to check

    Returns:
        True if mask should be considered empty
    """
    if mask is None:
        return True

    if mask.numel() == 0:
        return True

    # Check for placeholder shape from Preview nodes
    if mask.shape == (1, 64, 64) or mask.shape == (64, 64):
        if torch.all(mask == 0):
            return True

    # Check for all-zeros
    if torch.all(mask == 0):
        return True

    return False


def resize_mask(
    mask: torch.Tensor,
    target_size: Tuple[int, int]
) -> torch.Tensor:
    """
    Resize mask to target dimensions.

    Args:
        mask: Mask tensor [B, H, W] or [H, W]
        target_size: (height, width)

    Returns:
        Resized mask tensor [B, H, W]
    """
    target_height, target_width = target_size

    # Add batch dimension if needed
    if len(mask.shape) == 2:
        mask = mask.unsqueeze(0)

    # Check if resize needed
    current_height, current_width = mask.shape[1], mask.shape[2]
    if current_height == target_height and current_width == target_width:
        return mask

    # Resize using interpolate (needs 4D tensor)
    mask_4d = mask.unsqueeze(1)  # [B, 1, H, W]
    resized = F.interpolate(
        mask_4d,
        size=(target_height, target_width),
        mode="bilinear",
        align_corners=False
    )
    return resized.squeeze(1)  # [B, H, W]


def process_input_mask(
    mask: Optional[torch.Tensor],
    target_size: Tuple[int, int]
) -> Optional[torch.Tensor]:
    """
    Process and resize input mask to target size.

    Args:
        mask: Input mask tensor or None
        target_size: (height, width) to resize to

    Returns:
        Processed mask or None if input is empty/invalid
    """
    if mask is None:
        return None

    if is_mask_empty(mask):
        return None

    # Resize mask to match image dimensions
    return resize_mask(mask, target_size)


def combine_masks_or(
    mask1: Optional[torch.Tensor],
    mask2: Optional[torch.Tensor],
    target_size: Tuple[int, int]
) -> Optional[torch.Tensor]:
    """
    Combine two masks using OR operation (union).

    Args:
        mask1: First mask tensor (can be None)
        mask2: Second mask tensor (can be None)
        target_size: (height, width) to resize masks to

    Returns:
        Combined mask tensor, or None if both inputs are None/empty
    """
    masks_to_combine = []

    for mask in [mask1, mask2]:
        if mask is not None and not is_mask_empty(mask):
            # Resize to target size
            resized = resize_mask(mask, target_size)
            masks_to_combine.append(resized)

    if len(masks_to_combine) == 0:
        return None
    elif len(masks_to_combine) == 1:
        return masks_to_combine[0]
    else:
        # Stack, sum, clamp (OR operation)
        combined = torch.sum(torch.stack(masks_to_combine, dim=0), dim=0)
        combined = torch.clamp(combined, 0, 1)
        return combined


def combine_masks_and(
    mask1: Optional[torch.Tensor],
    mask2: Optional[torch.Tensor],
    target_size: Tuple[int, int]
) -> Optional[torch.Tensor]:
    """
    Combine two masks using AND operation (intersection).

    Uses torch.min() for soft mask intersection, which preserves feathering
    and antialiasing. This is the fuzzy logic AND operation.

    Args:
        mask1: First mask tensor (can be None)
        mask2: Second mask tensor (can be None)
        target_size: (height, width) to resize masks to

    Returns:
        Intersection mask tensor, or None if either input is None/empty
    """
    # AND requires both masks to have content
    if mask1 is None or is_mask_empty(mask1):
        return None
    if mask2 is None or is_mask_empty(mask2):
        return None

    # Resize both to target size
    m1 = resize_mask(mask1, target_size)
    m2 = resize_mask(mask2, target_size)

    # Match batch sizes
    if m1.shape[0] != m2.shape[0]:
        if m1.shape[0] == 1:
            m1 = m1.expand(m2.shape[0], -1, -1)
        elif m2.shape[0] == 1:
            m2 = m2.expand(m1.shape[0], -1, -1)

    # Soft intersection using min (fuzzy AND)
    intersection = torch.min(m1, m2)
    return intersection


def compute_mask_delta(
    original_mask: Optional[torch.Tensor],
    new_mask: Optional[torch.Tensor],
    target_size: Tuple[int, int]
) -> Tuple[Optional[torch.Tensor], Optional[torch.Tensor], Optional[torch.Tensor]]:
    """
    Compute delta between original mask and new mask from MaskEditor.

    This enables the two-layer visualization:
    - Preserved areas (in both): shown as red (input layer)
    - Additions (only in new): shown as orange (editor layer)
    - Subtractions (only in original): erased, no color

    Args:
        original_mask: The immutable original mask_opt [B, H, W] or None
        new_mask: The new mask from MaskEditor [B, H, W] or None
        target_size: (height, width) for resizing

    Returns:
        Tuple of (preserved, additions, subtractions) tensors, any can be None
    """
    # Handle None cases
    if original_mask is None or is_mask_empty(original_mask):
        if new_mask is None or is_mask_empty(new_mask):
            return None, None, None
        # No original, all new content is additions
        additions = resize_mask(new_mask, target_size)
        return None, additions, None

    if new_mask is None or is_mask_empty(new_mask):
        # Original exists but new is empty - all original was subtracted
        subtractions = resize_mask(original_mask, target_size)
        return None, None, subtractions

    # Both exist - compute delta
    orig = resize_mask(original_mask, target_size)
    new = resize_mask(new_mask, target_size)

    # Match batch sizes
    if orig.shape[0] != new.shape[0]:
        if orig.shape[0] == 1:
            orig = orig.expand(new.shape[0], -1, -1)
        elif new.shape[0] == 1:
            new = new.expand(orig.shape[0], -1, -1)

    # Binarize for clean delta computation (threshold at 0.5)
    orig_binary = (orig > 0.5).float()
    new_binary = (new > 0.5).float()

    # Compute deltas
    # Preserved: areas that were in original AND still in new
    preserved = orig_binary * new_binary

    # Additions: areas in new but NOT in original
    additions = new_binary * (1.0 - orig_binary)

    # Subtractions: areas in original but NOT in new (erased by user)
    subtractions = orig_binary * (1.0 - new_binary)

    # Return None for empty results
    preserved = preserved if torch.any(preserved > 0) else None
    additions = additions if torch.any(additions > 0) else None
    subtractions = subtractions if torch.any(subtractions > 0) else None

    return preserved, additions, subtractions
