/**
 * Preview Bridge Extended - JavaScript integration for MaskEditor/clipspace handling.
 *
 * This handles:
 * 1. Widget value updates after execution (preserving user-drawn masks)
 * 2. Clipspace paste operations from MaskEditor
 *
 * Based on Impact Pack's approach but simplified since our Python implementation
 * handles mask loading directly from clipspace files (PR #1172 fix).
 *
 * COMPATIBILITY NOTE:
 * Uses dynamic imports with auto-depth detection to work in both:
 * - Standalone mode: /extensions/ComfyUI-PreviewBridgeExtended/
 * - DazzleNodes mode: /extensions/DazzleNodes/ComfyUI-PreviewBridgeExtended/
 */

// Dynamic import helper for standalone vs nested extension compatibility
async function importComfyCore() {
    const currentPath = import.meta.url;
    const urlParts = new URL(currentPath).pathname.split('/').filter(p => p);
    const depth = urlParts.length; // Each part requires one ../ to traverse up
    const prefix = '../'.repeat(depth);

    const appModule = await import(`${prefix}scripts/app.js`);
    return { app: appModule.app };
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
                                console.log("[PreviewBridgeExtended] Intercepted MaskEditor open, preparing editable image...");

                                // Save original state for cancel restoration
                                node._pbeOriginalWidgetValue = imageWidget.value;
                                node._pbeOriginalImgs = node._imgs ? [...node._imgs] : null;
                                node._pbeSaveDetected = false;

                                // Call Python API to prepare image with editable alpha
                                try {
                                    // Get current editor_target from widget (not stale cache)
                                    const editorTargetWidget = node.widgets.find(w => w.name === 'editor_target');
                                    const currentEditorTarget = editorTargetWidget ? editorTargetWidget.value : 'combined';
                                    console.log("[PreviewBridgeExtended] Current editor_target from widget:", currentEditorTarget);

                                    const response = await fetch('/preview-bridge-extended/prepare-for-edit', {
                                        method: 'POST',
                                        headers: {
                                            'Content-Type': 'application/json',
                                        },
                                        body: JSON.stringify({
                                            node_id: node.id.toString(),
                                            editor_target: currentEditorTarget
                                        })
                                    });

                                    console.log("[PreviewBridgeExtended] API response status:", response.status);

                                    if (response.ok) {
                                        const result = await response.json();
                                        console.log("[PreviewBridgeExtended] API result:", {
                                            success: result.success,
                                            hasImageData: !!result.image_data,
                                            editor_target: result.editor_target
                                        });

                                        if (result.success && result.image_data) {
                                            console.log("[PreviewBridgeExtended] Got editable image, uploading to clipspace...");

                                            // Convert data URI to blob for upload
                                            const base64Data = result.image_data.split(',')[1];
                                            const byteCharacters = atob(base64Data);
                                            const byteNumbers = new Array(byteCharacters.length);
                                            for (let j = 0; j < byteCharacters.length; j++) {
                                                byteNumbers[j] = byteCharacters.charCodeAt(j);
                                            }
                                            const byteArray = new Uint8Array(byteNumbers);
                                            const blob = new Blob([byteArray], { type: 'image/png' });

                                            // Upload to ComfyUI as temp file (MaskEditor reads from file, not memory)
                                            const formData = new FormData();
                                            const filename = `pbe-edit-${node.id}-${Date.now()}.png`;
                                            formData.append('image', blob, filename);
                                            formData.append('subfolder', 'PreviewBridgeExt');
                                            formData.append('type', 'temp');

                                            const uploadResponse = await fetch('/api/upload/image', {
                                                method: 'POST',
                                                body: formData
                                            });

                                            if (uploadResponse.ok) {
                                                const uploadResult = await uploadResponse.json();
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
                                                    setupMaskEditorCloseDetection(node, imageWidget);

                                                    // Now call the original MaskEditor callback
                                                    if (originalCallback) {
                                                        originalCallback();
                                                    }
                                                };
                                                editableImg.onerror = () => {
                                                    console.warn("[PreviewBridgeExtended] Failed to load editable image, opening MaskEditor anyway");
                                                    if (originalCallback) {
                                                        originalCallback();
                                                    }
                                                };
                                                editableImg.src = result.image_data;
                                                return; // Don't call original yet, wait for image load
                                            } else {
                                                console.warn("[PreviewBridgeExtended] Upload failed:", uploadResponse.status);
                                            }
                                        }
                                    } else {
                                        const errorText = await response.text();
                                        console.warn("[PreviewBridgeExtended] API error response:", errorText);
                                    }
                                } catch (e) {
                                    console.warn("[PreviewBridgeExtended] prepare-for-edit API error:", e);
                                }

                                // Fallback: just call original callback
                                console.log("[PreviewBridgeExtended] Falling back to original MaskEditor behavior");
                                // Still set up close detection for cancel handling
                                setupMaskEditorCloseDetection(node, imageWidget);
                                if (originalCallback) {
                                    originalCallback();
                                }
                            };
                        }
                    }
                }
            };

            // Helper function to detect MaskEditor close and restore state on cancel
            function setupMaskEditorCloseDetection(node, imageWidget) {
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

                        // If save wasn't detected, restore original state
                        if (!node._pbeSaveDetected && node._pbeOriginalWidgetValue !== null) {
                            console.log("[PreviewBridgeExtended] Cancel detected, restoring original display state");
                            imageWidget.value = node._pbeOriginalWidgetValue;
                            if (node._pbeOriginalImgs) {
                                node._imgs = node._pbeOriginalImgs;
                            }
                            node.setDirtyCanvas(true, true);
                            if (app.graph) {
                                app.graph.setDirtyCanvas(true, true);
                            }
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
                        // Save to clipspace for Python, but DON'T re-queue whole workflow
                        if (isDataUri) {
                            console.log("[PreviewBridgeExtended] Data URI detected from MaskEditor save");

                            // Convert data URI to blob and save to clipspace
                            (async () => {
                                try {
                                    // Decode base64 to blob
                                    const base64Data = src.split(',')[1];
                                    const byteCharacters = atob(base64Data);
                                    const byteNumbers = new Array(byteCharacters.length);
                                    for (let i = 0; i < byteCharacters.length; i++) {
                                        byteNumbers[i] = byteCharacters.charCodeAt(i);
                                    }
                                    const byteArray = new Uint8Array(byteNumbers);
                                    const blob = new Blob([byteArray], { type: 'image/png' });

                                    // Upload to ComfyUI as clipspace file
                                    const formData = new FormData();
                                    const filename = `clipspace-pbe-${node.id}-${Date.now()}.png`;
                                    formData.append('image', blob, filename);
                                    formData.append('subfolder', 'clipspace');
                                    formData.append('type', 'input');

                                    const response = await fetch('/api/upload/image', {
                                        method: 'POST',
                                        body: formData
                                    });

                                    if (response.ok) {
                                        const result = await response.json();
                                        const clipspacePath = `clipspace/${result.name} [input]`;
                                        console.log("[PreviewBridgeExtended] Saved to clipspace:", clipspacePath);

                                        // Update widget with clipspace path
                                        imageWidget.value = clipspacePath;

                                        // Call Python API to get colored preview (no workflow re-run!)
                                        // This generates the red/orange tinted preview server-side
                                        try {
                                            // Get current widget values (not stale cache)
                                            const maskOutputWidget = node.widgets.find(w => w.name === 'mask_output');
                                            const editorTargetWidget = node.widgets.find(w => w.name === 'editor_target');
                                            const currentMaskOutput = maskOutputWidget ? maskOutputWidget.value : 'combined';
                                            const currentEditorTarget = editorTargetWidget ? editorTargetWidget.value : 'combined';

                                            const apiResponse = await fetch('/preview-bridge-extended/refresh-preview', {
                                                method: 'POST',
                                                headers: {
                                                    'Content-Type': 'application/json',
                                                },
                                                body: JSON.stringify({
                                                    node_id: node.id.toString(),
                                                    clipspace_path: clipspacePath,
                                                    mask_output: currentMaskOutput,
                                                    editor_target: currentEditorTarget
                                                })
                                            });

                                            if (apiResponse.ok) {
                                                const apiResult = await apiResponse.json();
                                                console.log("[PreviewBridgeExtended] API response:", {
                                                    success: apiResult.success,
                                                    hasImageData: !!apiResult.image_data,
                                                    imageDataLength: apiResult.image_data ? apiResult.image_data.length : 0,
                                                    error: apiResult.error
                                                });
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
                                                        const fallbackImg = new Image();
                                                        fallbackImg.onload = () => {
                                                            node._imgs = [fallbackImg];
                                                            node.imageIndex = 0;
                                                            node.setDirtyCanvas(true, true);
                                                        };
                                                        fallbackImg.src = src;
                                                    };
                                                    previewImg.src = apiResult.image_data;
                                                } else {
                                                    console.warn("[PreviewBridgeExtended] API returned error:", apiResult.error);
                                                    // Fallback: show MaskEditor's version
                                                    const fallbackImg = new Image();
                                                    fallbackImg.onload = () => {
                                                        node._imgs = [fallbackImg];
                                                        node.imageIndex = 0;
                                                        node.setDirtyCanvas(true, true);
                                                    };
                                                    fallbackImg.src = src;
                                                }
                                            } else {
                                                console.warn("[PreviewBridgeExtended] API request failed:", apiResponse.status);
                                                // Fallback: show MaskEditor's version
                                                const fallbackImg = new Image();
                                                fallbackImg.onload = () => {
                                                    node._imgs = [fallbackImg];
                                                    node.imageIndex = 0;
                                                    node.setDirtyCanvas(true, true);
                                                };
                                                fallbackImg.src = src;
                                            }
                                        } catch (apiError) {
                                            console.warn("[PreviewBridgeExtended] API call error:", apiError);
                                            // Fallback: show MaskEditor's version
                                            const fallbackImg = new Image();
                                            fallbackImg.onload = () => {
                                                node._imgs = [fallbackImg];
                                                node.imageIndex = 0;
                                                node.setDirtyCanvas(true, true);
                                            };
                                            fallbackImg.src = src;
                                        }
                                    } else {
                                        console.warn("[PreviewBridgeExtended] Failed to upload to clipspace:", response.status);
                                    }
                                } catch (e) {
                                    console.warn("[PreviewBridgeExtended] Error saving data URI to clipspace:", e);
                                }
                            })();
                        }
                        // Legacy clipspace URL handling (older ComfyUI versions)
                        else if (isClipspace) {
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
                    }

                    node._imgs = v;
                },
                get() {
                    return node._imgs;
                }
            });
        }
    });

    console.log("[PreviewBridgeExtended] JavaScript extension loaded");
})();
