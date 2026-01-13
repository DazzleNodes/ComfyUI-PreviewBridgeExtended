"""
Preview Bridge Extended - LayerCache Architecture

Provides unified layer storage for the two-layer mask system.

The LayerCache stores masks in canonical form:
- upstream: Immutable mask_opt from node input
- additions: Areas user drew beyond upstream (orange layer)
- subtractions: Areas removed from upstream (erased from red layer)

This enables consistent output regardless of which mode created the edits:
- combined = max(upstream - subtractions, additions)  # "additions win"
- input_mask = upstream - subtractions
- mask_editor = additions

Architecture validated through Gemini 2.5 Pro consultation (2026-01-09).
"""

import logging
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple
import torch

# Use named logger so PBE_DEBUG environment variable works
logger = logging.getLogger("PreviewBridgeExtended")

from .mask_ops import is_mask_empty, resize_mask, compute_tensor_fingerprint


@dataclass
class LayerCache:
    """
    Canonical layer storage for a node's mask state.

    Layers:
    - upstream: The immutable mask_opt from node input
    - additions: Areas user drew that weren't in upstream (orange layer)
    - subtractions: Areas user erased from upstream (removed from red layer)

    The "additions win" formula ensures user's direct creative input
    is never unexpectedly hidden by subtractions from a different context.
    """

    # Immutable reference from mask_opt input
    upstream: Optional[torch.Tensor] = None

    # Decomposed layers (computed at save time)
    additions: Optional[torch.Tensor] = None
    subtractions: Optional[torch.Tensor] = None

    # Metadata for invalidation and debugging
    image_fingerprint: Optional[str] = None  # Content-based fingerprint for image change detection
    last_editor_target: Optional[str] = None  # Mode when last saved
    upstream_hash: Optional[str] = None  # Content-based fingerprint for upstream change detection

    def get_combined(self) -> Optional[torch.Tensor]:
        """
        Reconstruct combined mask with clear layer hierarchy ("additions win").

        Layer hierarchy:
        - Layer 0 (Base): upstream
        - Layer 1 (Modifier): subtractions apply only to upstream
        - Layer 2 (Top): additions always visible, unaffected by subtractions

        Returns:
            Combined mask tensor, or None if no layers exist
        """
        if self.upstream is None and self.additions is None:
            return None

        if self.upstream is None:
            # No upstream - additions are the entire mask
            return self.additions

        # Apply subtractions to upstream only
        if self.subtractions is not None and not is_mask_empty(self.subtractions):
            base_with_subtractions = torch.clamp(self.upstream - self.subtractions, 0, 1)
        else:
            base_with_subtractions = self.upstream.clone()

        # Additions layer on top, guaranteed visible (never hidden by subtractions)
        if self.additions is not None and not is_mask_empty(self.additions):
            return torch.max(base_with_subtractions, self.additions)

        return base_with_subtractions

    def get_input_mask(self) -> Optional[torch.Tensor]:
        """
        Get input layer (upstream minus subtractions).

        This represents the current state of the red/input layer,
        with any user subtractions applied.

        Returns:
            Input mask tensor, or None if no upstream
        """
        if self.upstream is None:
            return None

        if self.subtractions is None or is_mask_empty(self.subtractions):
            return self.upstream.clone()

        return torch.clamp(self.upstream - self.subtractions, 0, 1)

    def get_editor_mask(self) -> Optional[torch.Tensor]:
        """
        Get editor layer (just additions).

        This represents user-drawn areas that weren't in the original upstream.

        Returns:
            Additions tensor, or None if no additions
        """
        return self.additions

    def validate_image(self, images: torch.Tensor, preserve_layers: bool = False) -> bool:
        """
        Check if cache is valid for current image. Invalidate if image changed.

        Uses content-based fingerprinting to detect actual content changes,
        not just memory address changes (which happen every ComfyUI execution
        for dynamically generated images like outputs from upstream nodes).

        Args:
            images: Current input images tensor
            preserve_layers: If True, keep additions/subtractions for cross-image restoration.
                           Used when restore_mask="always" or "if_same_size" to allow
                           the restoration fallback path to access cached layers.

        Returns:
            True if cache is valid, False if cache was invalidated
        """
        current_fingerprint = compute_tensor_fingerprint(images)

        if self.image_fingerprint is not None and self.image_fingerprint != current_fingerprint:
            # Image CONTENT actually changed
            if preserve_layers:
                logger.debug(f"[LayerCache] Image CONTENT changed "
                             f"(old={self.image_fingerprint[:8]}... new={current_fingerprint[:8]}...), "
                             f"preserving layers for restoration")
            else:
                logger.debug(f"[LayerCache] Image CONTENT changed "
                             f"(old={self.image_fingerprint[:8]}... new={current_fingerprint[:8]}...), "
                             f"invalidating all layers")
                self.upstream = None
                self.additions = None
                self.subtractions = None
                self.upstream_hash = None
            self.image_fingerprint = current_fingerprint
            return False

        if self.image_fingerprint is None:
            logger.debug(f"[LayerCache] Image fingerprint initialized: {current_fingerprint[:8]}...")

        self.image_fingerprint = current_fingerprint
        return True

    def on_upstream_change(self, new_upstream: Optional[torch.Tensor]) -> None:
        """
        Handle upstream mask changes.

        Behavior (validated by Gemini consultation):
        - additions are preserved (absolute areas user painted intentionally)
        - subtractions are reset (they were relative to old upstream)

        Uses content-based fingerprinting to detect actual content changes,
        not just memory address changes (which happen every ComfyUI execution).

        Args:
            new_upstream: New upstream mask tensor
        """
        old_fingerprint = self.upstream_hash
        new_fingerprint = compute_tensor_fingerprint(new_upstream)

        # Log fingerprint comparison for debugging
        if new_upstream is not None:
            logger.debug(f"[LayerCache] Upstream fingerprint check: "
                         f"old={old_fingerprint[:8] if old_fingerprint else 'None'}... "
                         f"new={new_fingerprint[:8] if new_fingerprint else 'None'}... "
                         f"sum={new_upstream.sum().item():.2f}")

        if old_fingerprint != new_fingerprint:
            # Content actually changed - reset subtractions
            if old_fingerprint is not None:
                # Only log "changed" if we had a previous value (not first run)
                logger.debug(f"[LayerCache] Upstream CONTENT changed "
                             f"(old={old_fingerprint[:8]}... new={new_fingerprint[:8] if new_fingerprint else 'None'}...), "
                             f"preserving additions, resetting subtractions")
            else:
                logger.debug(f"[LayerCache] Upstream initialized "
                             f"(fingerprint={new_fingerprint[:8] if new_fingerprint else 'None'}...)")

            self.upstream = new_upstream
            self.upstream_hash = new_fingerprint
            # Additions are absolute - user painted these areas intentionally
            # Subtractions are relative to old upstream - now invalid
            self.subtractions = None
            # self.additions preserved
        else:
            # Same content, just update reference (no subtractions reset!)
            logger.debug(f"[LayerCache] Upstream content unchanged "
                         f"(fingerprint={new_fingerprint[:8] if new_fingerprint else 'None'}...), "
                         f"preserving subtractions")
            self.upstream = new_upstream

    def clear(self) -> None:
        """Clear all layers and metadata."""
        self.upstream = None
        self.additions = None
        self.subtractions = None
        self.image_fingerprint = None
        self.last_editor_target = None
        self.upstream_hash = None

    def has_content(self) -> bool:
        """Check if any layer has content."""
        return (
            (self.upstream is not None and not is_mask_empty(self.upstream)) or
            (self.additions is not None and not is_mask_empty(self.additions))
        )

    def debug_info(self) -> str:
        """Get debug string with layer statistics."""
        parts = []

        if self.upstream is not None:
            parts.append(f"upstream={self.upstream.sum().item():.2f}")
        else:
            parts.append("upstream=None")

        if self.additions is not None:
            parts.append(f"additions={self.additions.sum().item():.2f}")
        else:
            parts.append("additions=None")

        if self.subtractions is not None:
            parts.append(f"subtractions={self.subtractions.sum().item():.2f}")
        else:
            parts.append("subtractions=None")

        parts.append(f"last_mode={self.last_editor_target}")

        return ", ".join(parts)


