"""Tests for py/cache_policy.py -- the ComfyUI cache-ram default override.

The module is loaded by file path (not via the package __init__), so ComfyUI is
never imported, and every apply() call injects a fake args object: no test can
change a real server's cache settings.

Design: 2026-10-31__15-31-46__dev-workflow-process__comfy-ram-cache-evicts-paused-stage.md
"""
import importlib.util
import os
import sys
import types
from pathlib import Path

import pytest

_MOD_PATH = Path(__file__).resolve().parents[1] / "py" / "cache_policy.py"
_spec = importlib.util.spec_from_file_location("dazzle_cache_policy_under_test", _MOD_PATH)
cp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cp)

MB_PER_GB = 1024
STOCK_MAIN = (
    "def prompt_worker(q, server_instance, asset_manager):\n"
    f"        cache_ram = {cp.STOCK_ACTIVE_EXPR}\n"
    f"        cache_ram_inactive = {cp.STOCK_INACTIVE_EXPR}\n"
)
THIS_BOX_MB = 130153  # the measured machine: 127.1 GB


def decide(**overrides):
    kw = dict(cache_ram=[], cache_classic=False, cache_lru=0, cache_none=False,
              high_ram=False, total_ram_mb=THIS_BOX_MB, main_source=STOCK_MAIN, env={})
    kw.update(overrides)
    return cp.decide(**kw)


@pytest.fixture(autouse=True)
def _no_sentinel(monkeypatch):
    monkeypatch.delattr(sys, cp.SENTINEL, raising=False)


# --- the default case: what users without flags get -------------------------

def test_default_on_measured_box_matches_tested_shape():
    value, reason = decide()
    assert value == [10.0, 25.4]
    assert "--cache-ram 10 25.4" in reason


@pytest.mark.parametrize("total_gb, expected", [
    (32, [3.2, 6.4]),
    (64, [6.4, 12.8]),
    (96, [9.6, 19.2]),
    (128, [10.0, 25.6]),
])
def test_standard_machine_sizes(total_gb, expected):
    value, _ = decide(total_ram_mb=total_gb * MB_PER_GB)
    assert value == pytest.approx(expected)


def test_inactive_never_below_active_and_never_above_total():
    for total_gb in range(32, 129):
        value, _ = decide(total_ram_mb=total_gb * MB_PER_GB)
        active, inactive = value
        assert inactive >= active, total_gb
        assert inactive <= total_gb, total_gb


# --- explicit user choices win ----------------------------------------------

def test_one_value_keeps_users_active_threshold():
    value, _ = decide(cache_ram=[5.0])
    assert value == [5.0, 25.4]


def test_one_large_value_keeps_inactive_at_least_active():
    value, _ = decide(cache_ram=[40.0])
    assert value == [40.0, 40.0]


def test_two_values_are_respected():
    value, reason = decide(cache_ram=[10.0, 32.0])
    assert value is None
    assert "respecting" in reason


@pytest.mark.parametrize("flag", [
    {"cache_classic": True},
    {"high_ram": True},
    {"cache_lru": 50},
    {"cache_none": True},
])
def test_other_cache_modes_are_respected(flag):
    value, reason = decide(**flag)
    assert value is None
    assert "respecting your cache choice" in reason


@pytest.mark.parametrize("off", ["0", "false", "OFF", " no "])
def test_env_opt_out(off):
    value, reason = decide(env={cp.ENV_OPT_OUT: off})
    assert value is None
    assert cp.ENV_OPT_OUT in reason


def test_env_set_to_on_still_applies():
    value, _ = decide(env={cp.ENV_OPT_OUT: "1"})
    assert value == [10.0, 25.4]


# --- the self-retiring guard ------------------------------------------------

def test_guard_retires_when_inactive_expression_changes():
    changed = STOCK_MAIN.replace(cp.STOCK_INACTIVE_EXPR, "max(4.0, total_ram * 0.25)")
    value, reason = decide(main_source=changed)
    assert value is None
    assert "retired" in reason


