# Architecture

## LayerCache System

The node uses a unified `LayerCache` for mask storage with three explicit layers:

- **upstream**: Original mask from `mask_opt` input (immutable reference)
- **additions**: User additions drawn in MaskEditor (orange overlay)
- **subtractions**: User erasures from the upstream mask (removed from red overlay)

The "additions win" formula ensures predictable output:

```python
combined = max(upstream - subtractions, additions)
```

## Mask Combination

For "combined" mode, masks are combined using OR (union) operation:

```python
combined = torch.sum(torch.stack(masks_to_combine, dim=0), dim=0)
combined = torch.clamp(combined, 0, 1)
```

## Empty Mask Detection

Masks are considered empty if:
- `None` or `numel() == 0`
- Shape is `(1, 64, 64)` or `(64, 64)` with all zeros (ComfyUI Preview node placeholder)
- All values are zero

## Instant Preview Refresh

When `mask_output` or `editor_target` widgets change, JavaScript listeners trigger an API call to regenerate the preview from the current LayerCache state. This provides WYSIWYG behavior without re-running the workflow.

## node_base.py Architecture

Shared mask orchestration logic extracted for code reuse between IMAGE and LATENT variants:

- `process_masks()`: Full mask pipeline (input processing, LayerCache decomposition, output selection, blocking evaluation)
- `apply_dazzle_signal()`: Reads active state from `sys._dazzle_command_state`, picks play/pause config
- `should_block()`: Evaluates blocking condition based on block mode and mask state
- Widget constant definitions (`MASK_OUTPUT_WIDGET`, `BLOCK_WIDGET`, `DAZZLE_SIGNAL_WIDGET`, etc.)

Both IMAGE and LATENT variants call `process_masks()` for ~70% code reuse.

## Latent Node: VAE Decode Caching

The LATENT variant caches VAE decode results via content fingerprinting:
- Computes a lightweight fingerprint of the latent tensor (mean, std, shape)
- Skips re-decode when the fingerprint matches the previous run
- Supports all latent types (SD1.5, SDXL, Flux, WAN, Qwen, etc.)
