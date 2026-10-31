"""
ComfyUI cache policy -- keep other workflows' cached results across workflow switches.

Why this exists
---------------
ComfyUI's default RAM-pressure cache evicts every cached output that is not
part of the prompt currently running, as soon as that prompt executes its first
node. It does this through an "inactive" free-RAM target whose default is
min(128 GB, total RAM) -- a target free RAM can never reach on machines with up
to 128 GB. ComfyUI's own design intends this target as a *budget* ("limit the
amount of RAM background intermediates can use, so that switching workflows
doesn't degrade performance too much", ComfyUI #13802), but the default turns
the budget into "keep nothing".

For DazzleCommand / Preview Bridge Extended that breaks pause-and-resume: pause a
workflow, run anything else, come back, press Play -- the paused stage renders
again with identical inputs.

What it does
------------
At custom-node import time (before ComfyUI's prompt worker reads its cache
settings), fill in `--cache-ram` with a reachable inactive target:

    inactive = max(active, INACTIVE_FRACTION * total RAM)

It acts only when ALL of these hold, and otherwise changes nothing:
- not disabled via DAZZLE_CACHE_RAM_OVERRIDE=0
- the user is on the default RAM-pressure cache (no --cache-classic,
  --high-ram, --cache-lru N, --cache-none)
- the user gave zero or one --cache-ram values (two values: theirs wins;
  one value: kept as the active threshold, only the inactive one is added)
- ComfyUI's main.py still contains the stock default expressions, and the stock
  inactive default is unreachable on this machine -- so the override retires
  itself the moment upstream changes or fixes the default
- the machine has at least MIN_TOTAL_GB of RAM

Applied once per server process, whichever Dazzle pack loads first.

Design: 2026-10-31__15-31-46__dev-workflow-process__comfy-ram-cache-evicts-paused-stage.md
User docs: docs/comfyui-cache-settings.md
"""

import inspect
import os
import sys

ENV_OPT_OUT = "DAZZLE_CACHE_RAM_OVERRIDE"
SENTINEL = "_dazzle_cache_policy"

# Keep other workflows' results until free RAM falls below this share of total
# RAM. Measured 2026-10-31 on a heavy MOSS + Qwen workflow (128 GB box): the
# retained cache held ~2 GB and the largest 2-second free-RAM dip was 1.0 GB.
# 20% leaves 6.4 / 12.8 / 25.4 GB on 32 / 64 / 128 GB machines.
INACTIVE_FRACTION = 0.20

# Below this, keeping stale results risks swapping; leave ComfyUI's default.
MIN_TOTAL_GB = 32.0

# The stock expressions in ComfyUI's main.py prompt_worker (ComfyUI 7c4390a6f,
# 2026-10-31). If either disappears, ComfyUI changed its defaults and this
# override must not guess -- it steps aside.
STOCK_ACTIVE_EXPR = "min(10.0, max(2.0, comfy.model_management.total_ram * 0.10 / 1024.0))"
STOCK_INACTIVE_EXPR = "min(128.0, comfy.model_management.total_ram / 1024.0)"

_OFF_VALUES = ("0", "false", "no", "off")


def stock_active_gb(total_gb):
    """ComfyUI's default active headroom (main.py prompt_worker)."""
    return min(10.0, max(2.0, total_gb * 0.10))


def stock_inactive_gb(total_gb):
    """ComfyUI's default inactive target (main.py prompt_worker)."""
    return min(128.0, total_gb)


