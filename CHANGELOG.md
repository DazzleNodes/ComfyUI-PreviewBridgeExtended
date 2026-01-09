# Changelog

All notable changes to ComfyUI Preview Bridge Extended will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
