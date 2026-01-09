/**
 * Preview Bridge Extended - MaskEditor Integration
 *
 * Handles:
 * - Intercepting "Open in MaskEditor" menu action
 * - Preparing editable image before MaskEditor opens
 * - Detecting MaskEditor close for cancel handling
 * - Processing MaskEditor save (imgs setter)
 */

import { prepareForEdit, refreshPreview, getPreview, uploadImage, dataUriToBlob } from './api.js';


/**
 * Get current widget values from node.
 *
 * @param {object} node - ComfyUI node
 * @returns {{maskOutput: string, editorTarget: string}}
 */
function getWidgetValues(node) {
    const maskOutputWidget = node.widgets?.find(w => w.name === 'mask_output');
    const editorTargetWidget = node.widgets?.find(w => w.name === 'editor_target');

    return {
        maskOutput: maskOutputWidget?.value || 'combined',
        editorTarget: editorTargetWidget?.value || 'combined'
    };
}


/**
 * Set up MaskEditor close detection for cancel handling.
 *
 * @param {object} node - ComfyUI node
 * @param {object} imageWidget - The 'image' widget
 * @param {object} app - ComfyUI app instance
 */
export function setupMaskEditorCloseDetection(node, imageWidget, app) {
    console.log("[PreviewBridgeExtended] Setting up MaskEditor close detection");

    // Use polling to detect when MaskEditor modal closes
    // Track initial state to detect when dialog appears and disappears
    let dialogWasOpen = false;

    const checkInterval = setInterval(() => {
        // Look for ComfyUI's mask editor dialog - check multiple selectors
        // ComfyUI's MaskEditor typically uses a modal with canvas
        const maskEditorDialog = document.querySelector('.comfy-modal-content canvas') ||
                                document.querySelector('[class*="mask-editor"]') ||
                                document.querySelector('.graphdialog canvas') ||
                                document.querySelector('.comfy-modal canvas') ||
                                document.querySelector('[role="dialog"] canvas');

        // Track when dialog first appears
        if (maskEditorDialog && !dialogWasOpen) {
            dialogWasOpen = true;
            console.log("[PreviewBridgeExtended] MaskEditor dialog detected");
        }

        // Only trigger close logic if dialog was previously open and now closed
        if (!maskEditorDialog && dialogWasOpen) {
            clearInterval(checkInterval);
            console.log("[PreviewBridgeExtended] MaskEditor closed, save detected:", node._pbeSaveDetected);

            // If save wasn't detected, restore correct state
            if (!node._pbeSaveDetected && node._pbeOriginalWidgetValue !== null) {
                console.log("[PreviewBridgeExtended] Cancel detected, restoring state");
                imageWidget.value = node._pbeOriginalWidgetValue;

                // Call Python API to get correct preview from LayerCache
                const { maskOutput, editorTarget } = getWidgetValues(node);
                getPreview(node.id.toString(), maskOutput, editorTarget).then(result => {
                    if (result.success && result.image_data) {
                        const previewImg = new Image();
                        previewImg.onload = () => {
                            node._imgs = [previewImg];
                            node.imageIndex = 0;
                            node.setDirtyCanvas(true, true);
                            if (app.graph) {
                                app.graph.setDirtyCanvas(true, true);
                            }
                            console.log("[PreviewBridgeExtended] Preview restored after Cancel");
                        };
                        previewImg.src = result.image_data;
                    } else {
                        console.warn("[PreviewBridgeExtended] Failed to get preview after Cancel:", result.error);
                        // Just trigger canvas refresh anyway
                        node.setDirtyCanvas(true, true);
                    }
                }).catch(e => {
                    console.warn("[PreviewBridgeExtended] Error getting preview after Cancel:", e);
                    node.setDirtyCanvas(true, true);
                });
            }

            // Clear saved state
            node._pbeOriginalWidgetValue = null;
            node._pbeOriginalImgs = null;
            node._pbeSaveDetected = false;
        }
    }, 200); // Check every 200ms

    // Safety timeout - stop checking after 10 minutes
    setTimeout(() => {
        clearInterval(checkInterval);
    }, 600000);
}


/**
 * Handle MaskEditor open by preparing editable image.
 *
 * @param {object} node - ComfyUI node
 * @param {object} imageWidget - The 'image' widget
 * @param {object} app - ComfyUI app instance
 * @param {function} originalCallback - Original MaskEditor callback
 */
