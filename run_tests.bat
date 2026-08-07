@echo off
setlocal
cd /d "%~dp0"

set "PYTHON=python"
if exist "venv\Scripts\python.exe" set "PYTHON=venv\Scripts\python.exe"

echo === py_compile ===
"%PYTHON%" -m py_compile bot.py config.py tests.py channel_scan.py storage.py
if errorlevel 1 (
    echo.
    echo COMPILE FAILED
    exit /b 1
)
echo COMPILE OK

echo.
echo === tests.py ===
"%PYTHON%" tests.py
set "TEST_EXIT=%ERRORLEVEL%"

echo.
if not "%TEST_EXIT%"=="0" (
    echo TESTS FAILED
    exit /b %TEST_EXIT%
)

echo ALL CHECKS PASSED
exit /b 0
