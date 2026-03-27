"""
ComfyUI Preview Bridge Extended - DazzleNodes Custom Node
Enhanced Preview Bridge with optional mask input support.

Part of the DazzleNodes collection - standalone ComfyUI custom nodes.
"""

import logging
import os
import sys

# Configure module logger
_logger = logging.getLogger("PreviewBridgeExtended")

# Enable debug logging via environment variable: PBE_DEBUG=1
if os.environ.get('PBE_DEBUG', '').lower() in ('1', 'true', 'yes'):
    _logger.setLevel(logging.DEBUG)
    # Ensure debug messages are visible even if root logger level is higher
    if not _logger.handlers:
        _handler = logging.StreamHandler()
        _handler.setFormatter(logging.Formatter('[%(name)s] %(levelname)s: %(message)s'))
        _logger.addHandler(_handler)

# =====================================================
# DUAL-LOADING DETECTION
# Prevents split-brain cache bugs when PBE is installed
# both as a standalone node AND inside DazzleNodes.
# Uses a sys-level sentinel (shared across all module
# namespaces) to detect the second load.
# =====================================================
_PBE_SENTINEL = '_preview_bridge_extended_loaded'
_is_duplicate_load = hasattr(sys, _PBE_SENTINEL)

if _is_duplicate_load:
    _first_path = getattr(sys, _PBE_SENTINEL)
    _this_path = os.path.dirname(os.path.abspath(__file__))
    print(f"[PreviewBridgeExtended] WARNING: Duplicate installation detected!")
    print(f"[PreviewBridgeExtended]   Already loaded from: {_first_path}")
    print(f"[PreviewBridgeExtended]   Skipping this copy:  {_this_path}")
    print(f"[PreviewBridgeExtended]   Having two copies causes mask cache bugs.")
    print(f"[PreviewBridgeExtended]   Fix: Remove one installation (standalone symlink or DazzleNodes submodule).")
else:
    setattr(sys, _PBE_SENTINEL, os.path.dirname(os.path.abspath(__file__)))

from .py import NODE_CLASS_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS, generate_preview_for_api, get_preview_for_api, prepare_for_editing
from .version import __version__

# Tell ComfyUI where to find our JavaScript files
# Disabled on duplicate loads to prevent double JS extension registration
WEB_DIRECTORY = None if _is_duplicate_load else "./web"

__all__ = ['NODE_CLASS_MAPPINGS', 'NODE_DISPLAY_NAME_MAPPINGS', 'WEB_DIRECTORY']

# Register API endpoints for JS-Python communication (preview refresh, MaskEditor prep)
# Only register on first load - duplicate routes cause the API to target the wrong module's caches
if not _is_duplicate_load:
    try:
        import server
        from aiohttp import web

        @server.PromptServer.instance.routes.post("/preview-bridge-extended/refresh-preview")
        async def refresh_preview(request):
            """
            API endpoint to refresh the colored preview after MaskEditor save.

            POST body: {
                "node_id": "123",
                "clipspace_path": "clipspace/file.png [input]",
                "mask_output": "combined",  # optional - current widget value
                "editor_target": "combined"  # optional - current widget value
            }
            Returns: {"success": true, "image_data": "data:image/png;base64,..."}
            """
            try:
                data = await request.json()
                node_id = data.get('node_id')
                clipspace_path = data.get('clipspace_path')
                mask_output = data.get('mask_output')  # Optional override from JS
                editor_target = data.get('editor_target')  # Optional override from JS

                if not node_id:
                    return web.json_response({
                        'success': False,
                        'error': 'No node_id provided'
                    }, status=400)

                if not clipspace_path:
                    return web.json_response({
                        'success': False,
                        'error': 'No clipspace_path provided'
                    }, status=400)

                # Generate the colored preview
                result = generate_preview_for_api(
                    str(node_id), clipspace_path,
                    mask_output_override=mask_output,
                    editor_target_override=editor_target
                )

                if result.get('success'):
                    return web.json_response(result)
                else:
                    return web.json_response(result, status=400)

            except Exception as e:
                return web.json_response({
                    'success': False,
                    'error': str(e)
                }, status=500)

        _logger.debug("[PreviewBridgeExtended] Registered API endpoint: /preview-bridge-extended/refresh-preview")

        @server.PromptServer.instance.routes.post("/preview-bridge-extended/prepare-for-edit")
        async def prepare_for_edit_api(request):
            """
            API endpoint to prepare image for MaskEditor with editable alpha.

            Called by JS before opening MaskEditor. Regenerates the image with
            the combined editable mask in the alpha channel based on editor_target.

            POST body: {"node_id": "123", "editor_target": "combined|mask_editor|input_mask"}
            Returns: {"success": true, "image_data": "data:image/png;base64,..."}
            """
            try:
                data = await request.json()
                node_id = data.get('node_id')
                editor_target = data.get('editor_target')  # Current widget value from JS

                if not node_id:
                    return web.json_response({
                        'success': False,
                        'error': 'No node_id provided'
                    }, status=400)

                # Generate the editable preview with current editor_target
                result = prepare_for_editing(str(node_id), editor_target_override=editor_target)

                if result.get('success'):
                    return web.json_response(result)
                else:
                    return web.json_response(result, status=400)

            except Exception as e:
                return web.json_response({
                    'success': False,
                    'error': str(e)
                }, status=500)

        _logger.debug("[PreviewBridgeExtended] Registered API endpoint: /preview-bridge-extended/prepare-for-edit")

        @server.PromptServer.instance.routes.post("/preview-bridge-extended/get-preview")
        async def get_preview_api(request):
            """
            API endpoint to get current preview from LayerCache state.

            Used by Cancel handler to restore correct preview after MaskEditor closes
            without saving. Unlike refresh-preview, this doesn't decompose a new clipspace.

            POST body: {
                "node_id": "123",
                "mask_output": "combined",  # optional - current widget value
                "editor_target": "combined"  # optional - current widget value
            }
            Returns: {"success": true, "image_data": "data:image/png;base64,..."}
            """
            try:
                data = await request.json()
                node_id = data.get('node_id')
                mask_output = data.get('mask_output')
                editor_target = data.get('editor_target')

                if not node_id:
                    return web.json_response({
                        'success': False,
                        'error': 'No node_id provided'
                    }, status=400)

                # Generate preview from existing LayerCache state
                result = get_preview_for_api(
                    str(node_id),
                    mask_output_override=mask_output,
                    editor_target_override=editor_target
                )

                if result.get('success'):
                    return web.json_response(result)
                else:
                    return web.json_response(result, status=400)

            except Exception as e:
                return web.json_response({
                    'success': False,
                    'error': str(e)
                }, status=500)

        _logger.debug("[PreviewBridgeExtended] Registered API endpoint: /preview-bridge-extended/get-preview")

    except Exception as e:
        _logger.warning(f"[PreviewBridgeExtended] Could not register API endpoints: {e}")

# Display version info on load
if _is_duplicate_load:
    print(f"[PreviewBridgeExtended] Duplicate skipped v{__version__} (API routes and JS disabled)")
else:
    print(f"[PreviewBridgeExtended] Loaded v{__version__}")
