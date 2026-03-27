#!/bin/bash
# Test runner for PreviewBridgeExtended
# Uses ComfyUI venv for dependencies (torch, numpy, PIL)

echo "============================================================"
echo "PreviewBridgeExtended - Test Suite"
echo "============================================================"
echo ""

# Resolve script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Try primary venv first, fallback to venv_new
if [ -f "/c/code/ComfyUI_experiment/venv/Scripts/python.exe" ]; then
    echo "Using ComfyUI venv: /c/code/ComfyUI_experiment/venv"
    /c/code/ComfyUI_experiment/venv/Scripts/python.exe "$SCRIPT_DIR/run_tests.py" "$@"
elif [ -f "/c/code/ComfyUI_experiment/venv_new/Scripts/python.exe" ]; then
    echo "Using ComfyUI venv_new: /c/code/ComfyUI_experiment/venv_new"
    /c/code/ComfyUI_experiment/venv_new/Scripts/python.exe "$SCRIPT_DIR/run_tests.py" "$@"
else
    echo "ERROR: ComfyUI venv not found at:"
    echo "  - /c/code/ComfyUI_experiment/venv"
    echo "  - /c/code/ComfyUI_experiment/venv_new"
    echo ""
    echo "Falling back to system Python..."
    python "$SCRIPT_DIR/run_tests.py" "$@"
fi

echo ""
echo "============================================================"
echo "Test run complete"
echo "============================================================"
