"""
Tests for the dual-loading detection guard in __init__.py.

The guard uses a sys-level sentinel to detect when PreviewBridgeExtended
is loaded more than once (e.g., as standalone AND inside DazzleNodes).
On duplicate load, it disables WEB_DIRECTORY and skips API registration
to prevent split-brain cache bugs.

These tests verify the sentinel mechanism without requiring ComfyUI.
"""

import importlib
import importlib.util
import os
import sys
import pytest


# Path to the __init__.py we're testing
INIT_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "__init__.py")
SENTINEL_KEY = "_preview_bridge_extended_loaded"


@pytest.fixture(autouse=True)
def clean_sentinel():
    """Remove the sentinel before and after each test to ensure isolation."""
    if hasattr(sys, SENTINEL_KEY):
        delattr(sys, SENTINEL_KEY)
    yield
    if hasattr(sys, SENTINEL_KEY):
        delattr(sys, SENTINEL_KEY)


@pytest.fixture
def _mock_comfyui(monkeypatch, tmp_path):
    """
    Provide minimal stubs for ComfyUI imports so __init__.py can load
    without a full ComfyUI environment.

    Stubs: folder_paths, nodes, server, comfy_execution.graph
    """
    # Stub folder_paths
    folder_paths_mod = type(sys)("folder_paths")
    folder_paths_mod.get_temp_directory = lambda: str(tmp_path)
    monkeypatch.setitem(sys.modules, "folder_paths", folder_paths_mod)

    # Stub nodes with a minimal PreviewImage
    nodes_mod = type(sys)("nodes")

    class FakePreviewImage:
        def save_images(self, *args, **kwargs):
            return {"ui": {"images": []}}

    nodes_mod.PreviewImage = FakePreviewImage
    monkeypatch.setitem(sys.modules, "nodes", nodes_mod)

    # Stub server with a fake PromptServer that has a routes object
    server_mod = type(sys)("server")

    class FakeRoutes:
        def post(self, path):
            """Decorator that accepts the function and does nothing."""
            def decorator(fn):
                return fn
            return decorator

    class FakePromptServer:
        routes = FakeRoutes()

    class FakePromptServerClass:
        instance = FakePromptServer()

    server_mod.PromptServer = FakePromptServerClass
    monkeypatch.setitem(sys.modules, "server", server_mod)

    # Stub aiohttp.web with a fake json_response
    aiohttp_mod = type(sys)("aiohttp")
    web_mod = type(sys)("aiohttp.web")
    web_mod.json_response = lambda *args, **kwargs: None
    aiohttp_mod.web = web_mod
    monkeypatch.setitem(sys.modules, "aiohttp", aiohttp_mod)
    monkeypatch.setitem(sys.modules, "aiohttp.web", web_mod)

    # Stub comfy_execution.graph (for ExecutionBlocker)
    comfy_exec_mod = type(sys)("comfy_execution")
    graph_mod = type(sys)("comfy_execution.graph")
    graph_mod.ExecutionBlocker = lambda x: x
    comfy_exec_mod.graph = graph_mod
    monkeypatch.setitem(sys.modules, "comfy_execution", comfy_exec_mod)
    monkeypatch.setitem(sys.modules, "comfy_execution.graph", graph_mod)


def _load_init_as_module(module_name: str):
    """Load __init__.py as a fresh module with the given name."""
    spec = importlib.util.spec_from_file_location(module_name, INIT_PATH)
    module = importlib.util.module_from_spec(spec)
    # Temporarily register so relative imports work
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


