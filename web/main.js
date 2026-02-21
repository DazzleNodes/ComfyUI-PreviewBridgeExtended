/**
 * Preview Bridge Extended - Main Extension
 *
 * This is the main entry point for the JavaScript extension.
 * Handles:
 * - Extension registration with ComfyUI
 * - Node creation hooks
 * - Widget and image handling setup
 *
 * COMPATIBILITY NOTE:
 * Uses dynamic imports with auto-depth detection to work in both:
 * - Standalone mode: /extensions/ComfyUI-PreviewBridgeExtended/
 * - DazzleNodes mode: /extensions/DazzleNodes/ComfyUI-PreviewBridgeExtended/
 */

import {
    handleMaskEditorOpen,
    handleMaskEditorSave,
    handleLegacyClipspace,
    setupMaskEditorCloseDetection
} from './maskeditor.js';

import { getPreview } from './api.js';


// Dynamic import helper for standalone vs nested extension compatibility
async function importComfyCore() {
    const currentPath = import.meta.url;
    const urlParts = new URL(currentPath).pathname.split('/').filter(p => p);
    const depth = urlParts.length; // Each part requires one ../ to traverse up
    const prefix = '../'.repeat(depth);

    const appModule = await import(`${prefix}scripts/app.js`);
    return { app: appModule.app };
}


/**
 * Get current widget values from node.
 *
 * @param {object} node - ComfyUI node
 * @returns {{maskOutput: string, editorTarget: string, invertInputMask: boolean, invertOutputMask: boolean}}
 */
function getWidgetValues(node) {
    const maskOutputWidget = node.widgets?.find(w => w.name === 'mask_output');
    const editorTargetWidget = node.widgets?.find(w => w.name === 'editor_target');
    const invertInputMaskWidget = node.widgets?.find(w => w.name === 'invert_input_mask');
    const invertOutputMaskWidget = node.widgets?.find(w => w.name === 'invert_output_mask');

    return {
        maskOutput: maskOutputWidget?.value || 'combined',
        editorTarget: editorTargetWidget?.value || 'combined',
        invertInputMask: !!invertInputMaskWidget?.value,
        invertOutputMask: !!invertOutputMaskWidget?.value,
    };
}


/**
 * Refresh preview when display widgets change.
 *
 * Called when mask_output or editor_target widgets change value.
 * Fetches new preview from Python API with current LayerCache state.
 *
 * @param {object} node - ComfyUI node
 * @param {object} app - ComfyUI app instance
 */
async function refreshPreviewOnWidgetChange(node, app) {
    const { maskOutput, editorTarget, invertInputMask, invertOutputMask } = getWidgetValues(node);

    console.log(`[PreviewBridgeExtended] Widget changed, refreshing preview: mask_output=${maskOutput}, editor_target=${editorTarget}, invert_input_mask=${invertInputMask}, invert_output_mask=${invertOutputMask}`);

    try {
        const result = await getPreview(node.id.toString(), maskOutput, editorTarget, invertInputMask, invertOutputMask);

        if (result.success && result.image_data) {
            const previewImg = new Image();
            previewImg.onload = () => {
                node._imgs = [previewImg];
                node.imageIndex = 0;
                node.setDirtyCanvas(true, true);
                if (app.graph) {
                    app.graph.setDirtyCanvas(true, true);
                }
                console.log("[PreviewBridgeExtended] Preview refreshed after widget change");
            };
            previewImg.onerror = () => {
                console.warn("[PreviewBridgeExtended] Failed to load preview image after widget change");
            };
            previewImg.src = result.image_data;
        } else if (result.error) {
            // Don't warn on "no context cache" - just means workflow hasn't run yet
            if (!result.error.includes('No context cache')) {
                console.warn("[PreviewBridgeExtended] Failed to refresh preview:", result.error);
            }
        }
    } catch (e) {
        console.warn("[PreviewBridgeExtended] Error refreshing preview on widget change:", e);
    }
}


/**
 * Set up the PreviewBridgeExtended node.
 *
 * @param {object} node - ComfyUI node
 * @param {object} app - ComfyUI app instance
 */
