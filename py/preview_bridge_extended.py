"""
Preview Bridge Extended Node - Enhanced Preview Bridge with optional mask input.

This node extends the Preview Bridge concept from Impact Pack with:
1. Optional MASK input from upstream nodes (LoadImage, SAM, etc.)
2. Configurable mask output selection (combined, input_mask, mask_editor)
3. Proper empty mask detection for blocking decisions
4. Mask combination using OR (union) operation
5. restore_mask functionality with mask_output integration
6. Clipspace integration for capturing user-drawn masks from MaskEditor

Based on clipspace integration from Impact Pack PRs #1009 and #1172.
"""

import os
import time
import logging
import torch
import torch.nn.functional as F
import numpy as np
from PIL import Image, ImageOps
from typing import Tuple, Optional, Dict, Any
import folder_paths
import nodes


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

# Preview bridge registration system for clipspace integration
_pb_id_cnt = time.time()  # Counter for generating unique preview bridge IDs
_preview_bridge_image_id_map: Dict[str, tuple] = {}  # pb_id/path -> (file_path, ui_item)
_preview_bridge_image_name_map: Dict[tuple, tuple] = {}  # (unique_id, path) -> (pb_id, ui_item)


def _set_previewbridge_image(unique_id: str, file_path: str, ui_item: dict) -> str:
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

    @staticmethod
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
            logging.warning(f"[PreviewBridgeExtended] Error loading mask from clipspace: {e}")
            return None

    @staticmethod
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
        global _preview_bridge_image_id_map

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
        _set_previewbridge_image(unique_id, actual_file, ui_item)
        # Also register under the original clipspace path for compatibility
        _preview_bridge_image_id_map[clipspace_path] = (actual_file, ui_item)

        return True

    @staticmethod
    def _is_clipspace_path(path: str) -> bool:
        """Check if a path looks like a clipspace file path."""
        if not path:
            return False
        return "clipspace" in path.lower() or "[input]" in path

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

        Maintains separate caches for:
        - Input masks (mask_opt): displayed with reddish tint
        - Editor masks (MaskEditor/clipspace): displayed with orange tint
        - Input override masks: user edits to the input layer

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
        global _preview_bridge_cache, _preview_bridge_last_mask_cache
        global _preview_bridge_editor_mask_cache, _preview_bridge_input_override_cache
        global _preview_bridge_image_id_map

        # Get image dimensions
        batch, height, width, channels = images.shape
        target_size = (height, width)

        # Detect if images have changed
        images_changed = self._detect_images_changed(images, unique_id)

        # Handle cache clearing based on restore_mask setting
        if images_changed and restore_mask == "never":
            # Clear mask caches when images change and restore is disabled
            if unique_id in _preview_bridge_last_mask_cache:
                del _preview_bridge_last_mask_cache[unique_id]
            if unique_id in _preview_bridge_editor_mask_cache:
                del _preview_bridge_editor_mask_cache[unique_id]
            if unique_id in _preview_bridge_input_override_cache:
                del _preview_bridge_input_override_cache[unique_id]
            # Also clear delta caches
            if unique_id in _preview_bridge_additions_cache:
                del _preview_bridge_additions_cache[unique_id]
            if unique_id in _preview_bridge_subtractions_cache:
                del _preview_bridge_subtractions_cache[unique_id]

        # Handle clipspace registration when images haven't changed
        # This handles the case where user edited mask on same image
        if not images_changed and image and image not in _preview_bridge_image_id_map:
            if self._is_clipspace_path(image):
                self.register_clipspace_image(image, unique_id)

        # Process input mask from upstream (mask_opt)
        upstream_input_mask = self._process_input_mask(mask_opt, target_size)
        upstream_input_valid = not self._is_mask_empty(upstream_input_mask)

        # Store original input mask IMMUTABLY for delta computation
        # This is the reference for computing what user added vs preserved
        # Update when: images change, upstream mask changes, or not cached yet
        cached_original = _preview_bridge_original_input_cache.get(unique_id)
        upstream_mask_changed = upstream_input_valid and (
            cached_original is None or
            cached_original.shape != upstream_input_mask.shape or
            not torch.equal(cached_original, upstream_input_mask)
        )

        if images_changed or upstream_mask_changed or unique_id not in _preview_bridge_original_input_cache:
            if upstream_input_valid:
                _preview_bridge_original_input_cache[unique_id] = upstream_input_mask.clone()
                logging.debug(f"[PreviewBridgeExtended] Updated original_input_cache for {unique_id} (images_changed={images_changed}, mask_changed={upstream_mask_changed})")
            elif unique_id in _preview_bridge_original_input_cache:
                del _preview_bridge_original_input_cache[unique_id]
            # Clear delta caches on new input
            if unique_id in _preview_bridge_additions_cache:
                del _preview_bridge_additions_cache[unique_id]
            if unique_id in _preview_bridge_subtractions_cache:
                del _preview_bridge_subtractions_cache[unique_id]

        # When restore_mask=never AND upstream mask changed, clear ALL editor caches
        # This ensures clipspace/editor data doesn't override fresh upstream mask
        if upstream_mask_changed and restore_mask == "never":
            if unique_id in _preview_bridge_editor_mask_cache:
                del _preview_bridge_editor_mask_cache[unique_id]
            if unique_id in _preview_bridge_input_override_cache:
                del _preview_bridge_input_override_cache[unique_id]
            if unique_id in _preview_bridge_last_mask_cache:
                del _preview_bridge_last_mask_cache[unique_id]
            logging.debug(f"[PreviewBridgeExtended] Cleared editor caches for {unique_id} (upstream_mask_changed=True, restore_mask=never)")

        # Load clipspace mask (raw user edits from MaskEditor)
        clipspace_mask = self._load_clipspace_mask(
            unique_id=unique_id,
            images_changed=images_changed,
            restore_mask=restore_mask,
            target_size=target_size,
            clipspace_path=image
        )
        clipspace_mask_valid = not self._is_mask_empty(clipspace_mask)

        # Route clipspace edits to appropriate cache based on editor_target
        # This determines which layer(s) the MaskEditor affects
        editor_mask = None
        input_override = None

        if editor_target == "mask_editor":
            # MaskEditor edits go to orange layer only
            editor_mask = clipspace_mask
            # Get any existing input override from cache (preserve it)
            input_override = _preview_bridge_input_override_cache.get(unique_id)
        elif editor_target == "input_mask":
            # MaskEditor edits override the input (red) layer only
            input_override = clipspace_mask
            # Get any existing editor mask from cache (preserve it)
            editor_mask = _preview_bridge_editor_mask_cache.get(unique_id)
        elif editor_target == "combined":
            # MaskEditor edits affect both layers
            editor_mask = clipspace_mask
            input_override = clipspace_mask

        editor_mask_valid = not self._is_mask_empty(editor_mask)
        input_override_valid = not self._is_mask_empty(input_override)

        # Effective input mask = upstream OR input_override (if override exists, it takes precedence)
        if input_override_valid:
            input_mask = input_override
        else:
            input_mask = upstream_input_mask
        input_mask_valid = not self._is_mask_empty(input_mask)

        # Get editor cache for 9-combination matrix (preserved editor when editing input layer)
        # This is separate from editor_mask which varies based on editor_target
        editor_cache = _preview_bridge_editor_mask_cache.get(unique_id)

        # Determine final output mask based on mask_output and editor_target
        # Implements the full 9-combination matrix for predictable behavior
        final_mask = self._determine_final_mask(
            mask_output=mask_output,
            editor_target=editor_target,
            upstream_mask=upstream_input_mask,  # Immutable upstream reference
            upstream_mask_valid=upstream_input_valid,
            editor_mask=editor_mask,
            editor_mask_valid=editor_mask_valid,
            input_override=input_override,
            input_override_valid=input_override_valid,
            editor_cache=editor_cache,
            target_size=target_size
        )

        # Create empty mask if none available
        if final_mask is None:
            final_mask = torch.zeros((1, height, width), dtype=torch.float32)

        # Ensure mask has batch dimension
        if len(final_mask.shape) == 2:
            final_mask = final_mask.unsqueeze(0)

        # Check if mask is empty for blocking decision
        is_empty = self._is_mask_empty(final_mask)

        # Generate info string
        info = self._generate_info(
            input_mask_valid=input_mask_valid,
            restored_mask_valid=editor_mask_valid,
            mask_output=mask_output,
            restore_mask=restore_mask,
            block=block,
            images_changed=images_changed,
            final_empty=is_empty,
            image_size=(width, height)
        )

        # Cache context for API preview refresh (JS-Python communication)
        # This allows the API to regenerate colored preview without re-running workflow
        _preview_bridge_context_cache[unique_id] = {
            'images': images,
            'upstream_input_mask': upstream_input_mask,
            'original_input_mask': _preview_bridge_original_input_cache.get(unique_id),  # Immutable reference
            'input_override': input_override,
            'editor_mask': editor_mask,
            'mask_output': mask_output,
            'editor_target': editor_target,
        }

        # Determine which masks to show in preview based on mask_output
        # This keeps masks visually distinct for user clarity:
        # - Red tint = input_mask layer (from upstream mask_opt)
        # - Orange tint = editor_mask layer (user additions)
        #
        # IMPORTANT: Use upstream_input_mask for red layer, NOT input_mask.
        # When editor_target=combined, input_mask gets merged with editor content,
        # which would cause both layers to render as red. Using the original
        # upstream mask preserves correct layer identity and colors.
        preview_input_mask = None
        preview_editor_mask = None

        if mask_output == "combined":
            # Show both masks with distinct colors
            preview_input_mask = upstream_input_mask if upstream_input_valid else None
            preview_editor_mask = editor_mask
        elif mask_output == "input_mask":
            # Preview should match what OUTPUT produces
            # Must account for editor_target to show correct modified input
            if editor_target == "combined" and upstream_input_valid and input_override_valid:
                # When editor_target=combined, erasures tracked via intersection
                preview_input_mask = self._combine_masks_and(upstream_input_mask, input_override, target_size)
                if preview_input_mask is None:
                    preview_input_mask = upstream_input_mask
            elif editor_target == "input_mask" and input_override_valid:
                # When editor_target=input_mask, user directly edited input layer
                preview_input_mask = input_override
            else:
                # editor_target=mask_editor means input wasn't edited, use upstream
                preview_input_mask = upstream_input_mask if upstream_input_valid else input_mask
        elif mask_output == "mask_editor":
            # Only show editor mask (orange)
            preview_editor_mask = editor_mask

        # Save preview images with separate mask overlays
        # Pass original_mask for delta-based coloring (red=preserved, orange=additions)
        preview_result = self._save_preview_images(
            images=images,
            input_mask=preview_input_mask,
            editor_mask=preview_editor_mask,
            editor_target=editor_target,
            unique_id=unique_id,
            original_mask=_preview_bridge_original_input_cache.get(unique_id),
            prompt=prompt,
            extra_pnginfo=extra_pnginfo
        )

        # Register in preview bridge system for clipspace integration
        if preview_result:
            # Build path to the saved preview image
            preview_filename = preview_result[0].get('filename', '')
            preview_subfolder = preview_result[0].get('subfolder', '')
            if preview_filename:
                preview_path = os.path.join(
                    folder_paths.get_temp_directory(),
                    preview_subfolder,
                    preview_filename
                )
                # Register the preview image in our bridge system
                _set_previewbridge_image(unique_id, preview_path, preview_result[0])
                # Also register under the clipspace path if present
                if image:
                    _preview_bridge_image_id_map[image] = (preview_path, preview_result[0])
                    _preview_bridge_image_name_map[(unique_id, preview_path)] = (image, preview_result[0])

        # Update caches
        _preview_bridge_cache[unique_id] = (images, preview_result)

        # Cache the combined final mask
        if not is_empty:
            _preview_bridge_last_mask_cache[unique_id] = final_mask
        else:
            # Final mask is empty - clear from cache
            if unique_id in _preview_bridge_last_mask_cache:
                del _preview_bridge_last_mask_cache[unique_id]

        # Cache masks separately based on editor_target
        # This ensures the correct layer is editable on next MaskEditor open
        if editor_target == "mask_editor" or editor_target == "combined":
            if editor_mask_valid:
                _preview_bridge_editor_mask_cache[unique_id] = editor_mask
            else:
                if unique_id in _preview_bridge_editor_mask_cache:
                    del _preview_bridge_editor_mask_cache[unique_id]

        if editor_target == "input_mask" or editor_target == "combined":
            if input_override_valid:
                _preview_bridge_input_override_cache[unique_id] = input_override
            else:
                if unique_id in _preview_bridge_input_override_cache:
                    del _preview_bridge_input_override_cache[unique_id]

        # Handle blocking
        # - if_empty_mask: blocks if OUTPUT mask is empty
        # - if_empty_editor: blocks if user hasn't drawn anything in MaskEditor
        should_block = (
            block == "always" or
            (block == "if_empty_mask" and is_empty) or
            (block == "if_empty_editor" and not editor_mask_valid)
        )

        if should_block:
            try:
                from comfy_execution.graph import ExecutionBlocker
                result = (ExecutionBlocker(None), ExecutionBlocker(None), info)
            except ImportError:
                # ComfyUI version doesn't support ExecutionBlocker
                result = (images, final_mask, info)
        else:
            result = (images, final_mask, info)

        return {
            "ui": {"images": preview_result},
            "result": result,
        }

    def _detect_images_changed(self, images: torch.Tensor, unique_id: str) -> bool:
        """Detect if input images have changed from cached version."""
        global _preview_bridge_cache

        if unique_id not in _preview_bridge_cache:
            return True

        cached_images, _ = _preview_bridge_cache[unique_id]
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
        global _preview_bridge_editor_mask_cache, _preview_bridge_input_override_cache

        target_height, target_width = target_size

        # ALWAYS try clipspace FIRST - this is the CURRENT user edit
        if self._is_clipspace_path(clipspace_path):
            clipspace_mask = self.load_mask_from_clipspace(clipspace_path)
            if clipspace_mask is not None:
                # File loaded successfully - use it even if empty (user may have erased)
                if self._is_mask_empty(clipspace_mask):
                    # User erased the mask - return None, don't fall back to cache
                    return None

                # Resize if needed and return
                mask_height = clipspace_mask.shape[1] if len(clipspace_mask.shape) == 3 else clipspace_mask.shape[0]
                mask_width = clipspace_mask.shape[2] if len(clipspace_mask.shape) == 3 else clipspace_mask.shape[1]
                if mask_height != target_height or mask_width != target_width:
                    return self._resize_mask(clipspace_mask, target_size)
                return clipspace_mask

        # No clipspace available - check if we should restore from cache
        if restore_mask == "never":
            return None

        if images_changed and restore_mask not in ["always", "if_same_size"]:
            return None

        # Try cache fallback - check both editor and input_override caches
        # Return the first valid one found (editor mask takes precedence)
        for cache in [_preview_bridge_editor_mask_cache, _preview_bridge_input_override_cache]:
            mask = cache.get(unique_id)
            if mask is not None and not self._is_mask_empty(mask):
                mask_height = mask.shape[1] if len(mask.shape) == 3 else mask.shape[0]
                mask_width = mask.shape[2] if len(mask.shape) == 3 else mask.shape[1]

                if restore_mask == "if_same_size":
                    if mask_height == target_height and mask_width == target_width:
                        return mask
                elif restore_mask == "always":
                    if mask_height != target_height or mask_width != target_width:
                        return self._resize_mask(mask, target_size)
                    return mask

        return None

    def _determine_final_mask(
        self,
        mask_output: str,
        editor_target: str,
        upstream_mask: Optional[torch.Tensor],
        upstream_mask_valid: bool,
        editor_mask: Optional[torch.Tensor],
        editor_mask_valid: bool,
        input_override: Optional[torch.Tensor],
        input_override_valid: bool,
        editor_cache: Optional[torch.Tensor],
        target_size: Tuple[int, int]
    ) -> Optional[torch.Tensor]:
        """
        Determine final mask based on mask_output and editor_target settings.

        This implements the full 9-combination matrix (3 mask_output × 3 editor_target).

        9-Combination Matrix:
        | mask_output  | editor_target | Output Logic                    |
        |--------------|---------------|---------------------------------|
        | combined     | combined      | editor_mask (complete state)    |
        | combined     | mask_editor   | upstream OR editor_mask         |
        | combined     | input_mask    | input_override OR editor_cache  |
        | input_mask   | combined      | editor_mask AND upstream (intersection) |
        | input_mask   | mask_editor   | upstream (immutable)            |
        | input_mask   | input_mask    | input_override (user's edits)   |
        | mask_editor  | combined      | additions (delta from upstream) |
        | mask_editor  | mask_editor   | editor_mask                     |
        | mask_editor  | input_mask    | editor_cache (preserved)        |

        Args:
            mask_output: What to output ("combined", "input_mask", "mask_editor")
            editor_target: Which layer MaskEditor affects ("combined", "mask_editor", "input_mask")
            upstream_mask: The immutable upstream input mask from mask_opt
            upstream_mask_valid: Whether upstream_mask is valid/non-empty
            editor_mask: The editor mask from clipspace (current edits)
            editor_mask_valid: Whether editor_mask is valid/non-empty
            input_override: User's modifications to the input layer
            input_override_valid: Whether input_override is valid/non-empty
            editor_cache: Preserved editor mask from cache (when editing input layer)
            target_size: (height, width) for resizing
        """
        editor_cache_valid = editor_cache is not None and not self._is_mask_empty(editor_cache)

        # =========================================
        # mask_output = "combined"
        # =========================================
        if mask_output == "combined":
            if editor_target == "combined":
                # Combined editing: editor_mask IS the final merged state (with erasures)
                if editor_mask_valid:
                    return editor_mask
                # Fall back to upstream if no edits yet
                return upstream_mask if upstream_mask_valid else None

            elif editor_target == "mask_editor":
                # Separate layer editing: OR combine upstream + editor
                return self._combine_masks_or(upstream_mask, editor_mask, target_size)

            elif editor_target == "input_mask":
                # User editing input layer: OR combine input_override + cached editor
                return self._combine_masks_or(input_override, editor_cache, target_size)

        # =========================================
        # mask_output = "input_mask"
        # =========================================
        elif mask_output == "input_mask":
            if editor_target == "combined":
                # User edited both layers merged, but wants only "input" portion
                # Return intersection: what's in BOTH clipspace AND upstream
                # This preserves user's subtractions from upstream while ignoring additions
                if editor_mask_valid and upstream_mask_valid:
                    return self._combine_masks_and(editor_mask, upstream_mask, target_size)
                # If no edits, return upstream; if no upstream, return None
                return upstream_mask if upstream_mask_valid else None

            elif editor_target == "mask_editor":
                # User editing editor layer only: return immutable upstream
                return upstream_mask if upstream_mask_valid else None

            elif editor_target == "input_mask":
                # User editing input layer: return their modified input
                if input_override_valid:
                    return input_override
                # Fall back to upstream if no modifications yet
                return upstream_mask if upstream_mask_valid else None

        # =========================================
        # mask_output = "mask_editor"
        # =========================================
        elif mask_output == "mask_editor":
            if editor_target == "combined":
                # User edited both layers merged, but wants only "additions"
                # Compute delta: areas in editor_mask but NOT in upstream
                if not editor_mask_valid:
                    return None
                if not upstream_mask_valid:
                    # No upstream, so all of editor_mask is "additions"
                    return editor_mask
                _, additions, _ = self._compute_mask_delta(upstream_mask, editor_mask, target_size)
                return additions

            elif editor_target == "mask_editor":
                # User editing editor layer: return their editor mask directly
                return editor_mask if editor_mask_valid else None

            elif editor_target == "input_mask":
                # User editing input layer: return preserved editor from cache
                return editor_cache if editor_cache_valid else None

        return None

    def _process_input_mask(
        self,
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

        if self._is_mask_empty(mask):
            return None

        # Resize mask to match image dimensions
        return self._resize_mask(mask, target_size)

    def _resize_mask(
        self,
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

    def _is_mask_empty(self, mask: Optional[torch.Tensor]) -> bool:
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

    def _combine_masks_or(
        self,
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
            if mask is not None and not self._is_mask_empty(mask):
                # Resize to target size
                resized = self._resize_mask(mask, target_size)
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

    def _combine_masks_and(
        self,
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
        if mask1 is None or self._is_mask_empty(mask1):
            return None
        if mask2 is None or self._is_mask_empty(mask2):
            return None

        # Resize both to target size
        m1 = self._resize_mask(mask1, target_size)
        m2 = self._resize_mask(mask2, target_size)

        # Match batch sizes
        if m1.shape[0] != m2.shape[0]:
            if m1.shape[0] == 1:
                m1 = m1.expand(m2.shape[0], -1, -1)
            elif m2.shape[0] == 1:
                m2 = m2.expand(m1.shape[0], -1, -1)

        # Soft intersection using min (fuzzy AND)
        intersection = torch.min(m1, m2)
        return intersection

    def _compute_mask_delta(
        self,
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
        if original_mask is None or self._is_mask_empty(original_mask):
            if new_mask is None or self._is_mask_empty(new_mask):
                return None, None, None
            # No original, all new content is additions
            additions = self._resize_mask(new_mask, target_size)
            return None, additions, None

        if new_mask is None or self._is_mask_empty(new_mask):
            # Original exists but new is empty - all original was subtracted
            subtractions = self._resize_mask(original_mask, target_size)
            return None, None, subtractions

        # Both exist - compute delta
        orig = self._resize_mask(original_mask, target_size)
        new = self._resize_mask(new_mask, target_size)

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

    def _save_preview_images(
        self,
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

        Shows two distinct colors using delta-based coloring when original_mask provided:
        - Reddish tint: preserved areas (original mask_opt still present)
        - Orange tint: additions (new areas user drew)

        Args:
            images: Input images [B, H, W, C]
            input_mask: Input mask tensor [B, H, W] or None
            editor_mask: Editor mask tensor [B, H, W] or None
            editor_target: Which layer is editable ("mask_editor", "input_mask", "combined")
            unique_id: Node unique ID
            original_mask: Immutable original mask_opt for delta computation
            prompt: ComfyUI prompt data
            extra_pnginfo: Extra PNG info

        Returns:
            List of image info dicts for UI
        """
        input_empty = self._is_mask_empty(input_mask)
        editor_empty = self._is_mask_empty(editor_mask)

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
        masked_images = self._apply_mask_overlays(
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

    def _apply_mask_overlays(
        self,
        images: torch.Tensor,
        input_mask: Optional[torch.Tensor],
        editor_mask: Optional[torch.Tensor],
        editor_target: str = "mask_editor",
        original_mask: Optional[torch.Tensor] = None,
        for_editing: bool = False
    ) -> torch.Tensor:
        """
        Apply mask overlays with distinct colors for visual feedback.

        Two-layer delta system:
        When original_mask is provided, we compute delta to distinguish:
        - Preserved areas (in original AND current): Red tint, alpha=1 (opaque)
        - Additions (in current but NOT original): Orange tint, alpha=0 (transparent)
        - Subtractions (in original but NOT current): No color (erased)

        When original_mask is None (initial display, no edits yet):
        - input_mask: Red tint, alpha=1 (opaque, for display only)
        - editor_mask: Orange tint, alpha=0 (editable)

        for_editing mode:
        When True, puts ALL editable content into alpha for MaskEditor.
        NO RGB tinting is applied - just original image with proper alpha.
        Used by prepare-for-edit API before opening MaskEditor.

        Args:
            images: RGB images [B, H, W, 3]
            input_mask: Input mask tensor [B, H, W] or None
            editor_mask: Editor mask tensor [B, H, W] or None
            editor_target: Controls alpha for editing mode
            original_mask: Immutable original mask_opt for delta computation
            for_editing: If True, prepare image for MaskEditor (all editable in alpha)

        Returns:
            RGBA images [B, H, W, 4] with appropriate colors and alpha
        """
        batch, height, width, channels = images.shape
        target_size = (height, width)

        # Prepare masks - resize and batch-match
        def prepare_mask(mask: Optional[torch.Tensor]) -> Optional[torch.Tensor]:
            if mask is None or self._is_mask_empty(mask):
                return None

            # Ensure 3D tensor
            if len(mask.shape) == 2:
                mask = mask.unsqueeze(0)

            # Resize if needed
            if mask.shape[1] != height or mask.shape[2] != width:
                mask = self._resize_mask(mask, target_size)

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
        if for_editing:
            # Start with fully opaque alpha
            alpha = torch.ones((batch, height, width), dtype=images.dtype, device=images.device)

            # Compute delta if we have both original and editor masks
            # This separates "preserved" (from original) vs "additions" (user-drawn)
            preserved = None
            additions = None
            if original_m is not None and editor_m is not None:
                preserved, additions, _ = self._compute_mask_delta(
                    original_m, editor_m, target_size
                )

            if editor_target == "mask_editor":
                # Only editor additions are editable (NOT preserved input areas)
                # Bake input mask (original/preserved) as RED RGB (visible but not editable)
                input_to_bake = original_m if original_m is not None else input_m
                if input_to_bake is not None:
                    blend = input_to_bake * 0.5
                    rgba[:, :, :, 0] = rgba[:, :, :, 0] * (1 - blend) + 1.0 * blend
                    rgba[:, :, :, 1] = rgba[:, :, :, 1] * (1 - blend) + 0.2 * blend
                    rgba[:, :, :, 2] = rgba[:, :, :, 2] * (1 - blend) + 0.2 * blend

                # Put ONLY additions in alpha (user-drawn areas, not preserved input)
                if additions is not None:
                    additions_p = prepare_mask(additions)
                    if additions_p is not None:
                        alpha = 1.0 - additions_p
                elif editor_m is not None and original_m is None:
                    # No original, all of editor_m is additions
                    alpha = 1.0 - editor_m
                # If no editor mask yet, alpha stays 1.0 (user draws new content)

            elif editor_target == "input_mask":
                # Only input mask is editable (preserved + any input modifications)
                # Bake ONLY additions as ORANGE RGB (visible but not editable)
                if additions is not None:
                    additions_p = prepare_mask(additions)
                    if additions_p is not None:
                        blend = additions_p * 0.5
                        rgba[:, :, :, 0] = rgba[:, :, :, 0] * (1 - blend) + 1.0 * blend
                        rgba[:, :, :, 1] = rgba[:, :, :, 1] * (1 - blend) + 0.5 * blend
                        rgba[:, :, :, 2] = rgba[:, :, :, 2] * (1 - blend) + 0.0 * blend
                elif editor_m is not None and original_m is None:
                    # No original, all of editor_m is additions - bake as orange
                    blend = editor_m * 0.5
                    rgba[:, :, :, 0] = rgba[:, :, :, 0] * (1 - blend) + 1.0 * blend
                    rgba[:, :, :, 1] = rgba[:, :, :, 1] * (1 - blend) + 0.5 * blend
                    rgba[:, :, :, 2] = rgba[:, :, :, 2] * (1 - blend) + 0.0 * blend

                # Put input mask (preserved areas from original, or full original if no edits) in alpha
                if preserved is not None:
                    preserved_p = prepare_mask(preserved)
                    if preserved_p is not None:
                        alpha = 1.0 - preserved_p
                elif original_m is not None:
                    # No edits yet, full original is editable
                    alpha = 1.0 - original_m
                elif input_m is not None:
                    alpha = 1.0 - input_m

            elif editor_target == "combined":
                # When editor_target=combined, editor_m IS the complete combined state
                # (user can add AND erase), not just additions to overlay on original.
                # Do NOT OR with original - that would restore erased areas.

                # Debug logging for combined mode
                logging.info(f"[PreviewBridgeExtended] for_editing=combined: "
                            f"original_m={original_m is not None}, input_m={input_m is not None}, "
                            f"editor_m={editor_m is not None}")

                if editor_m is not None:
                    # Use editor_m directly - it already has the complete combined state
                    alpha = 1.0 - editor_m
                    logging.info(f"[PreviewBridgeExtended] Using editor_m directly: sum={editor_m.sum().item():.2f}")
                elif original_m is not None:
                    # No edits yet - use original as starting point
                    alpha = 1.0 - original_m
                    logging.info(f"[PreviewBridgeExtended] No editor_m, using original_m: sum={original_m.sum().item():.2f}")
                elif input_m is not None:
                    alpha = 1.0 - input_m
                    logging.info(f"[PreviewBridgeExtended] No editor_m/original_m, using input_m: sum={input_m.sum().item():.2f}")
                else:
                    logging.info(f"[PreviewBridgeExtended] No masks available for combined mode")

                logging.info(f"[PreviewBridgeExtended] Final alpha: min={alpha.min().item():.4f}, "
                            f"max={alpha.max().item():.4f}, transparent_pixels={(alpha < 1.0).sum().item()}")

            rgba[:, :, :, 3] = alpha
            return rgba

        # DISPLAY MODE: Apply RGB tinting for visual distinction

        # Determine if we should use delta-based coloring
        # Delta mode: we have original AND editor edits (can compute what was preserved vs added)
        use_delta = original_m is not None and editor_m is not None

        if use_delta:
            # Delta-based coloring: distinguish preserved vs additions
            # Compute delta components
            preserved, additions, subtractions = self._compute_mask_delta(
                original_m, editor_m, target_size
            )

            # Apply preserved areas (red tint: R=1.0, G=0.2, B=0.2)
            # These are areas from the original mask_opt that user kept
            if preserved is not None:
                preserved_p = prepare_mask(preserved)
                if preserved_p is not None:
                    blend = preserved_p * 0.5
                    rgba[:, :, :, 0] = rgba[:, :, :, 0] * (1 - blend) + 1.0 * blend
                    rgba[:, :, :, 1] = rgba[:, :, :, 1] * (1 - blend) + 0.2 * blend
                    rgba[:, :, :, 2] = rgba[:, :, :, 2] * (1 - blend) + 0.2 * blend

            # Apply additions (orange tint: R=1.0, G=0.5, B=0.0)
            # These are new areas user drew that weren't in original
            if additions is not None:
                additions_p = prepare_mask(additions)
                if additions_p is not None:
                    blend = additions_p * 0.5
                    rgba[:, :, :, 0] = rgba[:, :, :, 0] * (1 - blend) + 1.0 * blend
                    rgba[:, :, :, 1] = rgba[:, :, :, 1] * (1 - blend) + 0.5 * blend
                    rgba[:, :, :, 2] = rgba[:, :, :, 2] * (1 - blend) + 0.0 * blend

            # Subtractions: no color needed, user erased these areas

            # Alpha channel for display mode:
            # - Preserved areas: alpha=1 (opaque, no ComfyUI overlay, shows red RGB)
            # - Additions: alpha=0 (transparent, gets ComfyUI orange overlay)
            alpha = torch.ones((batch, height, width), dtype=images.dtype, device=images.device)
            if additions is not None:
                additions_p = prepare_mask(additions)
                if additions_p is not None:
                    alpha = alpha * (1.0 - additions_p)  # Make additions transparent

        else:
            # Non-delta mode: original behavior for initial display
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

    def _generate_info(
        self,
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


# API helper function for JS-Python preview refresh
def generate_preview_for_api(node_id: str, clipspace_path: str) -> Optional[Dict[str, Any]]:
    """
    Generate a colored preview image for a given node using cached context.

    Called by the API endpoint to refresh preview after MaskEditor save
    without re-running the entire workflow.

    Args:
        node_id: The unique_id of the node
        clipspace_path: Path to the clipspace file with updated editor mask

    Returns:
        Dict with 'success', 'image_path', 'image_data' (base64) or 'error'
    """
    import base64
    from io import BytesIO

    # Get cached context for this node
    context = _preview_bridge_context_cache.get(node_id)
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
    mask_output = context.get('mask_output', 'combined')
    editor_target = context.get('editor_target', 'mask_editor')

    if images is None:
        return {
            'success': False,
            'error': 'No cached images for node'
        }

    # Load the new clipspace mask from MaskEditor
    clipspace_mask = PreviewBridgeExtended.load_mask_from_clipspace(clipspace_path)

    # Get image dimensions for mask operations
    batch, height, width, channels = images.shape
    target_size = (height, width)

    logging.info(f"[PreviewBridgeExtended API] refresh-preview called: node_id={node_id}, "
                 f"mask_output={mask_output}, editor_target={editor_target}")
    logging.info(f"[PreviewBridgeExtended API] clipspace_path={clipspace_path}, "
                 f"clipspace_loaded={clipspace_mask is not None}")

    # Create a temporary instance to use helper methods
    pbe = PreviewBridgeExtended()

    clipspace_valid = clipspace_mask is not None and not pbe._is_mask_empty(clipspace_mask)
    logging.info(f"[PreviewBridgeExtended API] clipspace_valid={clipspace_valid}")

    if clipspace_mask is not None:
        mask_sum = clipspace_mask.sum().item()
        mask_max = clipspace_mask.max().item()
        mask_min = clipspace_mask.min().item()
        logging.info(f"[PreviewBridgeExtended API] clipspace stats: sum={mask_sum:.2f}, "
                     f"min={mask_min:.4f}, max={mask_max:.4f}, shape={clipspace_mask.shape}")

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

    # Effective input mask = upstream OR input_override
    if input_override is not None and not pbe._is_mask_empty(input_override):
        input_mask = input_override
    else:
        input_mask = upstream_input_mask

    # Determine which masks to show based on mask_output
    # IMPORTANT: Use upstream_input_mask for red layer to preserve correct colors
    # (see comment in process() method for full explanation)
    preview_input_mask = None
    preview_editor_mask = None
    upstream_valid = upstream_input_mask is not None and not pbe._is_mask_empty(upstream_input_mask)

    if mask_output == "combined":
        preview_input_mask = upstream_input_mask if upstream_valid else None
        preview_editor_mask = editor_mask
    elif mask_output == "input_mask":
        # Preview should match what OUTPUT will produce
        # Must account for editor_target to show correct modified input
        if editor_target == "combined" and upstream_valid and input_override is not None:
            # When editor_target=combined, erasures from input are tracked via intersection
            # Output will be: min(upstream, clipspace) - areas in BOTH
            preview_input_mask = pbe._combine_masks_and(upstream_input_mask, input_override, target_size)
            if preview_input_mask is None:
                preview_input_mask = upstream_input_mask
            logging.info(f"[PreviewBridgeExtended API] input_mask preview using AND intersection: "
                        f"sum={preview_input_mask.sum().item():.2f}")
        elif editor_target == "input_mask" and input_override is not None and not pbe._is_mask_empty(input_override):
            # When editor_target=input_mask, user directly edited input layer
            preview_input_mask = input_override
            logging.info(f"[PreviewBridgeExtended API] input_mask preview using input_override: "
                        f"sum={preview_input_mask.sum().item():.2f}")
        else:
            # editor_target=mask_editor means input wasn't edited, use upstream
            preview_input_mask = upstream_input_mask if upstream_valid else input_mask
            logging.info(f"[PreviewBridgeExtended API] input_mask preview using upstream: "
                        f"sum={preview_input_mask.sum().item() if preview_input_mask is not None else 0:.2f}")
    elif mask_output == "mask_editor":
        preview_editor_mask = editor_mask

    # Check if we have any masks to overlay
    input_empty = pbe._is_mask_empty(preview_input_mask)
    editor_empty = pbe._is_mask_empty(preview_editor_mask)

    if input_empty and editor_empty:
        # No masks - just return the original image as base64
        img_tensor = images[0]  # First image in batch
    else:
        # Generate colored preview with overlays
        # Pass original_input_mask for delta-based coloring (red=preserved, orange=additions)
        masked_images = pbe._apply_mask_overlays(
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
    clipspace_valid = clipspace_mask is not None and not pbe._is_mask_empty(clipspace_mask)

    if editor_target == "mask_editor" or editor_target == "combined":
        if clipspace_valid:
            _preview_bridge_editor_mask_cache[node_id] = clipspace_mask
        else:
            if node_id in _preview_bridge_editor_mask_cache:
                del _preview_bridge_editor_mask_cache[node_id]

    if editor_target == "input_mask" or editor_target == "combined":
        if clipspace_valid:
            _preview_bridge_input_override_cache[node_id] = clipspace_mask
        else:
            if node_id in _preview_bridge_input_override_cache:
                del _preview_bridge_input_override_cache[node_id]

    # CRITICAL: Also update context cache so prepare_for_editing has latest masks
    if node_id in _preview_bridge_context_cache:
        _preview_bridge_context_cache[node_id]['editor_mask'] = editor_mask
        _preview_bridge_context_cache[node_id]['input_override'] = input_override

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
    import base64
    from io import BytesIO

    logging.info(f"[PreviewBridgeExtended] prepare_for_editing called for node {node_id}, override={editor_target_override}")

    # Get cached context for this node
    context = _preview_bridge_context_cache.get(node_id)
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
    # Use override from JS if provided, otherwise fall back to cached value
    editor_target = editor_target_override if editor_target_override else context.get('editor_target', 'combined')

    logging.info(f"[PreviewBridgeExtended] Context: editor_target={editor_target}, "
                 f"has_images={images is not None}, "
                 f"has_upstream={upstream_input_mask is not None}, "
                 f"has_original={original_input_mask is not None}, "
                 f"has_editor={cached_editor_mask is not None}")

    if images is None:
        return {
            'success': False,
            'error': 'No cached images for node'
        }

    # Create a temporary instance to use helper methods
    pbe = PreviewBridgeExtended()

    # For editing, we need to put the editable content in alpha
    # Use the original input mask (immutable) or fall back to upstream
    input_mask = original_input_mask if original_input_mask is not None else upstream_input_mask

    # Log mask info with detailed stats for debugging
    input_empty = pbe._is_mask_empty(input_mask)
    editor_empty = pbe._is_mask_empty(cached_editor_mask)
    original_empty = pbe._is_mask_empty(original_input_mask)
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

    # Generate image with for_editing=True
    # This puts the appropriate masks in alpha based on editor_target
    masked_images = pbe._apply_mask_overlays(
        images, input_mask, cached_editor_mask, editor_target,
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

    return {
        'success': True,
        'image_data': data_uri,
        'editor_target': editor_target,
    }


# Node registration
NODE_CLASS_MAPPINGS = {
    "PreviewBridgeExtended": PreviewBridgeExtended,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "PreviewBridgeExtended": "Preview Bridge Extended (DazzleNodes)",
}
