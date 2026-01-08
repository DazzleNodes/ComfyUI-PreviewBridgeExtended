# ComfyUI Preview Bridge Extended

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![ComfyUI Registry](https://img.shields.io/badge/ComfyUI-Registry-green.svg)](https://registry.comfy.org/publishers/djdarcy/nodes/DazzleNodes)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

Enhanced Preview Bridge node with optional mask input support. Extends the Preview Bridge concept with configurable mask source selection and proper empty mask detection.

## Overview

ComfyUI's IMAGE type is RGB-only (3 channels) - alpha channels are separated to MASK at load time. This means the original Preview Bridge cannot detect masks from input images. This node solves that limitation by:

1. Adding an optional MASK input from upstream nodes (LoadImage, SAM, detection nodes, etc.)
2. Allowing users to select how input masks interact with MaskEditor drawings
3. Properly detecting empty masks for blocking decisions

## Features

- **Optional Mask Input**: Accept masks from upstream nodes alongside MaskEditor drawings
- **Configurable Mask Source**: Choose between combined, input_mask, or mask_editor modes
- **Mask Restoration**: Persist masks across image changes (never, always, if_same_size)
- **Smart Empty Detection**: Detects placeholder masks (64x64) and all-zero masks
- **Mask Combination**: OR (union) combination of multiple mask sources
- **Flexible Blocking**: Block on empty mask or always (debugging backstop)
- **Preview Display**: Visual overlay showing masked areas with red tint
- **Debug Info Output**: Detailed information about mask processing state

## Prerequisites

- ComfyUI installation
- Python 3.10+ (or ComfyUI's embedded Python)
- PyTorch (included with ComfyUI)

## Installation

### Git Clone (Recommended)

```bash
cd ComfyUI/custom_nodes
git clone https://github.com/DazzleNodes/ComfyUI-PreviewBridgeExtended.git
```

Then restart ComfyUI or use **Manager → Refresh Node Definitions**.

### Manual Installation

1. Download the [latest release](https://github.com/DazzleNodes/ComfyUI-PreviewBridgeExtended/releases)
2. Extract to `ComfyUI/custom_nodes/ComfyUI-PreviewBridgeExtended/`
3. Restart ComfyUI
4. Find the node in: **DazzleNodes → Preview Bridge Extended**

## Usage

### Inputs

| Input | Type | Required | Description |
|-------|------|----------|-------------|
| images | IMAGE | Yes | Input image(s) |
| mask_opt | MASK | No | Optional mask from upstream (LoadImage, SAM, etc.) |
| mask_source | Selection | No | How to determine final mask (default: combined) |
| restore_mask | Selection | No | Mask restoration mode (default: never) |
| block | Selection | No | Blocking mode (default: never) |

### Mask Source Options

| Option | Behavior |
|--------|----------|
| `combined` | OR (union) combine input_mask + restored/editor mask |
| `input_mask` | Only use input mask, ignore restored mask |
| `mask_editor` | Only use restored/cached mask, ignore input mask |

### Restore Mask Options

| Option | Behavior |
|--------|----------|
| `never` | Do not restore cached masks (default) |
| `always` | Always restore cached mask, resize if needed |
| `if_same_size` | Only restore if new image has same dimensions |

### Block Options

| Option | Behavior |
|--------|----------|
| `never` | Never block execution (default) |
| `if_empty_mask` | Block when result mask is empty |
| `always` | Always block execution (debugging backstop) |

### Outputs

- **image**: Pass-through of input image
- **mask**: Final processed mask based on mask_source selection
- **info**: Debug string showing mask processing details

### Common Use Cases

#### 1. Combine LoadImage Mask with MaskEditor

Connect LoadImage's MASK output to `mask_opt`, set `mask_source` to "combined":
- Users can draw additional mask areas in MaskEditor
- Both sources are combined using OR operation

#### 2. Use Detection Node Masks

Connect SAM or other detection node output:
- Set `mask_source` to "input_mask" to use only the detection
- Or "combined" to allow refinement with MaskEditor

#### 3. Fallback to MaskEditor Only

Set `mask_source` to "mask_editor":
- Ignores any input mask
- Uses only user-drawn masks from MaskEditor

## How It Works

1. **Mask Detection**: Checks if input mask is valid (not None, not all zeros, not placeholder 64x64)
2. **Source Selection**: Based on `mask_source`, determines which masks to use
3. **Combination**: For "combined" mode, uses OR operation (sum + clamp)
4. **Resizing**: Automatically resizes masks to match image dimensions
5. **Blocking**: If `block` enabled and final mask is empty, blocks execution

## Technical Details

### Empty Mask Detection

Masks are considered empty if:
- None or numel() == 0
- Shape is (1, 64, 64) or (64, 64) with all zeros (Preview node placeholder)
- All values are zero

### Mask Combination (OR)

```python
combined = torch.sum(torch.stack(masks_to_combine, dim=0), dim=0)
combined = torch.clamp(combined, 0, 1)
```

## Development

This project uses Git-RepoKit hooks for automatic version tracking.

```bash
# Clone repository
git clone https://github.com/DazzleNodes/ComfyUI-PreviewBridgeExtended.git
cd ComfyUI-PreviewBridgeExtended

# Install hooks for version tracking
bash scripts/install-hooks.sh

# Symlink to ComfyUI for development
cd /path/to/ComfyUI/custom_nodes
ln -s /path/to/ComfyUI-PreviewBridgeExtended ComfyUI-PreviewBridgeExtended
```

## Contributing

Contributions welcome! Please:

1. Fork the repository
2. Create a feature branch
3. Test changes in ComfyUI
4. Submit a pull request

## License

Preview Bridge Extended, Copyright (C) 2026 Dustin Darcy

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## Acknowledgements

Part of the [DazzleNodes](https://github.com/DazzleNodes/DazzleNodes) collection.

Inspired by:
- [ComfyUI-Impact-Pack](https://github.com/ltdrdata/ComfyUI-Impact-Pack) Preview Bridge node
- [WAS Node Suite](https://github.com/WASasquatch/was-node-suite-comfyui) mask combination patterns