function setupPreviewBridgeExtendedNode(node, app) {
    if (!node.widgets) {
        return;
    }

    // Find the 'image' widget that stores clipspace paths
    const imageWidget = node.widgets.find(obj => obj.name === 'image');
    if (!imageWidget) {
        return;
    }

    // Initialize image storage
    node._imgs = [new Image()];
    node.imageIndex = 0;

    // Set up widget change listeners for preview refresh
    // When mask_output or editor_target change, refresh preview from LayerCache
    const maskOutputWidget = node.widgets.find(w => w.name === 'mask_output');
    const editorTargetWidget = node.widgets.find(w => w.name === 'editor_target');
    const invertInputMaskWidget = node.widgets.find(w => w.name === 'invert_input_mask');
    const invertOutputMaskWidget = node.widgets.find(w => w.name === 'invert_output_mask');

    if (maskOutputWidget) {
        const origMaskOutputCallback = maskOutputWidget.callback;
        maskOutputWidget.callback = async (value) => {
            if (origMaskOutputCallback) {
                origMaskOutputCallback.call(maskOutputWidget, value);
            }
            await refreshPreviewOnWidgetChange(node, app);
        };
    }

    if (editorTargetWidget) {
        const origEditorTargetCallback = editorTargetWidget.callback;
        editorTargetWidget.callback = async (value) => {
            if (origEditorTargetCallback) {
                origEditorTargetCallback.call(editorTargetWidget, value);
            }
            await refreshPreviewOnWidgetChange(node, app);
        };
    }

    if (invertInputMaskWidget) {
        const origInvertInputCallback = invertInputMaskWidget.callback;
        invertInputMaskWidget.callback = async (value) => {
            if (origInvertInputCallback) {
                origInvertInputCallback.call(invertInputMaskWidget, value);
            }
            await refreshPreviewOnWidgetChange(node, app);
        };
    }

    if (invertOutputMaskWidget) {
        const origInvertOutputCallback = invertOutputMaskWidget.callback;
        invertOutputMaskWidget.callback = async (value) => {
            if (origInvertOutputCallback) {
                origInvertOutputCallback.call(invertOutputMaskWidget, value);
            }
            await refreshPreviewOnWidgetChange(node, app);
        };
    }

    // Hook into onExecuted to handle widget value updates
    // This replaces Object.defineProperty approach which fails on
    // ComfyUI v1.34+ where widget.value is non-configurable
    const origOnExecuted = node.onExecuted;
    node.onExecuted = async function(output) {
        // Check if we should preserve clipspace path (user-drawn mask)
        const outputImage = output?.images?.[0];
        const isNewTempFile = outputImage &&
                              outputImage.subfolder === 'PreviewBridgeExt' &&
                              outputImage.type === 'temp';
        const isClipspacePath = imageWidget.value &&
                                (imageWidget.value.includes('clipspace') ||
                                 imageWidget.value.includes('[input]'));

        // Preserve clipspace paths (user edits) - don't overwrite with temp file path
        // Only update widget if it's a new temp file and NOT a clipspace path we want to keep
        if (!isClipspacePath || isNewTempFile) {
            if (output && output.images && output.images.length > 0) {
                const img = output.images[0];
                // Build the path in ComfyUI format: "subfolder/filename [type]"
                let path = "";
                if (img.subfolder) {
                    path += img.subfolder + "/";
                }
                path += `${img.filename} [${img.type}]`;
                imageWidget.value = path;
            }
        }

        // Call original handler if present
        if (origOnExecuted) {
            origOnExecuted.call(this, output);
        }
    };

    // Hook into getExtraMenuOptions to intercept "Open in MaskEditor"
    // We need to prepare the image with editable alpha BEFORE MaskEditor opens
    const origGetExtraMenuOptions = node.getExtraMenuOptions;

    // Track state for cancel detection
    node._pbeOriginalWidgetValue = null;
    node._pbeOriginalImgs = null;
    node._pbeSaveDetected = false;

    node.getExtraMenuOptions = function(_, options) {
        // Call original first to get standard menu items
        if (origGetExtraMenuOptions) {
            origGetExtraMenuOptions.call(this, _, options);
        }

        // Find and wrap the "Open in MaskEditor" option
        // Check for various possible menu text formats
        for (let i = 0; i < options.length; i++) {
            const opt = options[i];
            if (opt && opt.content) {
                const content = opt.content.toLowerCase();
                // Match "Open in MaskEditor", "Open in Mask Editor", etc.
                if (content.includes("mask") && content.includes("editor")) {
                    console.log("[PreviewBridgeExtended] Found MaskEditor menu item:", opt.content);
                    const originalCallback = opt.callback;
                    opt.callback = async () => {
                        await handleMaskEditorOpen(node, imageWidget, app, originalCallback);
                    };
                }
            }
        }
    };

    // Handle clipspace paste operations - intercept 'imgs' property
    Object.defineProperty(node, 'imgs', {
        set(v) {
            // Don't set if empty
            if (v && v.length === 0) {
                return;
            }

            if (v && v.length > 0 && v[0].src) {
                const src = v[0].src;
                const isDataUri = src.startsWith('data:image/png;base64,');
                const isClipspace = src.includes('clipspace') || src.includes('type=input');

                console.log("[PreviewBridgeExtended] imgs setter called, isDataUri:", isDataUri, "isClipspace:", isClipspace);

                // Mark that a save was detected (MaskEditor is saving)
                // This prevents cancel restoration from overwriting the save
                if (isDataUri || isClipspace) {
                    node._pbeSaveDetected = true;
                    console.log("[PreviewBridgeExtended] Save detected, setting _pbeSaveDetected = true");
                }

                // Data URI = MaskEditor just saved (new ComfyUI behavior)
                if (isDataUri) {
                    handleMaskEditorSave(node, imageWidget, app, src);
                }
                // Legacy clipspace URL handling (older ComfyUI versions)
                else if (isClipspace) {
                    handleLegacyClipspace(node, imageWidget, app, src);
                }
            }

            node._imgs = v;
        },
        get() {
            return node._imgs;
        }
    });
}


// Initialize extension with dynamic imports
(async () => {
    const { app } = await importComfyCore();

    app.registerExtension({
        name: "DazzleNodes.PreviewBridgeExtended",

        nodeCreated(node, app) {
            if (node.comfyClass !== "PreviewBridgeExtended") {
                return;
            }

            setupPreviewBridgeExtendedNode(node, app);
        }
    });

    console.log("[PreviewBridgeExtended] JavaScript extension loaded (modular)");
})();
