# Dazzle Command Integration

## DAZZLE_SIGNAL Protocol

Preview Bridge Extended accepts an optional `DAZZLE_SIGNAL` input from the [Dazzle Command](https://github.com/DazzleNodes/ComfyUI-DazzleCommand) orchestration node.

The signal dict contains both play and pause configurations (static across toggles):

```python
{
    "pause_gate_intent": "block",
    "pause_gate_mode": "auto",       # or "always", "if_empty_mask", "if_empty_editor"
    "play_gate_intent": "open",
    "play_gate_mode": "never",       # or "auto"
    "pause_seed_intent": "transient",
    "play_seed_intent": "lock",
    "schema_version": 1,
}
```

## Cache-Transparent Operation

The signal dict is **static** -- it doesn't change between play/pause toggles. The active state is communicated via `sys._dazzle_command_state` side-channel, written by Dazzle Command's JS before prompt generation.

PBE reads the active state during `apply_dazzle_signal()`:

```python
cmd_state = getattr(sys, '_dazzle_command_state', None)
state = cmd_state.get('state', 'paused')  # 'playing' or 'paused'
```

Based on the active state, PBE picks the corresponding gate config from the signal dict.

## IS_CHANGED

PBE's `IS_CHANGED` returns `"dazzle:STATE"` based on the sys side-channel. This forces re-execution when play/pause toggles (PBE must re-evaluate its blocking decision), while allowing SmartResCalc and KSampler to cache.

## Smart Block Selection (auto mode)

When `gate_mode` is `"auto"`, PBE intelligently selects a block mode based on current mask state:

**For blocking (pause):**
- No editor content, has upstream mask -> `if_empty_editor`
- Empty mask -> `if_empty_mask`
- Both present -> `always`

**For unblocking (play):**
- `auto` maps to `never` (let execution through)

## Companion Versions

| Node | Min Version | Role |
|------|------------|------|
| Dazzle Command | v0.2.0-alpha | Provides DAZZLE_SIGNAL output |
| Smart Resolution Calculator | v0.11.0 | Seed control integration |