def decide(*, cache_ram, cache_classic, cache_lru, cache_none, high_ram,
           total_ram_mb, main_source, env):
    """Pure decision: return (new_cache_ram_list or None, reason).

    None means "change nothing"; the reason says why, for the startup log.
    """
    flag = str(env.get(ENV_OPT_OUT, "")).strip().lower()
    if flag in _OFF_VALUES:
        return None, f"disabled by {ENV_OPT_OUT}={env.get(ENV_OPT_OUT)} -- using ComfyUI's cache defaults"

    if not isinstance(cache_ram, list):
        return None, "this ComfyUI predates two-value --cache-ram -- not applied"

    if cache_classic or high_ram:
        return None, "--cache-classic/--high-ram selected -- respecting your cache choice"
    if (cache_lru or 0) > 0:
        return None, f"--cache-lru {cache_lru} selected -- respecting your cache choice"
    if cache_none:
        return None, "--cache-none selected -- respecting your cache choice"

    if len(cache_ram) >= 2:
        return None, f"--cache-ram {cache_ram[0]:g} {cache_ram[1]:g} set -- respecting your cache choice"

    if main_source is None:
        return None, "could not read ComfyUI's prompt_worker to verify its cache defaults -- not applied"
    if STOCK_INACTIVE_EXPR not in main_source or (
            len(cache_ram) == 0 and STOCK_ACTIVE_EXPR not in main_source):
        return None, "ComfyUI's cache defaults have changed -- override retired, using ComfyUI's defaults"

    total_gb = float(total_ram_mb) / 1024.0
    if total_gb < MIN_TOTAL_GB:
        return None, f"{total_gb:.0f} GB RAM is below {MIN_TOTAL_GB:.0f} GB -- using ComfyUI's cache defaults"
    if stock_inactive_gb(total_gb) < total_gb:
        return None, "ComfyUI's default already keeps other workflows' results on this machine -- not needed"

    active = float(cache_ram[0]) if len(cache_ram) == 1 else stock_active_gb(total_gb)
    inactive = round(max(active, INACTIVE_FRACTION * total_gb), 1)
    return [active, inactive], (
        f"keeping other workflows' cached results until free RAM < {inactive:g} GB "
        f"(equivalent to --cache-ram {active:g} {inactive:g})")


def _read_main_source():
    main_mod = sys.modules.get("__main__")
    fn = getattr(main_mod, "prompt_worker", None)
    if fn is None:
        return None
    try:
        return inspect.getsource(fn)
    except (OSError, TypeError):
        return None


def apply(pack="DazzleNodes", args=None, total_ram_mb=None, main_source=None, env=None):
    """Apply the policy once per process. Returns the record stored on sys.

    All inputs are injectable so tests never touch ComfyUI's real settings;
    with no injections it reads comfy.cli_args.args, comfy.model_management,
    __main__.prompt_worker and os.environ.
    """
    existing = getattr(sys, SENTINEL, None)
    if existing is not None:
        return existing

    record = {"applied": False, "value": None, "reason": "", "by": pack}
    try:
        if args is None:
            from comfy.cli_args import args as comfy_args
            args = comfy_args
        if total_ram_mb is None:
            import comfy.model_management as mm
            total_ram_mb = mm.total_ram
        if main_source is None:
            main_source = _read_main_source()
        if env is None:
            env = os.environ

        value, reason = decide(
            cache_ram=getattr(args, "cache_ram", None),
            cache_classic=getattr(args, "cache_classic", False),
            cache_lru=getattr(args, "cache_lru", 0),
            cache_none=getattr(args, "cache_none", False),
            high_ram=getattr(args, "high_ram", False),
            total_ram_mb=total_ram_mb,
            main_source=main_source,
            env=env,
        )
        record["reason"] = reason
        if value is not None:
            args.cache_ram = value
            record["applied"] = True
            record["value"] = value
            print(f"[{pack}] ComfyUI cache: {reason}.")
            print(f"[{pack}]   Keeps paused workflows from re-rendering after you run another workflow. "
                  f"Freed automatically when RAM is needed. Set {ENV_OPT_OUT}=0 to use ComfyUI's default.")
        else:
            print(f"[{pack}] ComfyUI cache: {reason}.")
    except Exception as e:  # never let a policy helper break node loading
        record["reason"] = f"error: {type(e).__name__}: {e} -- not applied"
        print(f"[{pack}] WARNING: ComfyUI cache policy skipped ({type(e).__name__}: {e}).")

    setattr(sys, SENTINEL, record)
    return record
