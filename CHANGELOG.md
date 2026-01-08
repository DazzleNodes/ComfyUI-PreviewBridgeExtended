# Changelog

All notable changes to ComfyUI Preview Bridge Extended will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
