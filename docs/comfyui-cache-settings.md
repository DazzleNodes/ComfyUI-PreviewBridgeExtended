# ComfyUI cache settings and paused workflows

Dazzle Command and Preview Bridge Extended let you pause a workflow, look at the result, and resume. Resuming is only cheap if ComfyUI still has the first half of the workflow cached. This page explains a ComfyUI default that throws that cache away, what the Dazzle nodes change about it, and how to tune or turn off that change.

## The symptom

1. Pause your workflow and run it. The first stage renders and the gate blocks.
2. Run a different workflow (another tab, a quick test, anything).
3. Come back, press **Play**, and run again.

Without the fix, step 3 renders the first stage **again**, producing an identical image with an identical seed. You pay the full render time twice, and any Image Save on the first stage writes a duplicate file.

If you never run anything else between pausing and pressing Play, you never see it. That's why it looks intermittent.

## Why it happens

Recent versions of ComfyUI use a "RAM pressure" cache by default (the startup log says `Using RAM pressure cache.`). It keeps the results of the workflow you're running now, and is meant to keep other workflows' results only up to a RAM budget. The default budget, though, is "keep them until free RAM drops below `min(128 GB, all of your RAM)`". On any machine with 128 GB of RAM or less, free RAM is always below that, so the moment another workflow starts executing, every cached result of your paused workflow is discarded.

ComfyUI controls this with the `--cache-ram` launch flag, which takes two values in GB:

```
--cache-ram ACTIVE INACTIVE
```

- **ACTIVE**: when free RAM falls below this, ComfyUI starts evicting cached results, oldest workflows first. Default: 10% of your RAM, between 2 and 10 GB.
- **INACTIVE**: results from *other* workflows are kept until free RAM falls below this. Default: all of your RAM, up to 128 GB, which in practice means "never keep them".

## What the Dazzle nodes change

When ComfyUI starts, Dazzle Command or Preview Bridge Extended (whichever loads first, once per server) sets a reachable INACTIVE value:

```
INACTIVE = 20% of your RAM (never less than ACTIVE)
```

| Your RAM | Equivalent launch flag |
|---|---|
| 32 GB | `--cache-ram 3.2 6.4` |
| 64 GB | `--cache-ram 6.4 12.8` |
| 128 GB | `--cache-ram 10 25.4` |

You'll see one line in the ComfyUI console at startup, for example:

```
[DazzleCommand] ComfyUI cache: keeping other workflows' cached results until free RAM < 25.4 GB (equivalent to --cache-ram 10 25.4).
```

Results kept this way are still released automatically when RAM is needed, oldest first. On a heavy two-stage workflow (an image model plus a refiner), keeping the paused workflow cost about 2 GB of RAM.

### When it does nothing

The Dazzle nodes leave ComfyUI's settings alone, and say why at startup, when:

- you passed `--cache-ram` with two values (yours wins); with one value, your ACTIVE value is kept and only INACTIVE is filled in
- you chose a different cache mode: `--cache-classic`, `--high-ram`, `--cache-lru N`, or `--cache-none`
- your machine has less than 32 GB of RAM (keeping extra results there risks swapping)
- your machine has more than 128 GB of RAM (ComfyUI's default already keeps other workflows' results there)
- your ComfyUI version computes its cache defaults differently from the version this was written for; the Dazzle nodes then step aside rather than guess
- you turned it off (below)

## Turning it off

Set this environment variable before starting ComfyUI:

```
set DAZZLE_CACHE_RAM_OVERRIDE=0          (Windows cmd)
$env:DAZZLE_CACHE_RAM_OVERRIDE = "0"     (PowerShell)
export DAZZLE_CACHE_RAM_OVERRIDE=0       (Linux / macOS)
```

The startup line then reads `ComfyUI cache: disabled by DAZZLE_CACHE_RAM_OVERRIDE=0 -- using ComfyUI's cache defaults.`

## Choosing your own values

Pass `--cache-ram` yourself and the Dazzle nodes will respect it. Some starting points:

- **Keep more** (you switch between workflows a lot and have RAM to spare): lower INACTIVE, for example `--cache-ram 10 16` on a 128 GB machine.
- **Keep less** (you run memory-hungry video workflows): raise INACTIVE, for example `--cache-ram 10 48`.
- **On a 16-24 GB machine** where you still want pause-and-resume across workflows: try a small INACTIVE such as `--cache-ram 2 4`, and watch for swapping.

Keep INACTIVE at least as large as ACTIVE. Otherwise ComfyUI can evict the workflow you're running now while keeping stale ones.

Other cache modes, for reference:

- `--cache-classic` keeps only the current workflow (other workflows are always discarded, like the default on most machines).
- `--cache-lru N` keeps the N most recently used node results across all workflows, regardless of RAM.
- `--cache-none` disables caching (every node runs every time).

## Checking it yourself

Start ComfyUI with `--verbose DETAIL` and watch the console. Every sampler that actually runs logs a line such as `Sampler: model=...`. After pressing Play, the first stage's model should **not** appear; only the stages after the gate should.
