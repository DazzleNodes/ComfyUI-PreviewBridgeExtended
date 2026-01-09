"""
Preview Bridge Extended - Main Node Class

This is the main ComfyUI node class that implements the Preview Bridge Extended functionality.
"""

import os
import logging
import torch
from typing import Tuple, Optional, Dict, Any
import folder_paths

from .utils import is_clipspace_path, load_mask_from_clipspace, register_clipspace_image
from .mask_ops import is_mask_empty, resize_mask, process_input_mask
from .caches import (
    get_cache, set_cache,
    get_original_input_cache, set_original_input_cache, delete_original_input_cache,
    get_context_cache, set_context_cache,
    set_previewbridge_image, _preview_bridge_image_id_map, _preview_bridge_image_name_map
)
from .layer_cache import get_layer_cache, decompose_and_store, get_output_mask, get_preview_masks
from .preview import save_preview_images, generate_info


class PreviewBridgeExtended:
    """
    Extended Preview Bridge with optional mask input support.

    Allows users to:
    - Pass masks from upstream nodes (LoadImage, detection nodes, etc.)
    - Choose how input masks interact with MaskEditor drawings
    - Restore masks across image changes (never, always, if_same_size)
    - Block execution based on combined or individual mask states
    """

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "images": ("IMAGE",),  # Input image(s)
                "image": ("STRING", {"default": ""}),  # Clipspace path from widget
            },
            "optional": {
                "mask_opt": ("MASK",),  # Optional mask from upstream
                "mask_output": (
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
                ),
                "editor_target": (
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
                ),
                "restore_mask": (
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
                ),
                "block": (
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
                ),
            },
            "hidden": {
                "unique_id": "UNIQUE_ID",
                "prompt": "PROMPT",
                "extra_pnginfo": "EXTRA_PNGINFO",
            }
        }

    RETURN_TYPES = ("IMAGE", "MASK", "STRING")
    RETURN_NAMES = ("image", "mask", "info")
    FUNCTION = "process"
    OUTPUT_NODE = True
    CATEGORY = "DazzleNodes"
    DESCRIPTION = (
        "Extended Preview Bridge with optional mask input. "
        "Combines masks from upstream nodes with MaskEditor drawings. "
        "Supports restore_mask functionality for mask persistence."
    )

    def __init__(self):
        self.output_dir = folder_paths.get_temp_directory()
        self.type = "temp"

    def process(
        self,
        images: torch.Tensor,
        image: str = "",
        mask_opt: Optional[torch.Tensor] = None,
        mask_output: str = "combined",
        editor_target: str = "mask_editor",
        restore_mask: str = "never",
        block: str = "never",
        unique_id: str = "",
        prompt=None,
        extra_pnginfo=None
    ) -> Dict[str, Any]:
        """
        Process images with optional mask input and restore_mask functionality.

        Uses LayerCache for unified layer storage:
        - upstream: Immutable mask from mask_opt input
        - additions: User-drawn areas (orange layer)
        - subtractions: User-erased areas from upstream (removed from red layer)

        Args:
            images: Input image tensor [B, H, W, C]
            image: Clipspace path from widget (populated by MaskEditor)
            mask_opt: Optional mask from upstream [B, H, W] or [H, W]
            mask_output: What goes to OUTPUT mask ("combined", "input_mask", "mask_editor")
            editor_target: Which layer MaskEditor affects ("mask_editor", "input_mask", "combined")
            restore_mask: Mask restoration mode ("never", "always", "if_same_size")
            block: Block mode ("never", "if_empty_mask", "always")
            unique_id: Node unique ID for caching
            prompt: ComfyUI prompt data
            extra_pnginfo: Extra PNG info for saving

        Returns:
            Dict with "ui" and "result" keys
        """
        # Get image dimensions
        batch, height, width, channels = images.shape
        target_size = (height, width)

        # DIAGNOSTIC: Log widget values received by process()
        logging.info(f"[PreviewBridgeExtended] process() called: unique_id={unique_id}")
        logging.info(f"[PreviewBridgeExtended] process() WIDGETS: mask_output='{mask_output}', "
                     f"editor_target='{editor_target}', restore_mask='{restore_mask}', block='{block}'")

        # Detect if images have changed
        images_changed = self._detect_images_changed(images, unique_id)

        # Get LayerCache for this node
        layer_cache = get_layer_cache(unique_id)

        # Handle image change - validate LayerCache
        if images_changed:
            layer_cache.validate_image(images)
            if restore_mask == "never":
                # Clear LayerCache when images change and restore is disabled
                layer_cache.clear()
                logging.info(f"[PreviewBridgeExtended] Image changed, LayerCache cleared (restore_mask=never)")

        # Handle clipspace registration when images haven't changed
        if not images_changed and image and image not in _preview_bridge_image_id_map:
            if is_clipspace_path(image):
                register_clipspace_image(image, unique_id)

        # Process input mask from upstream (mask_opt)
        upstream_input_mask = process_input_mask(mask_opt, target_size)
        upstream_input_valid = not is_mask_empty(upstream_input_mask)

        # Update LayerCache with upstream (handles change detection internally)
        layer_cache.on_upstream_change(upstream_input_mask)

        # Store original input mask for preview coloring
        if upstream_input_valid:
            set_original_input_cache(unique_id, upstream_input_mask.clone())
        else:
            delete_original_input_cache(unique_id)

        # Load clipspace mask (raw user edits from MaskEditor)
        clipspace_mask = self._load_clipspace_mask(
            unique_id=unique_id,
            images_changed=images_changed,
            restore_mask=restore_mask,
            target_size=target_size,
            clipspace_path=image
        )
        clipspace_mask_valid = not is_mask_empty(clipspace_mask)

        # =====================================================
        # LAYERCACHE: Decompose clipspace into canonical layers
        # This is the ONLY layer logic - LayerCache handles all cases
        # =====================================================
        if clipspace_mask_valid:
            layer_cache = decompose_and_store(
                node_id=unique_id,
                clipspace=clipspace_mask,
                upstream=upstream_input_mask,
                editor_target=editor_target,
                target_size=target_size
            )
            logging.info(f"[PreviewBridgeExtended] LayerCache updated: {layer_cache.debug_info()}")
        else:
            layer_cache.last_editor_target = editor_target

        # =====================================================
        # GET OUTPUT MASK FROM LAYERCACHE
        # Simple, unified output - no 9-combination matrix needed
        # =====================================================
        final_mask = get_output_mask(unique_id, mask_output)

        # Log final mask
        if final_mask is not None:
            logging.info(f"[PreviewBridgeExtended] FINAL MASK (LayerCache): shape={final_mask.shape}, "
                         f"sum={final_mask.sum().item():.2f}")
        else:
            logging.info(f"[PreviewBridgeExtended] FINAL MASK (LayerCache): None")

        # Create empty mask if none available
        if final_mask is None:
            final_mask = torch.zeros((1, height, width), dtype=torch.float32)

        # Ensure mask has batch dimension
        if len(final_mask.shape) == 2:
            final_mask = final_mask.unsqueeze(0)

        # Check if mask is empty for blocking decision
        is_empty = is_mask_empty(final_mask)

        # Check if editor has content (for if_empty_editor blocking)
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

        # Cache context for API preview refresh (JS-Python communication)
        set_context_cache(unique_id, {
            'images': images,
            'upstream_input_mask': upstream_input_mask,
            'original_input_mask': get_original_input_cache(unique_id),
            'mask_output': mask_output,
            'editor_target': editor_target,
            'layer_cache': layer_cache,
        })

        # =====================================================
        # GET PREVIEW MASKS FROM LAYERCACHE
        # Simple, unified preview selection
        # =====================================================
        preview_input_mask, preview_editor_mask = get_preview_masks(unique_id, mask_output)

        # Save preview images with separate mask overlays
        preview_result = save_preview_images(
            images=images,
            input_mask=preview_input_mask,
            editor_mask=preview_editor_mask,
            editor_target=editor_target,
            unique_id=unique_id,
            original_mask=get_original_input_cache(unique_id),
            prompt=prompt,
            extra_pnginfo=extra_pnginfo
        )

        # Register in preview bridge system for clipspace integration
        if preview_result:
            preview_filename = preview_result[0].get('filename', '')
            preview_subfolder = preview_result[0].get('subfolder', '')
            if preview_filename:
                preview_path = os.path.join(
                    folder_paths.get_temp_directory(),
                    preview_subfolder,
                    preview_filename
                )
                set_previewbridge_image(unique_id, preview_path, preview_result[0])
                if image:
                    _preview_bridge_image_id_map[image] = (preview_path, preview_result[0])
                    _preview_bridge_image_name_map[(unique_id, preview_path)] = (image, preview_result[0])

        # Update basic caches for ComfyUI integration
        set_cache(unique_id, images, preview_result)

        # Handle blocking
        should_block = (
            block == "always" or
            (block == "if_empty_mask" and is_empty) or
            (block == "if_empty_editor" and not editor_has_content)
        )

        if should_block:
            try:
                from comfy_execution.graph import ExecutionBlocker
                result = (ExecutionBlocker(None), ExecutionBlocker(None), info)
            except ImportError:
                result = (images, final_mask, info)
        else:
            result = (images, final_mask, info)

        return {
            "ui": {"images": preview_result},
            "result": result,
        }

    def _detect_images_changed(self, images: torch.Tensor, unique_id: str) -> bool:
        """Detect if input images have changed from cached version."""
        cached = get_cache(unique_id)
        if cached is None:
            return True

        cached_images, _ = cached
        # Use 'is not' for identity check (same tensor object)
        return cached_images is not images

    def _load_clipspace_mask(
        self,
        unique_id: str,
        images_changed: bool,
        restore_mask: str,
        target_size: Tuple[int, int],
        clipspace_path: str = ""
    ) -> Optional[torch.Tensor]:
        """
        Load mask from clipspace file or cache.

        This loads the raw user edits from MaskEditor. The routing to
        appropriate caches (editor vs input_override) is handled by the
        caller based on editor_target setting.

        Priority order:
        1. Clipspace file (user's most recent MaskEditor edit) - ALWAYS checked
        2. Combined cache fallback (editor + input_override) - controlled by restore_mask

        Args:
            unique_id: Node unique ID
            images_changed: Whether images have changed
            restore_mask: Restoration mode (controls cache fallback only)
            target_size: (height, width) for size comparison
            clipspace_path: Path to clipspace file from widget

        Returns:
            Mask tensor or None
        """
        target_height, target_width = target_size

        # ALWAYS try clipspace FIRST - this is the CURRENT user edit
        if is_clipspace_path(clipspace_path):
            clipspace_mask = load_mask_from_clipspace(clipspace_path)
            if clipspace_mask is not None:
                # File loaded successfully - use it even if empty (user may have erased)
                if is_mask_empty(clipspace_mask):
                    # User erased the mask - return None, don't fall back to cache
                    return None

                # Resize if needed and return
                mask_height = clipspace_mask.shape[1] if len(clipspace_mask.shape) == 3 else clipspace_mask.shape[0]
                mask_width = clipspace_mask.shape[2] if len(clipspace_mask.shape) == 3 else clipspace_mask.shape[1]
                if mask_height != target_height or mask_width != target_width:
                    return resize_mask(clipspace_mask, target_size)
                return clipspace_mask

        # No clipspace available - check if we should restore from LayerCache
        if restore_mask == "never":
            return None

        # CRITICAL: If images haven't changed, DON'T restore from LayerCache!
        # The existing LayerCache state is already correct for the current mode.
        # Restoring would cause re-decomposition with wrong semantics when
        # editor_target changes (e.g., get_combined() treated as "only additions").
        if not images_changed:
            return None

        # Images changed - check if restore_mask allows restoration
        if restore_mask not in ["always", "if_same_size"]:
            return None

        # Try LayerCache fallback - use get_combined() to restore full state
        # This is ONLY for cross-image restoration when images actually changed
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


# Node registration
NODE_CLASS_MAPPINGS = {
    "PreviewBridgeExtended": PreviewBridgeExtended,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "PreviewBridgeExtended": "Preview Bridge Extended (DazzleNodes)",
}
