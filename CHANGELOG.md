# Changelog

All notable changes to ComfyUI Preview Bridge Extended will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.3.4-alpha] - 2026-03-26

### Added
- **Dual-Loading Detection Guard**: Detects when PBE is installed both as a standalone node and inside DazzleNodes
  - Uses `sys`-level sentinel shared across all Python module namespaces
  - Prints actionable WARNING at startup with both installation paths
  - Disables duplicate instance's `WEB_DIRECTORY` (prevents double JS extension)
  - Skips duplicate instance's API route registration (prevents cache mismatch)
  - Root cause: Each `importlib` load creates separate module-level caches (`_layer_cache`, `_context_cache`); API routes from the first load target the wrong cache when node class comes from the second load
- **Context Cache Fallback**: `prepare_for_editing` reconstructs context from basic caches when context cache is unavailable, preventing MaskEditor from opening with blank alpha
- **ComfyUI Registry Install Instructions**: Added registry install method to README

### Fixed
- **`editor_target` Default Mismatch**: Function signature default was `"mask_editor"` but `INPUT_TYPES` default was `"combined"`
  - Fixed in `node.py` process() signature
  - Fixed in `api.py` fallback defaults for `generate_preview_for_api()` and `get_preview_for_api()`
  - Prevents incorrect fallback behavior when widget value or context is missing
- **Cross-Image Mask Resize Crash**: `get_combined()` and `get_input_mask()` now resize cached layers to match current image dimensions when `restore_mask=always` carries masks across image changes
- **Stale Project Name**: Fixed CONTRIBUTING.md referencing "ImageMask-Fix" instead of "PreviewBridgeExtended"

### Removed
- `web/preview_bridge_extended.js.deprecated` (unused legacy file)

### Technical Details
- See `private/claude/2026-02-22__15-38-06__dual-loading-mask-output-bug-analysis.md` for dual-loading root cause analysis
- See `private/claude/2026-02-22__16-08-09__full-postmortem_dual-loading-split-brain-cache-bug.md`
- See `private/claude/2026-03-26__22-32-01__first-run-combined-excludes-upstream-bug.md` for context cache investigation

## [0.3.3-alpha] - 2026-01-12

### Changed
- **Quieter Console Output**: Changed verbose diagnostic logging from INFO to DEBUG level
  - Normal workflow runs now produce minimal console output
  - Keeps single "Loaded v{version}" message on startup for visibility
  - To enable debug logging, set environment variable: `PBE_DEBUG=1`
  - Or programmatically: `logging.getLogger("PreviewBridgeExtended").setLevel(logging.DEBUG)`
- **API Registration Logging**: Changed print statements to proper logging
  - Success messages now use `logging.debug()` (silent by default)
  - Failure messages use `logging.warning()` (always visible)

## [0.3.2-alpha] - 2026-01-12

### Fixed
- **restore_mask Clipspace Bypass**: Fixed clipspace file bypassing restore_mask setting
  - Root cause: Clipspace file persists on disk across image changes
  - When loading clipspace, it was resized to new image dimensions regardless of restore_mask
  - Solution: Check restore_mask setting before resizing clipspace when dimensions differ
  - Now `if_same_size` correctly clears mask when image dimensions change
  - `never` also correctly clears when clipspace size differs from image
  - `always` continues to resize and restore as expected

- **restore_mask=always LayerCache Preservation**: Fixed masks being cleared instead of resized
  - Root cause: `validate_image()` cleared LayerCache layers before restoration could occur
  - When image changed, layers were invalidated before `_load_clipspace_mask()` could fall back to them
  - Solution: Added `preserve_layers` parameter to `validate_image()`
  - `restore_mask=always` preserves layers unconditionally for cross-image restoration
  - `restore_mask=if_same_size` checks mask dimensions vs image dimensions BEFORE preserving
    - Only preserves if sizes match; clears if sizes differ
  - `restore_mask=never` clears layers unconditionally

## [0.3.1-alpha] - 2026-01-12

