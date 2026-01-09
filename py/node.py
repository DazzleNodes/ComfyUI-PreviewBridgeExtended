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
from .mask_ops import (
    is_mask_empty, resize_mask, process_input_mask,
    combine_masks_or, combine_masks_and, compute_mask_delta
)
from .caches import (
    get_cache, set_cache,
    get_last_mask_cache, set_last_mask_cache, delete_last_mask_cache,
    get_editor_mask_cache, set_editor_mask_cache, delete_editor_mask_cache,
    get_input_override_cache, set_input_override_cache, delete_input_override_cache,
    get_original_input_cache, set_original_input_cache, delete_original_input_cache,
    get_additions_cache, delete_additions_cache,
    get_subtractions_cache, delete_subtractions_cache,
    clear_delta_caches, get_context_cache, set_context_cache,
    set_previewbridge_image, _preview_bridge_image_id_map, _preview_bridge_image_name_map
)
from .layer_cache import get_layer_cache, decompose_and_store, get_output_mask
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
        # Get image dimensions
        batch, height, width, channels = images.shape
        target_size = (height, width)

        # DIAGNOSTIC: Log widget values received by process()
        logging.info(f"[PreviewBridgeExtended] process() called: unique_id={unique_id}")
        logging.info(f"[PreviewBridgeExtended] process() WIDGETS: mask_output='{mask_output}', "
                     f"editor_target='{editor_target}', restore_mask='{restore_mask}', block='{block}'")

        # Detect if images have changed
        images_changed = self._detect_images_changed(images, unique_id)

        # Handle cache clearing based on restore_mask setting
        if images_changed and restore_mask == "never":
            # Clear mask caches when images change and restore is disabled
            delete_last_mask_cache(unique_id)
            delete_editor_mask_cache(unique_id)
            delete_input_override_cache(unique_id)
            # Also clear delta caches
            clear_delta_caches(unique_id)

        # Handle clipspace registration when images haven't changed
        # This handles the case where user edited mask on same image
        if not images_changed and image and image not in _preview_bridge_image_id_map:
            if is_clipspace_path(image):
                register_clipspace_image(image, unique_id)

        # Process input mask from upstream (mask_opt)
        upstream_input_mask = process_input_mask(mask_opt, target_size)
        upstream_input_valid = not is_mask_empty(upstream_input_mask)

        # Store original input mask IMMUTABLY for delta computation
        # This is the reference for computing what user added vs preserved
        # Update when: images change, upstream mask changes, or not cached yet
        cached_original = get_original_input_cache(unique_id)
        upstream_mask_changed = upstream_input_valid and (
            cached_original is None or
            cached_original.shape != upstream_input_mask.shape or
            not torch.equal(cached_original, upstream_input_mask)
        )

        if images_changed or upstream_mask_changed or cached_original is None:
            if upstream_input_valid:
                set_original_input_cache(unique_id, upstream_input_mask.clone())
                logging.debug(f"[PreviewBridgeExtended] Updated original_input_cache for {unique_id} (images_changed={images_changed}, mask_changed={upstream_mask_changed})")
            else:
                delete_original_input_cache(unique_id)
            # Clear delta caches on new input
            clear_delta_caches(unique_id)

        # When restore_mask=never AND upstream mask changed, clear ALL editor caches
        # This ensures clipspace/editor data doesn't override fresh upstream mask
        if upstream_mask_changed and restore_mask == "never":
            delete_editor_mask_cache(unique_id)
            delete_input_override_cache(unique_id)
            delete_last_mask_cache(unique_id)
            logging.debug(f"[PreviewBridgeExtended] Cleared editor caches for {unique_id} (upstream_mask_changed=True, restore_mask=never)")

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
        # MODE SWITCH DECOMPOSITION
        # When switching FROM combined mode TO mask_editor or input_mask,
        # we need to decompose the combined clipspace into separate layers.
        # This preserves user's work when they change modes.
        # =====================================================
        cached_context = get_context_cache(unique_id)
        previous_editor_target = cached_context.get('editor_target') if cached_context else None

        # Check if mode changed from combined to a specific layer mode
        if (previous_editor_target == "combined" and
            editor_target != "combined" and
            clipspace_mask_valid and
            upstream_input_valid):

            logging.info(f"[PreviewBridgeExtended] Mode switch detected: {previous_editor_target} -> {editor_target}")

            # Get the combined state from either clipspace or editor cache
            combined_state = clipspace_mask
            if combined_state is None or is_mask_empty(combined_state):
                combined_state = get_editor_mask_cache(unique_id)

            if combined_state is not None and not is_mask_empty(combined_state):
                # Decompose combined state into additions and subtractions
                _, additions, subtractions = compute_mask_delta(
                    upstream_input_mask, combined_state, target_size
                )

                additions_valid = additions is not None and not is_mask_empty(additions)
                subtractions_valid = subtractions is not None and not is_mask_empty(subtractions)

                logging.info(f"[PreviewBridgeExtended] Decomposition: "
                            f"additions_valid={additions_valid}, subtractions_valid={subtractions_valid}")

                if additions_valid:
                    additions_sum = additions.sum().item()
                    logging.info(f"[PreviewBridgeExtended] Additions sum: {additions_sum:.2f}")

                if subtractions_valid:
                    subtractions_sum = subtractions.sum().item()
                    logging.info(f"[PreviewBridgeExtended] Subtractions sum: {subtractions_sum:.2f}")

                if editor_target == "mask_editor":
                    # Switching to mask_editor mode:
                    # - Store ONLY additions in editor_mask cache
                    # - Store input with subtractions applied in input_override cache
                    if additions_valid:
                        set_editor_mask_cache(unique_id, additions)
                        clipspace_mask = additions  # Use decomposed additions for current edit
                        logging.info(f"[PreviewBridgeExtended] Stored additions in editor cache")

                    if subtractions_valid:
                        # Compute modified input: upstream minus subtractions
                        # This is what the user "kept" from the original mask
                        modified_input = combine_masks_and(upstream_input_mask, combined_state, target_size)
                        if modified_input is not None and not is_mask_empty(modified_input):
                            set_input_override_cache(unique_id, modified_input)
                            logging.info(f"[PreviewBridgeExtended] Stored modified input in input_override cache: "
                                        f"sum={modified_input.sum().item():.2f}")

                elif editor_target == "input_mask":
                    # Switching to input_mask mode:
                    # - Store additions in editor cache (preserve them)
                    # - Store the modified input (intersection) in input_override
                    if additions_valid:
                        set_editor_mask_cache(unique_id, additions)
                        logging.info(f"[PreviewBridgeExtended] Preserved additions in editor cache")

                    # The "input" portion is the intersection of combined state and upstream
                    modified_input = combine_masks_and(upstream_input_mask, combined_state, target_size)
                    if modified_input is not None and not is_mask_empty(modified_input):
                        clipspace_mask = modified_input  # Use decomposed input for current edit
                        set_input_override_cache(unique_id, modified_input)
                        logging.info(f"[PreviewBridgeExtended] Stored modified input: "
                                    f"sum={modified_input.sum().item():.2f}")

        # Also handle switching FROM a specific mode TO combined
        # In this case, we need to reconstruct the combined state from layers
        elif (previous_editor_target in ("mask_editor", "input_mask") and
              editor_target == "combined" and
              upstream_input_valid):

            logging.info(f"[PreviewBridgeExtended] Mode switch to combined: {previous_editor_target} -> {editor_target}")

            # Get preserved layers from caches
            cached_additions = get_editor_mask_cache(unique_id)
            cached_input_override = get_input_override_cache(unique_id)

            cached_additions_valid = cached_additions is not None and not is_mask_empty(cached_additions)
            cached_input_override_valid = cached_input_override is not None and not is_mask_empty(cached_input_override)

            # If we have separate layers, combine them for the combined view
            if cached_additions_valid or cached_input_override_valid:
                # Start with input (either override or upstream)
                base = cached_input_override if cached_input_override_valid else upstream_input_mask

                # Add the additions layer
                if cached_additions_valid:
                    combined_reconstructed = combine_masks_or(base, cached_additions, target_size)
                    if combined_reconstructed is not None:
                        # Only use reconstructed if we don't have a fresh clipspace
                        if not clipspace_mask_valid:
                            clipspace_mask = combined_reconstructed
                            clipspace_mask_valid = True
                            logging.info(f"[PreviewBridgeExtended] Reconstructed combined from layers: "
                                        f"sum={combined_reconstructed.sum().item():.2f}")

        # Update clipspace validity after decomposition
        clipspace_mask_valid = not is_mask_empty(clipspace_mask)

        # =====================================================
        # LAYERCACHE INTEGRATION
        # Store decomposed layers in LayerCache for unified state management
        # This runs in parallel with the legacy cache system during transition
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
            # No clipspace - just update upstream in LayerCache
            layer_cache = get_layer_cache(unique_id)
            layer_cache.on_upstream_change(upstream_input_mask)
            layer_cache.last_editor_target = editor_target

        # Route clipspace edits to appropriate cache based on editor_target
        # This determines which layer(s) the MaskEditor affects
        editor_mask = None
        input_override = None

        if editor_target == "mask_editor":
            # MaskEditor edits go to orange layer only
            editor_mask = clipspace_mask
            # Get any existing input override from cache (preserve it)
            input_override = get_input_override_cache(unique_id)
        elif editor_target == "input_mask":
            # MaskEditor edits override the input (red) layer only
            input_override = clipspace_mask
            # Get any existing editor mask from cache (preserve it)
            editor_mask = get_editor_mask_cache(unique_id)
        elif editor_target == "combined":
            # MaskEditor edits affect both layers
            editor_mask = clipspace_mask
            input_override = clipspace_mask

        editor_mask_valid = not is_mask_empty(editor_mask)
        input_override_valid = not is_mask_empty(input_override)

        # Effective input mask = upstream OR input_override (if override exists, it takes precedence)
        if input_override_valid:
            input_mask = input_override
        else:
            input_mask = upstream_input_mask
        input_mask_valid = not is_mask_empty(input_mask)

        # Get editor cache for 9-combination matrix (preserved editor when editing input layer)
        # This is separate from editor_mask which varies based on editor_target
        editor_cache = get_editor_mask_cache(unique_id)

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

        # DIAGNOSTIC: Log final mask determination
        logging.info(f"[PreviewBridgeExtended] process() MASK INPUTS: "
                     f"upstream_valid={upstream_input_valid}, editor_valid={editor_mask_valid}, "
                     f"input_override_valid={input_override_valid}, editor_cache_valid={editor_cache is not None}")
        if final_mask is not None:
            logging.info(f"[PreviewBridgeExtended] process() FINAL MASK: shape={final_mask.shape}, "
                         f"sum={final_mask.sum().item():.2f}")
        else:
            logging.info(f"[PreviewBridgeExtended] process() FINAL MASK: None")

        # LAYERCACHE VALIDATION: Compare with legacy output
        layer_cache_mask = get_output_mask(unique_id, mask_output)
        if layer_cache_mask is not None and final_mask is not None:
            lc_sum = layer_cache_mask.sum().item()
            legacy_sum = final_mask.sum().item()
            diff = abs(lc_sum - legacy_sum)
            if diff > 1.0:  # Threshold for significant difference
                logging.warning(f"[PreviewBridgeExtended] LayerCache/Legacy MISMATCH: "
                               f"LayerCache={lc_sum:.2f}, Legacy={legacy_sum:.2f}, diff={diff:.2f}")
            else:
                logging.info(f"[PreviewBridgeExtended] LayerCache/Legacy MATCH: "
                            f"LayerCache={lc_sum:.2f}, Legacy={legacy_sum:.2f}")

        # Create empty mask if none available
        if final_mask is None:
            final_mask = torch.zeros((1, height, width), dtype=torch.float32)

        # Ensure mask has batch dimension
        if len(final_mask.shape) == 2:
            final_mask = final_mask.unsqueeze(0)

        # Check if mask is empty for blocking decision
        is_empty = is_mask_empty(final_mask)

        # Generate info string
        info = generate_info(
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
        set_context_cache(unique_id, {
            'images': images,
            'upstream_input_mask': upstream_input_mask,
            'original_input_mask': get_original_input_cache(unique_id),  # Immutable reference
            'input_override': input_override,
            'editor_mask': editor_mask,
            'mask_output': mask_output,
            'editor_target': editor_target,
            'layer_cache': layer_cache,  # LayerCache for unified state management
        })

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
                preview_input_mask = combine_masks_and(upstream_input_mask, input_override, target_size)
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
                set_previewbridge_image(unique_id, preview_path, preview_result[0])
                # Also register under the clipspace path if present
                if image:
                    _preview_bridge_image_id_map[image] = (preview_path, preview_result[0])
                    _preview_bridge_image_name_map[(unique_id, preview_path)] = (image, preview_result[0])

        # Update caches
        set_cache(unique_id, images, preview_result)

        # Cache the combined final mask
        if not is_empty:
            set_last_mask_cache(unique_id, final_mask)
        else:
            # Final mask is empty - clear from cache
            delete_last_mask_cache(unique_id)

        # Cache masks separately based on editor_target
        # This ensures the correct layer is editable on next MaskEditor open
        if editor_target == "mask_editor" or editor_target == "combined":
            if editor_mask_valid:
                set_editor_mask_cache(unique_id, editor_mask)
            else:
                delete_editor_mask_cache(unique_id)

        if editor_target == "input_mask" or editor_target == "combined":
            if input_override_valid:
                set_input_override_cache(unique_id, input_override)
            else:
                delete_input_override_cache(unique_id)

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

        # No clipspace available - check if we should restore from cache
        if restore_mask == "never":
            return None

        if images_changed and restore_mask not in ["always", "if_same_size"]:
            return None

        # Try cache fallback - check both editor and input_override caches
        # Return the first valid one found (editor mask takes precedence)
        for cache_getter in [get_editor_mask_cache, get_input_override_cache]:
            mask = cache_getter(unique_id)
            if mask is not None and not is_mask_empty(mask):
                mask_height = mask.shape[1] if len(mask.shape) == 3 else mask.shape[0]
                mask_width = mask.shape[2] if len(mask.shape) == 3 else mask.shape[1]

                if restore_mask == "if_same_size":
                    if mask_height == target_height and mask_width == target_width:
                        return mask
                elif restore_mask == "always":
                    if mask_height != target_height or mask_width != target_width:
                        return resize_mask(mask, target_size)
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
        | input_mask   | mask_editor   | input_override AND upstream (intersection) |
        | input_mask   | input_mask    | input_override (user's edits)   |
        | mask_editor  | combined      | additions (delta from upstream) |
        | mask_editor  | mask_editor   | additions (delta from upstream) |
        | mask_editor  | input_mask    | additions from editor_cache (delta) |

        NOTE: For all mask_editor outputs, we compute delta (additions only) because
        the clipspace file may contain combined state from a previous mode session.

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
        editor_cache_valid = editor_cache is not None and not is_mask_empty(editor_cache)

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
                return combine_masks_or(upstream_mask, editor_mask, target_size)

            elif editor_target == "input_mask":
                # User editing input layer: OR combine input_override + cached editor
                return combine_masks_or(input_override, editor_cache, target_size)

        # =========================================
        # mask_output = "input_mask"
        # =========================================
        elif mask_output == "input_mask":
            if editor_target == "combined":
                # User edited both layers merged, but wants only "input" portion
                # Return intersection: what's in BOTH clipspace AND upstream
                # This preserves user's subtractions from upstream while ignoring additions
                if editor_mask_valid and upstream_mask_valid:
                    intersection = combine_masks_and(editor_mask, upstream_mask, target_size)
                    # Check if intersection is empty/near-empty - this can happen when
                    # clipspace contains additions-only data from a previous mask_editor mode
                    # (additions are areas NOT in upstream, so AND gives ~0)
                    if intersection is None or is_mask_empty(intersection):
                        editor_sum = editor_mask.sum().item()
                        upstream_sum = upstream_mask.sum().item()
                        logging.info(f"[PreviewBridgeExtended] input_mask/combined: intersection empty, "
                                    f"editor_sum={editor_sum:.2f}, upstream_sum={upstream_sum:.2f}")
                        # Fall back to upstream if intersection is empty but upstream is valid
                        # This handles stale clipspace from different mode
                        return upstream_mask
                    intersection_sum = intersection.sum().item()
                    upstream_sum = upstream_mask.sum().item()
                    logging.info(f"[PreviewBridgeExtended] input_mask/combined: intersection computed, "
                                f"intersection_sum={intersection_sum:.2f}, upstream_sum={upstream_sum:.2f}")
                    return intersection
                # If no edits, return upstream; if no upstream, return None
                return upstream_mask if upstream_mask_valid else None

            elif editor_target == "mask_editor":
                # User editing editor layer only
                # Input layer output = intersection of upstream and any previous combined edits
                # This handles the case where input_override contains combined state
                if input_override_valid and upstream_mask_valid:
                    # input_override may contain combined state from previous combined mode edits
                    # Compute intersection to get just the input layer portion (upstream - subtractions)
                    intersection = combine_masks_and(upstream_mask, input_override, target_size)
                    if intersection is not None and not is_mask_empty(intersection):
                        logging.info(f"[PreviewBridgeExtended] input_mask/mask_editor: intersection computed, "
                                    f"sum={intersection.sum().item():.2f}")
                        return intersection
                # No previous modifications or empty intersection, return immutable upstream
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
                _, additions, _ = compute_mask_delta(upstream_mask, editor_mask, target_size)
                return additions

            elif editor_target == "mask_editor":
                # User editing editor layer: return their editor mask
                # BUT: editor_mask may contain combined state from previous mode
                # (clipspace file persists across mode changes), so compute delta
                if not editor_mask_valid:
                    return None
                if not upstream_mask_valid:
                    # No upstream to delta against, return full editor_mask
                    logging.info(f"[PreviewBridgeExtended] mask_editor/mask_editor: no upstream, returning full editor_mask")
                    return editor_mask
                # Compute additions: areas in editor_mask but NOT in upstream
                # This extracts only the true editor additions
                _, additions, _ = compute_mask_delta(upstream_mask, editor_mask, target_size)
                editor_sum = editor_mask.sum().item() if editor_mask is not None else 0
                additions_sum = additions.sum().item() if additions is not None else 0
                logging.info(f"[PreviewBridgeExtended] mask_editor/mask_editor: computed delta, "
                            f"editor_sum={editor_sum:.2f}, additions_sum={additions_sum:.2f}")
                return additions

            elif editor_target == "input_mask":
                # User editing input layer: return preserved editor from cache
                # BUT: if cache contains combined state from previous mode, we need
                # to compute delta to extract only the editor additions
                if not editor_cache_valid:
                    logging.info(f"[PreviewBridgeExtended] mask_editor/input_mask: no editor_cache, returning None")
                    return None
                if not upstream_mask_valid:
                    # No upstream to delta against, return full cache
                    logging.info(f"[PreviewBridgeExtended] mask_editor/input_mask: no upstream, returning full cache")
                    return editor_cache
                # Compute additions: areas in editor_cache but NOT in upstream
                # This handles the case where editor_cache contains combined state
                _, additions, _ = compute_mask_delta(upstream_mask, editor_cache, target_size)
                cache_sum = editor_cache.sum().item() if editor_cache is not None else 0
                additions_sum = additions.sum().item() if additions is not None else 0
                logging.info(f"[PreviewBridgeExtended] mask_editor/input_mask: computed delta, "
                            f"cache_sum={cache_sum:.2f}, additions_sum={additions_sum:.2f}")
                return additions

        return None


# Node registration
NODE_CLASS_MAPPINGS = {
    "PreviewBridgeExtended": PreviewBridgeExtended,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "PreviewBridgeExtended": "Preview Bridge Extended (DazzleNodes)",
}
