# DazzleNodes - Preview Bridge Extended
# Node implementations
#
# This package provides the Preview Bridge Extended node for ComfyUI.
# The implementation is split into modules for maintainability:
#   - node.py: Main node class
#   - mask_ops.py: Tensor operations
#   - preview.py: Preview image generation
#   - api.py: API endpoint handlers
#   - caches.py: Module-level caches
#   - utils.py: Utility functions

# Re-export node class and registration mappings
from .node import (
    PreviewBridgeExtended,
    NODE_CLASS_MAPPINGS,
    NODE_DISPLAY_NAME_MAPPINGS,
)

# Re-export API functions for route handlers
from .api import (
    generate_preview_for_api,
    prepare_for_editing,
)

# Re-export commonly used utilities for external access
from .utils import (
    load_mask_from_clipspace,
    register_clipspace_image,
    is_clipspace_path,
)

# Re-export mask operations
from .mask_ops import (
    is_mask_empty,
    resize_mask,
    process_input_mask,
    combine_masks_or,
    combine_masks_and,
    compute_mask_delta,
)

__all__ = [
    # Node
    'PreviewBridgeExtended',
    'NODE_CLASS_MAPPINGS',
    'NODE_DISPLAY_NAME_MAPPINGS',
    # API
    'generate_preview_for_api',
    'prepare_for_editing',
    # Utils
    'load_mask_from_clipspace',
    'register_clipspace_image',
    'is_clipspace_path',
    # Mask ops
    'is_mask_empty',
    'resize_mask',
    'process_input_mask',
    'combine_masks_or',
    'combine_masks_and',
    'compute_mask_delta',
]
