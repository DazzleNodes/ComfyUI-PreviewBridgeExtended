/**
 * Preview Bridge Extended - API Client
 *
 * Handles communication with Python backend for:
 * - Preparing images for MaskEditor (prepare-for-edit)
 * - Refreshing colored preview after save (refresh-preview)
 * - Uploading images to clipspace
 */

/**
 * Prepare image for MaskEditor by getting editable version from Python.
 *
 * @param {string} nodeId - Node unique ID
 * @param {string} editorTarget - Current editor_target widget value
 * @returns {Promise<{success: boolean, image_data?: string, error?: string}>}
 */
export async function prepareForEdit(nodeId, editorTarget) {
    console.log("[PreviewBridgeExtended API] prepareForEdit called:", { nodeId, editorTarget });

    try {
        const response = await fetch('/preview-bridge-extended/prepare-for-edit', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify({
                node_id: nodeId.toString(),
                editor_target: editorTarget
            })
        });

        console.log("[PreviewBridgeExtended API] prepareForEdit response status:", response.status);

        if (response.ok) {
            const result = await response.json();
            console.log("[PreviewBridgeExtended API] prepareForEdit result:", {
                success: result.success,
                hasImageData: !!result.image_data,
                editor_target: result.editor_target
            });
            return result;
        } else {
            const errorText = await response.text();
            console.warn("[PreviewBridgeExtended API] prepareForEdit error:", errorText);
            return { success: false, error: errorText };
        }
    } catch (e) {
        console.warn("[PreviewBridgeExtended API] prepareForEdit exception:", e);
        return { success: false, error: e.message };
    }
}


/**
 * Refresh colored preview after MaskEditor save.
 *
 * @param {string} nodeId - Node unique ID
 * @param {string} clipspacePath - Path to clipspace file
 * @param {string} maskOutput - Current mask_output widget value
 * @param {string} editorTarget - Current editor_target widget value
 * @returns {Promise<{success: boolean, image_data?: string, error?: string}>}
 */
export async function refreshPreview(nodeId, clipspacePath, maskOutput, editorTarget) {
    console.log("[PreviewBridgeExtended API] refreshPreview called:", {
        nodeId, clipspacePath, maskOutput, editorTarget
    });

    try {
        const response = await fetch('/preview-bridge-extended/refresh-preview', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify({
                node_id: nodeId.toString(),
                clipspace_path: clipspacePath,
                mask_output: maskOutput,
                editor_target: editorTarget
            })
        });

        if (response.ok) {
            const result = await response.json();
            console.log("[PreviewBridgeExtended API] refreshPreview result:", {
                success: result.success,
                hasImageData: !!result.image_data,
                imageDataLength: result.image_data ? result.image_data.length : 0,
                error: result.error
            });
            return result;
        } else {
            console.warn("[PreviewBridgeExtended API] refreshPreview failed:", response.status);
            return { success: false, error: `HTTP ${response.status}` };
        }
    } catch (e) {
        console.warn("[PreviewBridgeExtended API] refreshPreview exception:", e);
        return { success: false, error: e.message };
    }
}


/**
 * Upload image to ComfyUI server.
 *
 * @param {Blob} blob - Image blob to upload
 * @param {string} filename - Filename for the image
 * @param {string} subfolder - Subfolder to upload to
 * @param {string} type - File type ('temp' or 'input')
 * @returns {Promise<{success: boolean, name?: string, error?: string}>}
 */
export async function uploadImage(blob, filename, subfolder, type) {
    try {
        const formData = new FormData();
        formData.append('image', blob, filename);
        formData.append('subfolder', subfolder);
        formData.append('type', type);

        const response = await fetch('/api/upload/image', {
            method: 'POST',
            body: formData
        });

        if (response.ok) {
            const result = await response.json();
            console.log("[PreviewBridgeExtended API] uploadImage result:", result);
            return { success: true, ...result };
        } else {
            console.warn("[PreviewBridgeExtended API] uploadImage failed:", response.status);
            return { success: false, error: `HTTP ${response.status}` };
        }
    } catch (e) {
        console.warn("[PreviewBridgeExtended API] uploadImage exception:", e);
        return { success: false, error: e.message };
    }
}


/**
 * Convert base64 data URI to Blob.
 *
 * @param {string} dataUri - Data URI string (data:image/png;base64,...)
 * @returns {Blob}
 */
export function dataUriToBlob(dataUri) {
    const base64Data = dataUri.split(',')[1];
    const byteCharacters = atob(base64Data);
    const byteNumbers = new Array(byteCharacters.length);
    for (let i = 0; i < byteCharacters.length; i++) {
        byteNumbers[i] = byteCharacters.charCodeAt(i);
    }
    const byteArray = new Uint8Array(byteNumbers);
    return new Blob([byteArray], { type: 'image/png' });
}
