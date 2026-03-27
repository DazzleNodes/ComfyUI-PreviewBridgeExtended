"""
Preview Bridge Extended - LATENT Node Class

ComfyUI node that accepts LATENT + VAE input, decodes for preview display,
and provides the same 3-layer mask editing as the IMAGE variant.
The original LATENT passes through unmodified.
"""

import os
import logging
import torch
from typing import Optional, Dict, Any
import folder_paths

# Use named logger so PBE_DEBUG environment variable works
logger = logging.getLogger("PreviewBridgeExtended")

from .mask_ops import is_mask_empty, resize_mask, compute_tensor_fingerprint
from .caches import (
    set_cache,
    get_original_input_cache,
    set_context_cache,
    set_previewbridge_image, _preview_bridge_image_id_map, _preview_bridge_image_name_map,
    get_latent_decode_cache, set_latent_decode_cache,
)
from .preview import save_preview_images
from .node_base import (
    process_masks, apply_dazzle_signal, should_block,
    MASK_OUTPUT_WIDGET, EDITOR_TARGET_WIDGET, RESTORE_MASK_WIDGET,
    BLOCK_WIDGET, DAZZLE_SIGNAL_WIDGET,
)


INJECT_NOISE_MASK_WIDGET = (
    ["no", "yes"],
    {
        "default": "no",
        "tooltip": (
            "Controls whether the edited mask is injected into the LATENT output as noise_mask.\n"
            "no: LATENT passes through unchanged. Use MASK output with SetLatentNoiseMask.\n"
            "yes: Edited mask is written into LATENT.noise_mask automatically."
        )
    }
)


