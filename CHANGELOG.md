# Changelog

All notable changes to ComfyUI Preview Bridge Extended will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0-alpha] - 2026-01-07

### Added
- Initial release of Preview Bridge Extended node
- Optional MASK input (`mask_opt`) for upstream mask sources
- `mask_source` selection: combined, input_mask, mask_editor
- `restore_mask` functionality: never, always, if_same_size
  - Persist masks across image changes
  - Size-aware restoration option
- `block` modes: never, if_empty_mask, always
  - "always" mode as debugging backstop
- OR (union) mask combination for "combined" mode
- Empty mask detection including placeholder masks (64x64)
- Preview display with red tint overlay for masked areas
- Debug info output showing mask processing state
- Automatic mask resizing to match image dimensions
- Independent cache system for mask persistence

### Technical Details
- Based on Impact Pack's Preview Bridge architecture
- Uses PyTorch interpolate for GPU-accelerated mask resizing
- Implements ExecutionBlocker for conditional blocking
- Module-level caches separate from Impact Pack
- Part of DazzleNodes collection
