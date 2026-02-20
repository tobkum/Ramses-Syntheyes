@echo off
setlocal

:: Get the directory where the batch file is located
set "PROJECT_ROOT=%~dp0"

:: Set PYTHONPATH to include the lib directory and project root
set "PYTHONPATH=%PROJECT_ROOT%lib;%PROJECT_ROOT%;%PYTHONPATH%"

echo [Ramses-Syntheyes] Running complete test suite...
echo.

:: Run discovery from the project root
python -m unittest discover -v tests

if %ERRORLEVEL% neq 0 (
    echo.
    echo [ERROR] Some tests failed!
    exit /b %ERRORLEVEL%
)

echo.
echo [SUCCESS] All tests passed.
pause