export async function handleMaskEditorOpen(node, imageWidget, app, originalCallback) {
    console.log("[PreviewBridgeExtended] Intercepted MaskEditor open, preparing editable image...");

    // Save original state for cancel restoration
    node._pbeOriginalWidgetValue = imageWidget.value;
    node._pbeOriginalImgs = node._imgs ? [...node._imgs] : null;
    node._pbeSaveDetected = false;

    // Get current editor_target from widget (not stale cache)
    const { editorTarget } = getWidgetValues(node);
    console.log("[PreviewBridgeExtended] Current editor_target from widget:", editorTarget);

    // Call Python API to prepare image with editable alpha
    const result = await prepareForEdit(node.id.toString(), editorTarget);

    if (result.success && result.image_data) {
        console.log("[PreviewBridgeExtended] Got editable image, uploading to clipspace...");

        // Convert data URI to blob for upload
        const blob = dataUriToBlob(result.image_data);

        // Upload to ComfyUI as temp file (MaskEditor reads from file, not memory)
        const filename = `pbe-edit-${node.id}-${Date.now()}.png`;
        const uploadResult = await uploadImage(blob, filename, 'PreviewBridgeExt', 'temp');

        if (uploadResult.success) {
            console.log("[PreviewBridgeExtended] Uploaded editable image:", uploadResult);

            // Update widget value to point to new file
            const editPath = `PreviewBridgeExt/${uploadResult.name} [temp]`;
            imageWidget.value = editPath;

            // Also update node._imgs with the image for display
            const editableImg = new Image();
            editableImg.onload = () => {
                node._imgs = [editableImg];
                node.imageIndex = 0;
                node.setDirtyCanvas(true, true);
                console.log("[PreviewBridgeExtended] Node updated, opening MaskEditor...");

                // Set up observer to detect MaskEditor close for cancel handling
                setupMaskEditorCloseDetection(node, imageWidget, app);

                // Now call the original MaskEditor callback
                if (originalCallback) {
                    originalCallback();
                }
            };
            editableImg.onerror = () => {
                console.warn("[PreviewBridgeExtended] Failed to load editable image, opening MaskEditor anyway");
                setupMaskEditorCloseDetection(node, imageWidget, app);
                if (originalCallback) {
                    originalCallback();
                }
            };
            editableImg.src = result.image_data;
            return; // Don't call original yet, wait for image load
        } else {
            console.warn("[PreviewBridgeExtended] Upload failed:", uploadResult.error);
        }
    }

    // Fallback: just call original callback
    console.log("[PreviewBridgeExtended] Falling back to original MaskEditor behavior");
    setupMaskEditorCloseDetection(node, imageWidget, app);
    if (originalCallback) {
        originalCallback();
    }
}


/**
 * Handle MaskEditor save (data URI from MaskEditor).
 *
 * @param {object} node - ComfyUI node
 * @param {object} imageWidget - The 'image' widget
 * @param {object} app - ComfyUI app instance
 * @param {string} src - Image source (data URI)
 */
export async function handleMaskEditorSave(node, imageWidget, app, src) {
    console.log("[PreviewBridgeExtended] Data URI detected from MaskEditor save");

    // Mark that a save was detected
    node._pbeSaveDetected = true;

    // Convert data URI to blob and save to clipspace
    try {
        const blob = dataUriToBlob(src);

        // Upload to ComfyUI as clipspace file
        const filename = `clipspace-pbe-${node.id}-${Date.now()}.png`;
        const uploadResult = await uploadImage(blob, filename, 'clipspace', 'input');

        if (uploadResult.success) {
            const clipspacePath = `clipspace/${uploadResult.name} [input]`;
            console.log("[PreviewBridgeExtended] Saved to clipspace:", clipspacePath);

            // Update widget with clipspace path
            imageWidget.value = clipspacePath;

            // Get current widget values
            const { maskOutput, editorTarget } = getWidgetValues(node);

            // Call Python API to get colored preview
            const apiResult = await refreshPreview(
                node.id.toString(),
                clipspacePath,
                maskOutput,
                editorTarget
            );

            if (apiResult.success && apiResult.image_data) {
                // Update preview with colored version from Python
                const previewImg = new Image();
                previewImg.onload = () => {
                    node._imgs = [previewImg];
                    node.imageIndex = 0;
                    node.setDirtyCanvas(true, true);
                    if (app.graph) {
                        app.graph.setDirtyCanvas(true, true);
                    }
                    console.log("[PreviewBridgeExtended] Preview updated with colored overlay (API refresh)");
                };
                previewImg.onerror = (e) => {
                    console.error("[PreviewBridgeExtended] Failed to load preview image:", e);
                    // Fallback: show MaskEditor's version
                    loadFallbackImage(node, src);
                };
                previewImg.src = apiResult.image_data;
            } else {
                console.warn("[PreviewBridgeExtended] API returned error:", apiResult.error);
                // Fallback: show MaskEditor's version
                loadFallbackImage(node, src);
            }
        } else {
            console.warn("[PreviewBridgeExtended] Failed to upload to clipspace:", uploadResult.error);
        }
    } catch (e) {
        console.warn("[PreviewBridgeExtended] Error saving data URI to clipspace:", e);
    }
}


/**
 * Handle legacy clipspace URL (older ComfyUI versions).
 *
 * @param {object} node - ComfyUI node
 * @param {object} imageWidget - The 'image' widget
 * @param {object} app - ComfyUI app instance
 * @param {string} src - Image source URL
 */
export function handleLegacyClipspace(node, imageWidget, app, src) {
    try {
        // Parse the image source URL to extract clipspace path
        const sp = new URLSearchParams(src.split("?")[1]);
        let str = "";
        if (sp.get('subfolder')) {
            str += sp.get('subfolder') + '/';
        }
        str += `${sp.get("filename")} [${sp.get("type")}]`;

        console.log("[PreviewBridgeExtended] Updating widget to:", str);

        // Update widget with clipspace path
        imageWidget.value = str;

        // Refresh canvas display (no workflow re-run)
        node.setDirtyCanvas(true, true);
        if (app.graph) {
            app.graph.setDirtyCanvas(true, true);
        }
        console.log("[PreviewBridgeExtended] Preview updated (colored version on next run)");
    } catch (e) {
        console.warn("[PreviewBridgeExtended] Error parsing clipspace path:", e);
    }
}


/**
 * Load fallback image when API fails.
 *
 * @param {object} node - ComfyUI node
 * @param {string} src - Image source
 */
function loadFallbackImage(node, src) {
    const fallbackImg = new Image();
    fallbackImg.onload = () => {
        node._imgs = [fallbackImg];
        node.imageIndex = 0;
        node.setDirtyCanvas(true, true);
    };
    fallbackImg.src = src;
}