class PreviewBridgeExtendedLatent:
    """
    Latent Preview Bridge with mask editing for inpainting workflows.

    Accepts LATENT + VAE, decodes for preview display, provides full
    3-layer mask editing (upstream/additions/subtractions), and passes
    the original LATENT through unmodified.

    If the LATENT contains a noise_mask (from inpainting), it is
    composited with external mask_opt as the upstream mask layer.
    """

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "latent": ("LATENT",),
                "vae": ("VAE",),
                "image": ("STRING", {"default": ""}),
            },
            "optional": {
                "mask_opt": ("MASK",),
                "mask_output": MASK_OUTPUT_WIDGET,
                "editor_target": EDITOR_TARGET_WIDGET,
                "restore_mask": RESTORE_MASK_WIDGET,
                "block": BLOCK_WIDGET,
                "inject_noise_mask": INJECT_NOISE_MASK_WIDGET,
                "dazzle_signal": DAZZLE_SIGNAL_WIDGET,
            },
            "hidden": {
                "unique_id": "UNIQUE_ID",
                "prompt": "PROMPT",
                "extra_pnginfo": "EXTRA_PNGINFO",
            }
        }

    RETURN_TYPES = ("LATENT", "MASK", "IMAGE", "STRING")
    RETURN_NAMES = ("latent", "mask", "preview", "info")
    FUNCTION = "process"
    OUTPUT_NODE = True
    CATEGORY = "DazzleNodes"
    DESCRIPTION = (
        "Latent Preview Bridge for inpainting workflows. "
        "Decodes LATENT for preview, provides mask editing, "
        "and passes original LATENT through unmodified. "
        "Extracts noise_mask from LATENT as upstream mask."
    )

    def __init__(self):
        self.output_dir = folder_paths.get_temp_directory()
        self.type = "temp"

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        import sys
        # Re-execute when Dazzle Command state changes (play/pause toggle).
        cmd_state = getattr(sys, '_dazzle_command_state', None)
        if cmd_state:
            state = cmd_state.get('state', '')
            return f"dazzle:{state}"
        return ""

    def _decode_latent(self, latent: Dict, vae, unique_id: str) -> torch.Tensor:
        """
        Decode latent to IMAGE for preview display, with caching.

        Only re-decodes when latent content actually changes.
        Supports all latent types (SD1.5, SDXL, Flux, WAN, etc.)
        transparently via ComfyUI's VAE.decode().
        """
        samples = latent["samples"]
        current_fp = compute_tensor_fingerprint(samples)

        # Check cache
        cached = get_latent_decode_cache(unique_id)
        if cached is not None and cached['fingerprint'] == current_fp:
            logger.debug(f"[PBE-Latent] Using cached decoded image (fingerprint={current_fp[:8]}...)")
            return cached['decoded_images']

        # Decode
        logger.debug(f"[PBE-Latent] Decoding latent: shape={samples.shape}, fingerprint={current_fp[:8]}...")
        decoded = vae.decode(samples)

        # Handle video/batched outputs
        if len(decoded.shape) == 5:
            decoded = decoded.reshape(-1, decoded.shape[-3], decoded.shape[-2], decoded.shape[-1])

        # Cache
        set_latent_decode_cache(unique_id, current_fp, decoded)
        logger.debug(f"[PBE-Latent] Decoded: shape={decoded.shape}")

        return decoded

    def _extract_noise_mask(
        self,
        latent: Dict,
        target_size: tuple,
        mask_opt: Optional[torch.Tensor]
    ) -> Optional[torch.Tensor]:
        """
        Extract noise_mask from LATENT dict and composite with external mask_opt.

        noise_mask is in latent space (1/8th or 1/16th resolution).
        Upscales to pixel space for mask editing, then composites with
        external mask_opt via OR (union).

        Args:
            latent: LATENT dict (may contain "noise_mask")
            target_size: (height, width) in pixel space
            mask_opt: Optional external mask from upstream

        Returns:
            Composited upstream mask, or None if neither source has content
        """
        noise_mask = latent.get("noise_mask")
        pixel_noise_mask = None

        if noise_mask is not None and not is_mask_empty(noise_mask):
            # noise_mask shape: [B, 1, H_latent, W_latent] -> [B, H_pixel, W_pixel]
            mask_2d = noise_mask.squeeze(1)  # Remove channel dim
            pixel_noise_mask = resize_mask(mask_2d, target_size)
            logger.debug(f"[PBE-Latent] Extracted noise_mask: latent_shape={noise_mask.shape}, "
                          f"pixel_shape={pixel_noise_mask.shape}, sum={pixel_noise_mask.sum().item():.2f}")

        # Composite: OR union of noise_mask and external mask_opt
        if pixel_noise_mask is not None and mask_opt is not None and not is_mask_empty(mask_opt):
            composited = torch.clamp(pixel_noise_mask + mask_opt, 0, 1)
            logger.debug(f"[PBE-Latent] Composited noise_mask + mask_opt: sum={composited.sum().item():.2f}")
            return composited
        elif pixel_noise_mask is not None:
            return pixel_noise_mask
        elif mask_opt is not None:
            return mask_opt
        else:
            return None

    def process(
        self,
        latent: Dict,
        vae,
        image: str = "",
        mask_opt: Optional[torch.Tensor] = None,
        mask_output: str = "combined",
        editor_target: str = "combined",
        restore_mask: str = "never",
        block: str = "never",
        inject_noise_mask: str = "no",
        dazzle_signal=None,
        unique_id: str = "",
        prompt=None,
        extra_pnginfo=None
    ) -> Dict[str, Any]:
        """Process latent with mask editing for inpainting workflows."""
        # Decode latent for preview (cached)
        decoded_images = self._decode_latent(latent, vae, unique_id)
        batch, height, width, channels = decoded_images.shape
        target_size = (height, width)

        # Log inputs
        logger.debug(f"[PBE-Latent] process() called: unique_id={unique_id}")
        logger.debug(f"[PBE-Latent] process() LATENT: samples_shape={latent['samples'].shape}, "
                      f"has_noise_mask={'noise_mask' in latent}")
        logger.debug(f"[PBE-Latent] process() DECODED: {width}x{height} (shape={decoded_images.shape})")
        logger.debug(f"[PBE-Latent] process() WIDGETS: mask_output='{mask_output}', "
                      f"editor_target='{editor_target}', restore_mask='{restore_mask}', "
                      f"block='{block}', inject_noise_mask='{inject_noise_mask}'")

        # Extract and composite upstream mask (noise_mask + external mask_opt)
        composited_mask_opt = self._extract_noise_mask(latent, target_size, mask_opt)

        # Run shared mask orchestration pipeline (uses decoded image for preview/caching)
        result = process_masks(
            unique_id=unique_id,
            images=decoded_images,
            image=image,
            mask_opt=composited_mask_opt,
            mask_output=mask_output,
            editor_target=editor_target,
            restore_mask=restore_mask,
            block=block,
        )

        # Cache context for API preview refresh
        logger.debug(f"[PBE-Latent] Setting context cache for key='{unique_id}'")
        set_context_cache(unique_id, {
            'images': decoded_images,
            'upstream_input_mask': result.upstream_input_mask,
            'original_input_mask': get_original_input_cache(unique_id),
            'mask_output': mask_output,
            'editor_target': editor_target,
            'layer_cache': result.layer_cache,
        })

        # Save preview images with mask overlays
        preview_result = save_preview_images(
            images=decoded_images,
            input_mask=result.preview_input_mask,
            editor_mask=result.preview_editor_mask,
            editor_target=editor_target,
            unique_id=unique_id,
            original_mask=get_original_input_cache(unique_id),
            prompt=prompt,
            extra_pnginfo=extra_pnginfo
        )

        # Register in preview bridge system
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

        # Update basic caches (stores decoded image for change detection)
        set_cache(unique_id, decoded_images, preview_result)

        # Apply DAZZLE_SIGNAL override
        block = apply_dazzle_signal(dazzle_signal, block, result.editor_has_content, result.is_empty)

        # Prepare LATENT output
        output_latent = latent
        if inject_noise_mask == "yes" and not result.is_empty:
            # Write edited mask back into LATENT dict as noise_mask
            # Resize from pixel space to latent space
            latent_h, latent_w = latent["samples"].shape[-2], latent["samples"].shape[-1]
            latent_mask = resize_mask(result.final_mask, (latent_h, latent_w))
            # SetLatentNoiseMask format: [B, 1, H, W]
            latent_mask = latent_mask.reshape((-1, 1, latent_h, latent_w))
            output_latent = latent.copy()
            output_latent["noise_mask"] = latent_mask
            logger.debug(f"[PBE-Latent] Injected noise_mask: shape={latent_mask.shape}")

        # Handle blocking
        if should_block(block, result.is_empty, result.editor_has_content):
            try:
                from comfy_execution.graph import ExecutionBlocker
                output = (ExecutionBlocker(None), ExecutionBlocker(None),
                          ExecutionBlocker(None), result.info)
            except ImportError:
                output = (output_latent, result.final_mask, decoded_images, result.info)
        else:
            output = (output_latent, result.final_mask, decoded_images, result.info)

        return {
            "ui": {"images": preview_result},
            "result": output,
        }


# Node registration
NODE_CLASS_MAPPINGS = {
    "PreviewBridgeExtendedLatent": PreviewBridgeExtendedLatent,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "PreviewBridgeExtendedLatent": "Preview Bridge Ext. Latent (DazzleNodes)",
}