### Fixed
- **MaskEditor Clear Button**: Fixed Clear button not actually clearing masks (fixes #3)
  - Combined mode: Use full subtraction instead of setting layers to None
  - Input_mask mode: Prevent fallback to original mask when fully subtracted
  - All modes: Fixed `prepare_for_editing()` fallback to `original_m` when mask is intentionally empty
- **restore_mask=never Behavior**: Fixed user edits persisting when `restore_mask=never`
  - Previously only cleared on image change, now clears additions/subtractions on every workflow run
- **Subtraction Layer Reset Bug**: Fixed subtractions being lost on every workflow re-run (fixes #12)
  - Root cause: `on_upstream_change()` used `data_ptr()` (memory address) for change detection
  - Memory addresses change every execution even when content is identical
  - Solution: Content-based fingerprinting using strategic sampling (first/last 4 values + shape)
- **Chained Node Cache Invalidation**: Fixed second PBE node in a chain losing all edits on re-run
  - Root cause: `validate_image()` and `_detect_images_changed()` used object identity (`id()` and `is not`)
  - Dynamically generated images (outputs from upstream nodes) have new tensor objects each execution
  - Solution: Apply same content-based fingerprinting to image change detection

### Added
- `compute_tensor_fingerprint()` function in `mask_ops.py` for O(1) content-based tensor comparison
  - Based on [gist](https://gist.github.com/djdarcy/f7aaf10d36f2c9f207e948e6f39e8ad7) from Impact Pack PR #1172
  - Samples 8 values (first 4 + last 4) combined with shape for MD5 fingerprint

### Changed
- `LayerCache.image_id` renamed to `image_fingerprint` (now stores content hash, not memory address)
- `LayerCache.upstream_hash` now stores content fingerprint string instead of pointer hash

## [0.3.0-alpha] - 2026-01-09

### Added
- **Instant Preview Refresh**: Preview now updates immediately when changing `mask_output` or `editor_target` widgets (fixes #1)
  - No longer requires re-running workflow to see preview changes
  - Widget change listeners call `get-preview` API to regenerate preview from LayerCache state
- **get-preview API**: New endpoint `/preview-bridge-extended/get-preview` for fetching current preview without clipspace decomposition
  - Used by Cancel handler and widget change listeners
  - Returns preview based on current LayerCache state and widget values
- **VS Code Debug Configs**: Added ComfyUI debugging configurations to `.vscode/launch.json`
  - "ComfyUI: Debug This Node" - Launch ComfyUI with debugger attached
  - "ComfyUI: Attach to Running" - Attach to running ComfyUI instance
  - Uses `COMFYUI_PATH` environment variable for portability

### Changed
- **Full LayerCache Migration**: Removed all legacy dual-cache code
  - LayerCache is now the exclusive storage system
  - Removed `_preview_bridge_input_mask_cache` and `_preview_bridge_editor_mask_cache`
  - Simplified `node.py`, `api.py`, and `preview.py` to use LayerCache exclusively
- **Removed Delta Mode**: Eliminated obsolete delta computation from `preview.py`
  - Delta mode was incompatible with LayerCache architecture
  - Preview now uses direct layer display (input_mask as red, editor_mask as orange)
  - Simplified `apply_mask_overlays()` function

### Fixed
- **Widget Switch Preview Bug**: Fixed preview not updating when switching between modes
  - Root cause: Widget changes didn't trigger preview refresh (only workflow execution did)
  - Solution: Added widget callbacks that call `getPreview` API
- **Cancel Handler**: MaskEditor Cancel now correctly restores preview from LayerCache state
  - Previously could show stale preview after cancelling
- **GitHub Workflows**: Fixed workflow names from "Fit Mask to Image" to "Preview Bridge Extended"
  - Updated `main.yml` CI workflow name
  - Updated `publish-to-registry.yml` workflow name and repo URL
- **Issue Templates**: Fixed project name in bug report and feature request templates

### Technical Details
- See `private/claude/2026-01-09__06-05-13__widget-switch-preview-refresh.md` for widget listener implementation
- See `private/claude/2026-01-09__05-51-13__preview-delta-mode-bug.md` for delta mode removal analysis

## [0.2.0-alpha] - 2026-01-09

### Added
- **Phase 1: LayerCache Architecture**: New unified layer storage system
  - `LayerCache` dataclass with explicit `upstream`, `additions`, `subtractions` layers
  - "Additions win" formula: `combined = max(upstream - subtractions, additions)`
  - `decompose_and_store()`: Decomposes clipspace into canonical layers based on `editor_target`
  - `get_output_mask()`: Returns appropriate mask based on `mask_output` setting
  - `get_combined()`, `get_input_mask()`, `get_editor_mask()` accessor methods

### Changed
- LayerCache now runs in parallel with legacy caches (validation phase)
- RE-COMPOSITION in `prepare_for_editing` now uses LayerCache's `get_combined()`

### Technical Details
- Architecture validated through Gemini 2.5 Pro consultation
- Continuation ID: `d41790c9-6e5c-4505-b3a4-80f00796b768`
- See `private/claude/2026-01-09__04-51-54__layercache-full-migration-analysis.md`

### Known Issues
- LayerCache/Legacy MISMATCH warnings expected (~12 unit differences due to anti-aliasing)
- Full migration to LayerCache-only planned for v0.2.1

## [0.1.5-alpha] - 2026-01-09

### Fixed
- **RE-COMPOSITION Subtractions**: Fixed subtractions being lost when switching back to combined mode
  - RE-COMPOSITION now uses `cached_input_override` (user's edited input with subtractions) instead of upstream
  - Preserves subtractions made in `input_mask` mode when returning to `combined` mode

### Known Issues
- Complex multi-step mode sequences (3+ switches) may still lose some subtractions
- Root cause: Subtractions not tracked as explicit layer - planned for Phase 1 LayerCache architecture

## [0.1.4-alpha] - 2026-01-09

### Changed
- **Phase 0 Module Refactor**: Split monolithic files into focused modules for maintainability
  - Python: `preview_bridge_extended.py` (1768 lines) → 6 modules:
    - `node.py`: Main PreviewBridgeExtended class
    - `api.py`: API endpoint handlers (generate_preview_for_api, prepare_for_editing)
    - `mask_ops.py`: Tensor operations (resize, combine, delta computation)
    - `preview.py`: Preview image generation with mask overlays
    - `caches.py`: Module-level cache dictionaries
    - `utils.py`: Clipspace loading utilities
  - JavaScript: `preview_bridge_extended.js` (478 lines) → 3 modules:
    - `main.js`: Widget setup and node initialization
    - `api.js`: API communication with Python backend
    - `maskeditor.js`: MaskEditor integration

### Fixed
- **Mode-Switch Cache Corruption**: Fixed multiple bugs causing edits to be lost when switching modes
  - `input_mask/mask_editor`: Now computes intersection instead of returning full upstream
  - Added decomposition when switching FROM combined TO mask_editor/input_mask
  - Added RE-COMPOSITION when switching TO combined FROM other modes
  - `input_mask/input_mask`: MaskEditor now loads intersection (not full upstream)
  - Context cache now updated with editor_target in prepare_for_editing

### Known Issues
- RE-COMPOSITION uses upstream instead of cached_input_override, causing subtractions to be lost in complex mode sequences (documented in private/claude/2026-01-09__03-42-22__subtractions-layer-architecture-analysis.md)
- Explicit subtractions layer needed for reliable multi-mode editing (planned for Phase 1.5)

## [0.1.3-alpha] - 2026-01-09

### Fixed
- **Stale Clipspace Data**: Fixed output errors when switching between `editor_target` modes
  - `mask_editor/mask_editor`: Now computes delta (additions only) instead of returning stale combined data
  - `mask_editor/input_mask`: Now computes delta from editor_cache instead of returning stale combined data
  - `input_mask/combined`: Falls back to upstream when intersection is empty (stale additions-only clipspace)
  - Root cause: Clipspace file persists across mode changes, containing incompatible data for new mode

### Added
- Diagnostic logging in `process()` for widget values and final mask computation
- Diagnostic logging for all `mask_editor/*` delta computations
- JS now passes current widget values to `refresh-preview` API (prevents stale cache usage)

### Changed
- All `mask_output=mask_editor` combinations now compute delta (additions only)
  - Ensures output contains only user additions, not stale combined state from previous modes
- Updated 9-combination matrix documentation to reflect delta computation for all mask_editor outputs

### Known Issues
- Cache architecture needs refactoring (see GitHub issue #2)
- Widget changes don't trigger preview refresh (no ComfyUI widget change events)

## [0.1.2-alpha] - 2026-01-09

### Added
- `_combine_masks_and()` helper method using `torch.min()` for soft mask intersection
  - Preserves feathering and antialiasing (fuzzy logic AND operation)
- Test images for development workflow (`tests/test-920x1022.jpg`, `tests/test-920x1022_clipspace-masked.png`)

### Fixed
- **9-Combination Matrix**: All 9 combinations of `mask_output` × `editor_target` now behave predictably
  - `input_mask + input_mask`: Returns user's modified input (`input_override`) instead of ignoring edits
  - `combined + input_mask`: Returns `input_override OR editor_cache` (both layers combined correctly)
  - `input_mask + combined`: Returns intersection (`editor_mask AND upstream`) using `torch.min()`
    - Preserves user's subtractions from upstream while ignoring additions outside upstream area
- **MaskEditor Loading**: Fixed erasures being restored when reopening MaskEditor in combined mode
  - For `editor_target=combined`, editor_m now used directly (not OR'd with original)
  - Erasures are properly preserved across MaskEditor sessions
- **Preview Generation**: Fixed `mask_output=input_mask` preview not showing erasures
  - Preview now uses intersection (AND) when `editor_target=combined`
  - Preview matches actual output for all `editor_target` modes
- Added comprehensive docstring documenting the full 9-combination matrix in `_determine_final_mask()`

### Changed
- Widget option ordering now consistent: both `mask_output` and `editor_target` use
  `[combined, mask_editor, input_mask]` order

### Technical Details
- Validated architecture through Collaborate3 expert consultation with Gemini 2.5 Pro
- Phase 1 of phased implementation approach (Phase 2: MaskState dataclass refactor planned)
- Added diagnostic logging for debugging mask states

## [0.1.1-alpha] - 2026-01-08

### Added
- New `block` option: `if_empty_editor` - blocks execution if user hasn't drawn in MaskEditor
  - Useful for workflows requiring user mask additions before proceeding
- Test workflow file for development/testing (`tests/Preview-Bridge-Extended_test-workflow.json`)
- README: Background section explaining relationship to Impact-Pack PRs
- README: Documentation for `editor_target` options

### Changed
- **BREAKING**: Renamed `mask_source` to `mask_output` for clarity
  - `mask_output` controls what goes to the OUTPUT mask slot
  - Preview display follows `mask_output` selection (WYSIWYG)
- Fixed preview color bug where editor_mask rendered as red with `editor_target=combined`
  - Preview now correctly uses upstream mask for red layer, editor mask for orange layer
- Fixed `mask_output=input_mask` outputting both layers when `editor_target=combined`
  - Output now correctly uses immutable `upstream_input_mask` for input layer routing
- Fixed `mask_output=mask_editor` outputting both layers when `editor_target=combined`
  - Output now correctly computes delta to extract only editor additions
- Fixed mask erasures (subtractions) being ignored with `mask_output=combined, editor_target=combined`
  - When editing combined mode, editor_mask IS the complete state (erasures included)
  - OR combination only used for separate layer editing modes
- Fixed upstream mask change detection using `torch.equal()` comparison
- Fixed cache clearing when `restore_mask=never` and upstream mask changes
- Updated `mask_output` tooltip to correctly describe WYSIWYG preview behavior

## [0.1.0-alpha] - 2026-01-07

### Added
- Initial release of Preview Bridge Extended node
- Optional MASK input (`mask_opt`) for upstream mask sources
- `mask_output` selection: combined, input_mask, mask_editor
- `editor_target` selection: controls which layer MaskEditor affects
- `restore_mask` functionality: never, always, if_same_size
  - Persist masks across image changes
  - Size-aware restoration option
- `block` modes: never, if_empty_mask, always
  - "always" mode as debugging backstop
- OR (union) mask combination for "combined" mode
- Empty mask detection including placeholder masks (64x64)
- Two-layer preview display: red tint for input mask, orange tint for editor mask
- Debug info output showing mask processing state
- Automatic mask resizing to match image dimensions
- Independent cache system for mask persistence

### Technical Details
- Based on Impact Pack's Preview Bridge architecture
- Uses PyTorch interpolate for GPU-accelerated mask resizing
- Implements ExecutionBlocker for conditional blocking
- Module-level caches separate from Impact Pack
- Part of DazzleNodes collection