# Module-level storage for LayerCache instances
_layer_cache: Dict[str, LayerCache] = {}


def get_layer_cache(node_id: str) -> LayerCache:
    """
    Get or create LayerCache for a node.

    Args:
        node_id: The node's unique ID

    Returns:
        LayerCache instance for this node
    """
    if node_id not in _layer_cache:
        _layer_cache[node_id] = LayerCache()
    return _layer_cache[node_id]


def delete_layer_cache(node_id: str) -> None:
    """Delete LayerCache for a node."""
    if node_id in _layer_cache:
        del _layer_cache[node_id]


def decompose_and_store(
    node_id: str,
    clipspace: torch.Tensor,
    upstream: Optional[torch.Tensor],
    editor_target: str,
    target_size: Optional[Tuple[int, int]] = None
) -> LayerCache:
    """
    Decompose clipspace into canonical layers based on editor_target.

    This is called after MaskEditor saves. The clipspace contains different
    data depending on which mode was active:
    - combined: clipspace = upstream + additions - subtractions
    - mask_editor: clipspace = additions only
    - input_mask: clipspace = upstream - subtractions

    We decompose into canonical layers that can reconstruct any output.

    Args:
        node_id: The node's unique ID
        clipspace: Mask tensor from MaskEditor save
        upstream: The upstream mask_opt (may be None)
        editor_target: Which mode was active ("combined", "mask_editor", "input_mask")
        target_size: Optional (height, width) for resizing

    Returns:
        Updated LayerCache
    """
    cache = get_layer_cache(node_id)

    # Ensure tensors are same size
    if target_size is not None:
        if clipspace is not None and (clipspace.shape[-2] != target_size[0] or clipspace.shape[-1] != target_size[1]):
            clipspace = resize_mask(clipspace, target_size)
        if upstream is not None and (upstream.shape[-2] != target_size[0] or upstream.shape[-1] != target_size[1]):
            upstream = resize_mask(upstream, target_size)

    # Update upstream (may trigger subtractions reset via on_upstream_change)
    cache.on_upstream_change(upstream)
    cache.last_editor_target = editor_target

    logger.debug(f"[LayerCache] decompose_and_store: mode={editor_target}, "
                 f"clipspace_sum={clipspace.sum().item():.2f}, "
                 f"upstream_sum={upstream.sum().item() if upstream is not None else 0:.2f}")

    if editor_target == "combined":
        # Clipspace is the full combined state
        # Decompose into additions and subtractions relative to upstream
        if upstream is not None and not is_mask_empty(upstream):
            # additions = areas in clipspace but not in upstream
            cache.additions = torch.clamp(clipspace - upstream, 0, 1)
            # subtractions = areas in upstream but not in clipspace
            cache.subtractions = torch.clamp(upstream - clipspace, 0, 1)

            # Clean up empty layers
            if is_mask_empty(cache.additions):
                cache.additions = None
            if is_mask_empty(cache.subtractions):
                cache.subtractions = None

            logger.debug(f"[LayerCache] Decomposed combined: "
                         f"additions={cache.additions.sum().item() if cache.additions is not None else 0:.2f}, "
                         f"subtractions={cache.subtractions.sum().item() if cache.subtractions is not None else 0:.2f}")
        else:
            # No upstream - all of clipspace is additions
            cache.additions = clipspace
            cache.subtractions = None
            logger.debug(f"[LayerCache] No upstream, all additions: sum={clipspace.sum().item():.2f}")

    elif editor_target == "mask_editor":
        # Clipspace contains only additions (user was editing orange layer)
        cache.additions = clipspace
        # Preserve existing subtractions (user was editing different layer)
        logger.debug(f"[LayerCache] mask_editor mode: additions={clipspace.sum().item():.2f}, "
                     f"subtractions preserved={cache.subtractions.sum().item() if cache.subtractions is not None else 0:.2f}")

    elif editor_target == "input_mask":
        # Clipspace contains the current input state (upstream - subtractions)
        if upstream is not None and not is_mask_empty(upstream):
            # subtractions = what was removed from upstream
            cache.subtractions = torch.clamp(upstream - clipspace, 0, 1)
            if is_mask_empty(cache.subtractions):
                cache.subtractions = None
            logger.debug(f"[LayerCache] input_mask mode: subtractions={cache.subtractions.sum().item() if cache.subtractions is not None else 0:.2f}, "
                         f"additions preserved={cache.additions.sum().item() if cache.additions is not None else 0:.2f}")
        else:
            cache.subtractions = None
        # Preserve existing additions (user was editing different layer)

    logger.debug(f"[LayerCache] Final state: {cache.debug_info()}")

    return cache