def test_guard_retires_when_active_expression_changes_and_we_must_copy_it():
    changed = STOCK_MAIN.replace(cp.STOCK_ACTIVE_EXPR, "min(12.0, total_ram * 0.08)")
    value, _ = decide(main_source=changed)
    assert value is None


def test_active_expression_change_does_not_matter_when_user_set_active():
    changed = STOCK_MAIN.replace(cp.STOCK_ACTIVE_EXPR, "min(12.0, total_ram * 0.08)")
    value, _ = decide(main_source=changed, cache_ram=[5.0])
    assert value == [5.0, 25.4]


def test_unreadable_main_source_is_a_no_op():
    value, reason = decide(main_source=None)
    assert value is None
    assert "could not read" in reason


@pytest.mark.parametrize("old", [4.0, None, "10"])
def test_older_comfyui_without_list_cache_ram_is_a_no_op(old):
    value, _ = decide(cache_ram=old)
    assert value is None


# --- machine size limits ----------------------------------------------------

def test_below_floor_is_a_no_op():
    value, reason = decide(total_ram_mb=16 * MB_PER_GB)
    assert value is None
    assert "below" in reason


def test_over_128gb_stock_default_is_reachable_so_not_needed():
    value, reason = decide(total_ram_mb=256 * MB_PER_GB)
    assert value is None
    assert "not needed" in reason


# --- apply(): mutates only what it is given, once ---------------------------

def _fake_args(**kw):
    base = dict(cache_ram=[], cache_classic=False, cache_lru=0, cache_none=False, high_ram=False)
    base.update(kw)
    return types.SimpleNamespace(**base)


def test_apply_sets_injected_args_and_records_sentinel():
    args = _fake_args()
    rec = cp.apply(pack="Test", args=args, total_ram_mb=THIS_BOX_MB, main_source=STOCK_MAIN, env={})
    assert args.cache_ram == [10.0, 25.4]
    assert rec["applied"] is True
    assert getattr(sys, cp.SENTINEL) is rec


def test_apply_runs_once_per_process():
    first = _fake_args()
    second = _fake_args()
    cp.apply(pack="A", args=first, total_ram_mb=THIS_BOX_MB, main_source=STOCK_MAIN, env={})
    rec = cp.apply(pack="B", args=second, total_ram_mb=THIS_BOX_MB, main_source=STOCK_MAIN, env={})
    assert second.cache_ram == []  # the second pack must not act
    assert rec["by"] == "A"


def test_apply_leaves_args_alone_when_not_applying():
    args = _fake_args(cache_ram=[10.0, 32.0])
    rec = cp.apply(pack="Test", args=args, total_ram_mb=THIS_BOX_MB, main_source=STOCK_MAIN, env={})
    assert args.cache_ram == [10.0, 32.0]
    assert rec["applied"] is False


def test_apply_swallows_errors_and_changes_nothing():
    args = _fake_args()
    rec = cp.apply(pack="Test", args=args, total_ram_mb="not-a-number", main_source=STOCK_MAIN, env={})
    assert args.cache_ram == []
    assert rec["applied"] is False
    assert rec["reason"].startswith("error:")


# --- canary: the installed ComfyUI still has the expressions we key on ------

_LOCAL_MAIN = Path(os.environ.get("COMFYUI_MAIN", r"C:\code\ComfyUI_experiment\main.py"))


@pytest.mark.skipif(not _LOCAL_MAIN.exists(), reason="no local ComfyUI checkout")
def test_installed_comfyui_still_has_the_stock_expressions():
    src = _LOCAL_MAIN.read_text(encoding="utf-8")
    # If this fails, ComfyUI changed its cache defaults: the override has
    # retired itself in production -- re-check the design before updating.
    assert cp.STOCK_ACTIVE_EXPR in src
    assert cp.STOCK_INACTIVE_EXPR in src
