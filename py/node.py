"""
Preview Bridge Extended - IMAGE Node Class

ComfyUI node that accepts IMAGE input with optional MASK, providing
MaskEditor integration and 3-layer mask editing (upstream/additions/subtractions).
"""

import os
import logging
import torch
from typing import Optional, Dict, Any
import folder_paths

# Use named logger so PBE_DEBUG environment variable works
logger = logging.getLogger("PreviewBridgeExtended")

from .caches import (
    set_cache,
    get_original_input_cache,
    set_context_cache,
    set_previewbridge_image, _preview_bridge_image_id_map, _preview_bridge_image_name_map
)
from .preview import save_preview_images
from .node_base import (
    process_masks, apply_dazzle_signal, should_block,
    MASK_OUTPUT_WIDGET, EDITOR_TARGET_WIDGET, RESTORE_MASK_WIDGET,
    BLOCK_WIDGET, DAZZLE_SIGNAL_WIDGET,
)


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
                "images": ("IMAGE",),
                "image": ("STRING", {"default": ""}),
            },
            "optional": {
                "mask_opt": ("MASK",),
                "mask_output": MASK_OUTPUT_WIDGET,
                "editor_target": EDITOR_TARGET_WIDGET,
                "restore_mask": RESTORE_MASK_WIDGET,
                "block": BLOCK_WIDGET,
                "dazzle_signal": DAZZLE_SIGNAL_WIDGET,
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

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        # Only check DazzleCommand state if dazzle_signal noodle is connected.
        # Standalone PBE nodes are not affected (#56).
        # Reads per-node active_state from signal (#5).
        dazzle_signal = kwargs.get('dazzle_signal')
        if dazzle_signal is not None and isinstance(dazzle_signal, dict):
            state = dazzle_signal.get('active_state', '')
            return f"dazzle:{state}"
        return ""

    def __init__(self):
        self.output_dir = folder_paths.get_temp_directory()
        self.type = "temp"

    def process(
        self,
        images: torch.Tensor,
        image: str = "",
        mask_opt: Optional[torch.Tensor] = None,
        mask_output: str = "combined",
        editor_target: str = "combined",
        restore_mask: str = "never",
        block: str = "never",
        dazzle_signal=None,
        unique_id: str = "",
        prompt=None,
        extra_pnginfo=None
    ) -> Dict[str, Any]:
        """Process images with optional mask input and MaskEditor integration."""
        # Log inputs
        batch, height, width, channels = images.shape
        logger.debug(f"[PreviewBridgeExtended] process() called: unique_id={unique_id}")
        logger.debug(f"[PreviewBridgeExtended] process() IMAGE: {width}x{height} (shape={images.shape})")
        logger.debug(f"[PreviewBridgeExtended] process() WIDGETS: mask_output='{mask_output}', "
                      f"editor_target='{editor_target}', restore_mask='{restore_mask}', block='{block}'")
        logger.debug(f"[PreviewBridgeExtended] process() mask_opt: "
                      f"{'None' if mask_opt is None else f'shape={mask_opt.shape}, sum={mask_opt.sum().item():.2f}'}")
        logger.debug(f"[PreviewBridgeExtended] process() image widget: '{image}'")

        # Run shared mask orchestration pipeline
        result = process_masks(
            unique_id=unique_id,
            images=images,
            image=image,
            mask_opt=mask_opt,
            mask_output=mask_output,
            editor_target=editor_target,
            restore_mask=restore_mask,
            block=block,
        )

        # Cache context for API preview refresh (JS-Python communication)
        logger.debug(f"[PreviewBridgeExtended] Setting context cache for key='{unique_id}' (type={type(unique_id).__name__})")
        set_context_cache(unique_id, {
            'images': images,
            'upstream_input_mask': result.upstream_input_mask,
            'original_input_mask': get_original_input_cache(unique_id),
            'mask_output': mask_output,
            'editor_target': editor_target,
            'layer_cache': result.layer_cache,
        })

        # Save preview images with mask overlays
        preview_result = save_preview_images(
            images=images,
            input_mask=result.preview_input_mask,
            editor_mask=result.preview_editor_mask,
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

        # Apply DAZZLE_SIGNAL override
        block = apply_dazzle_signal(dazzle_signal, block, result.editor_has_content, result.is_empty)

        # Handle blocking
        if should_block(block, result.is_empty, result.editor_has_content):
            try:
                from comfy_execution.graph import ExecutionBlocker
                output = (ExecutionBlocker(None), ExecutionBlocker(None), result.info)
            except ImportError:
                output = (images, result.final_mask, result.info)
        else:
            output = (images, result.final_mask, result.info)

        return {
            "ui": {"images": preview_result},
            "result": output,
        }


# Node registration
NODE_CLASS_MAPPINGS = {
    "PreviewBridgeExtended": PreviewBridgeExtended,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "PreviewBridgeExtended": "Preview Bridge Extended (DazzleNodes)",
}
