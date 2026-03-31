# Dazzle Command Integration

## DAZZLE_SIGNAL Protocol

Preview Bridge Extended accepts an optional `DAZZLE_SIGNAL` input from the [Dazzle Command](https://github.com/DazzleNodes/ComfyUI-DazzleCommand) orchestration node.

The signal dict contains both play and pause configurations plus the active state:

```python
{
    "active_state": "playing",          # or "paused" — per-node state
    "pause_gate_intent": "block",
    "pause_gate_mode": "auto",          # or "always", "if_empty_mask", "if_empty_editor"
    "play_gate_intent": "open",
    "play_gate_mode": "never",          # or "auto"
    "pause_seed_intent": "transient",
    "play_seed_intent": "lock",
    "schema_version": 2,
}
```

## Per-Node State

Each DazzleCommand maintains independent state in a per-node registry
(`sys._dazzle_command_states`). PBE reads `active_state` from the signal dict
received via the noodle — each PBE reads only its connected DazzleCommand's state.

Multiple DazzleCommand + PBE pairs in the same workflow operate independently
(e.g., DC-1=Play + DC-2=Pause).

PBE reads the active state during `apply_dazzle_signal()`:

```python
state = dazzle_signal.get('active_state', 'paused')
```

Based on the active state, PBE picks the corresponding gate config from the signal dict.

## IS_CHANGED

PBE's `IS_CHANGED` returns `"dazzle:STATE"` based on the signal's `active_state`.
This forces re-execution when play/pause toggles (PBE must re-evaluate its blocking
decision). Standalone PBE nodes (no `dazzle_signal` noodle) return `""` and are
unaffected by DazzleCommand nodes elsewhere in the workflow.

## Cache-Compatible Previews

PBE uses deterministic preview filenames based on node ID (e.g., `PBE-node1_00001_.png`)
instead of random temp suffixes. This prevents cache invalidation — downstream nodes
(KSampler, VAEEncode) correctly cache when PBE's input hasn't changed.

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
| Dazzle Command | v0.2.3-alpha | Provides DAZZLE_SIGNAL output with per-node state |
| Smart Resolution Calculator | v0.11.3 | Seed control with per-node DC lookup |