class TestSentinelMechanism:
    """Test the sys-level sentinel that detects dual loading."""

    def test_sentinel_not_set_initially(self):
        """Before any load, the sentinel should not exist."""
        assert not hasattr(sys, SENTINEL_KEY)

    def test_first_load_sets_sentinel(self, _mock_comfyui):
        """First load should set the sentinel with the module's directory path."""
        mod = _load_init_as_module("pbe_test_first")
        try:
            assert hasattr(sys, SENTINEL_KEY)
            sentinel_value = getattr(sys, SENTINEL_KEY)
            expected_dir = os.path.dirname(os.path.abspath(INIT_PATH))
            assert sentinel_value == expected_dir
        finally:
            sys.modules.pop("pbe_test_first", None)

    def test_first_load_not_duplicate(self, _mock_comfyui):
        """First load should not be flagged as duplicate."""
        mod = _load_init_as_module("pbe_test_single")
        try:
            assert mod._is_duplicate_load is False
        finally:
            sys.modules.pop("pbe_test_single", None)

    def test_first_load_enables_web_directory(self, _mock_comfyui):
        """First load should set WEB_DIRECTORY to './web'."""
        mod = _load_init_as_module("pbe_test_web")
        try:
            assert mod.WEB_DIRECTORY == "./web"
        finally:
            sys.modules.pop("pbe_test_web", None)

    def test_second_load_detects_duplicate(self, _mock_comfyui):
        """Second load should detect the sentinel and flag as duplicate."""
        mod1 = _load_init_as_module("pbe_test_dup1")
        try:
            assert mod1._is_duplicate_load is False

            # Simulate second load from a different module name
            # (like DazzleNodes' importlib loader would do)
            mod2 = _load_init_as_module("pbe_test_dup2")
            try:
                assert mod2._is_duplicate_load is True
            finally:
                sys.modules.pop("pbe_test_dup2", None)
        finally:
            sys.modules.pop("pbe_test_dup1", None)

    def test_duplicate_disables_web_directory(self, _mock_comfyui):
        """Duplicate load should set WEB_DIRECTORY to None."""
        mod1 = _load_init_as_module("pbe_test_webdup1")
        try:
            mod2 = _load_init_as_module("pbe_test_webdup2")
            try:
                assert mod1.WEB_DIRECTORY == "./web"
                assert mod2.WEB_DIRECTORY is None
            finally:
                sys.modules.pop("pbe_test_webdup2", None)
        finally:
            sys.modules.pop("pbe_test_webdup1", None)

    def test_both_export_node_class_mappings(self, _mock_comfyui):
        """Both loads should export NODE_CLASS_MAPPINGS (ComfyUI requires it)."""
        mod1 = _load_init_as_module("pbe_test_ncm1")
        try:
            mod2 = _load_init_as_module("pbe_test_ncm2")
            try:
                assert "PreviewBridgeExtended" in mod1.NODE_CLASS_MAPPINGS
                assert "PreviewBridgeExtended" in mod2.NODE_CLASS_MAPPINGS
            finally:
                sys.modules.pop("pbe_test_ncm2", None)
        finally:
            sys.modules.pop("pbe_test_ncm1", None)

    def test_sentinel_stores_first_load_path(self, _mock_comfyui):
        """Sentinel should store the first load's path, not the second's."""
        mod1 = _load_init_as_module("pbe_test_path1")
        try:
            first_path = getattr(sys, SENTINEL_KEY)

            mod2 = _load_init_as_module("pbe_test_path2")
            try:
                # Sentinel should still be the first path
                assert getattr(sys, SENTINEL_KEY) == first_path
            finally:
                sys.modules.pop("pbe_test_path2", None)
        finally:
            sys.modules.pop("pbe_test_path1", None)


class TestDefaultAlignment:
    """Test that function signature defaults match INPUT_TYPES defaults."""

    def test_editor_target_default_matches_input_types(self, _mock_comfyui):
        """process() editor_target default should match INPUT_TYPES default."""
        mod = _load_init_as_module("pbe_test_defaults")
        try:
            node_cls = mod.NODE_CLASS_MAPPINGS["PreviewBridgeExtended"]

            # Get INPUT_TYPES default
            input_types = node_cls.INPUT_TYPES()
            widget_default = input_types["optional"]["editor_target"][1]["default"]

            # Get function signature default
            import inspect
            sig = inspect.signature(node_cls.process)
            param_default = sig.parameters["editor_target"].default

            assert widget_default == param_default, (
                f"INPUT_TYPES default ({widget_default!r}) != "
                f"function signature default ({param_default!r})"
            )
        finally:
            sys.modules.pop("pbe_test_defaults", None)
