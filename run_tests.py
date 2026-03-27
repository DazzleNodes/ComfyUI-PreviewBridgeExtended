#!/usr/bin/env python
"""
Test driver for PreviewBridgeExtended.

Stubs ComfyUI-specific modules (folder_paths, nodes, server, etc.) before
invoking pytest, avoiding the `py/` directory collision with pytest's
internal `import py` dependency.

Usage:
    python run_tests.py [pytest args...]
    python run_tests.py -v
    python run_tests.py tests/test_dual_loading.py::TestSentinelMechanism -v
"""

import os
import sys
import types


def create_comfyui_stubs():
    """Stub all ComfyUI-specific modules so project code can import cleanly."""

    # folder_paths - used by node.py and utils.py
    mod = types.ModuleType('folder_paths')
    mod.get_temp_directory = lambda: os.path.join(os.path.dirname(__file__), 'tests')
    sys.modules['folder_paths'] = mod

    # nodes - used by preview.py (needs PreviewImage class)
    mod = types.ModuleType('nodes')

    class FakePreviewImage:
        def save_images(self, *args, **kwargs):
            return {"ui": {"images": []}}

    mod.PreviewImage = FakePreviewImage
    sys.modules['nodes'] = mod

    # server - used by __init__.py for API route registration
    mod = types.ModuleType('server')

    class FakeRoutes:
        def post(self, path):
            def decorator(fn):
                return fn
            return decorator

    class FakePromptServer:
        routes = FakeRoutes()

    class FakePromptServerClass:
        instance = FakePromptServer()

    mod.PromptServer = FakePromptServerClass
    sys.modules['server'] = mod

    # aiohttp / aiohttp.web - used by __init__.py for route registration
    aiohttp_mod = types.ModuleType('aiohttp')
    web_mod = types.ModuleType('aiohttp.web')
    web_mod.json_response = lambda *args, **kwargs: None
    aiohttp_mod.web = web_mod
    sys.modules['aiohttp'] = aiohttp_mod
    sys.modules['aiohttp.web'] = web_mod

    # comfy_execution.graph - used for ExecutionBlocker
    comfy_mod = types.ModuleType('comfy_execution')
    graph_mod = types.ModuleType('comfy_execution.graph')
    graph_mod.ExecutionBlocker = lambda x: x
    comfy_mod.graph = graph_mod
    sys.modules['comfy_execution'] = comfy_mod
    sys.modules['comfy_execution.graph'] = graph_mod


def main():
    # Remove project root from sys.path to prevent the local `py/` directory
    # from shadowing pytest's `import py` (the PyPI `py` package).
    # Tests load __init__.py via importlib.util.spec_from_file_location()
    # so they don't need the project root on sys.path.
    project_root = os.path.dirname(os.path.abspath(__file__))
    sys.path[:] = [p for p in sys.path if os.path.abspath(p) != project_root]

    # Stub ComfyUI modules BEFORE importing pytest
    create_comfyui_stubs()

    import pytest

    # Build pytest args: always use our pytest.ini config
    default_args = [
        f'--rootdir={project_root}',
        '-c', os.path.join(project_root, 'pytest.ini'),
    ]

    # If no test paths given, default to tests/
    user_args = sys.argv[1:]
    has_test_path = any(
        not arg.startswith('-') for arg in user_args
    )
    if not has_test_path:
        default_args.append(os.path.join(project_root, 'tests'))

    sys.exit(pytest.main(default_args + user_args))


if __name__ == '__main__':
    main()