def get_output_mask(node_id: str, mask_output: str) -> Optional[torch.Tensor]:
    """
    Get the appropriate output mask based on mask_output setting.

    This is the simplified output logic - editor_target doesn't matter
    at output time because we've already decomposed into canonical layers.

    Args:
        node_id: The node's unique ID
        mask_output: Which output mode ("combined", "mask_editor", "input_mask")

    Returns:
        Appropriate mask tensor, or None if no content
    """
    cache = get_layer_cache(node_id)

    if mask_output == "combined":
        return cache.get_combined()
    elif mask_output == "mask_editor":
        return cache.get_editor_mask()
    elif mask_output == "input_mask":
        return cache.get_input_mask()

    return None


def get_preview_masks(node_id: str, mask_output: str) -> Tuple[Optional[torch.Tensor], Optional[torch.Tensor]]:
    """
    Get (input_mask, editor_mask) for preview display.

    Returns the appropriate masks to display based on mask_output:
    - combined: Show upstream (red) + additions (orange)
    - input_mask: Show input layer only (red)
    - mask_editor: Show additions only (orange)

    Args:
        node_id: The node's unique ID
        mask_output: Which output mode ("combined", "mask_editor", "input_mask")

    Returns:
        Tuple of (input_mask, editor_mask) for preview overlay
    """
    cache = get_layer_cache(node_id)

    if mask_output == "combined":
        # Show both layers: input (with subtractions) as red, additions as orange
        # IMPORTANT: Use get_input_mask() not upstream, so subtractions are visible!
        return (cache.get_input_mask(), cache.additions)
    elif mask_output == "input_mask":
        # Show only input layer (with subtractions applied)
        return (cache.get_input_mask(), None)
    elif mask_output == "mask_editor":
        # Show only additions layer
        return (None, cache.additions)

    return (None, None)
