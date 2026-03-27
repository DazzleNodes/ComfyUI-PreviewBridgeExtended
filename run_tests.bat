@echo off
REM Test runner for PreviewBridgeExtended
REM Uses ComfyUI venv for dependencies (torch, numpy, PIL)

echo ============================================================
echo PreviewBridgeExtended - Test Suite
echo ============================================================
echo.

REM Try primary venv first, fallback to venv_new
if exist C:\code\ComfyUI_experiment\venv\Scripts\python.exe (
    echo Using ComfyUI venv: C:\code\ComfyUI_experiment\venv
    C:\code\ComfyUI_experiment\venv\Scripts\python.exe run_tests.py %*
) else if exist C:\code\ComfyUI_experiment\venv_new\Scripts\python.exe (
    echo Using ComfyUI venv_new: C:\code\ComfyUI_experiment\venv_new
    C:\code\ComfyUI_experiment\venv_new\Scripts\python.exe run_tests.py %*
) else (
    echo ERROR: ComfyUI venv not found at:
    echo   - C:\code\ComfyUI_experiment\venv
    echo   - C:\code\ComfyUI_experiment\venv_new
    echo.
    echo Falling back to system Python...
    python run_tests.py %*
)

echo.
echo ============================================================
echo Test run complete
echo ============================================================
