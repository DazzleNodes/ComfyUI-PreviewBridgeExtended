"""
Pytest configuration for PreviewBridgeExtended tests.

Stubs ComfyUI-specific modules before any test imports, so that
the PBE package can be loaded outside of a running ComfyUI instance.
"""

import sys
import types
import os
import tempfile


def _stub_comfyui_modules():
    """Create minimal stubs for ComfyUI modules that PBE imports at load time."""
    if "folder_paths" in sys.modules:
        return  # Already stubbed or running inside ComfyUI

    # folder_paths
    folder_paths = types.ModuleType("folder_paths")
    folder_paths.get_temp_directory = lambda: tempfile.gettempdir()
    sys.modules["folder_paths"] = folder_paths

    # nodes (with PreviewImage stub)
    nodes = types.ModuleType("nodes")

    class FakePreviewImage:
        def save_images(self, *args, **kwargs):
            return {"ui": {"images": []}}

    nodes.PreviewImage = FakePreviewImage
    sys.modules["nodes"] = nodes

    # server (with PromptServer stub that has routes)
    server = types.ModuleType("server")

    class FakeRoutes:
        def post(self, path):
            def decorator(fn):
                return fn
            return decorator

    class FakePromptServer:
        routes = FakeRoutes()

    class FakePromptServerClass:
        instance = FakePromptServer()

    server.PromptServer = FakePromptServerClass
    sys.modules["server"] = server

    # aiohttp.web
    aiohttp = types.ModuleType("aiohttp")
    web = types.ModuleType("aiohttp.web")
    web.json_response = lambda *args, **kwargs: None
    aiohttp.web = web
    sys.modules["aiohttp"] = aiohttp
    sys.modules["aiohttp.web"] = web

    # comfy_execution.graph (for ExecutionBlocker)
    comfy_exec = types.ModuleType("comfy_execution")
    graph = types.ModuleType("comfy_execution.graph")
    graph.ExecutionBlocker = lambda x: x
    comfy_exec.graph = graph
    sys.modules["comfy_execution"] = comfy_exec
    sys.modules["comfy_execution.graph"] = graph


# Stub before pytest collects any test modules
_stub_comfyui_modules()
