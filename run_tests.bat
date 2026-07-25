@echo off
REM Прогон тестов lavka_defense: сначала синтаксис-проверка, потом сам набор.
cd /d "%~dp0"

echo === py_compile ===
python -m py_compile bot.py config.py tests.py
if errorlevel 1 (
    echo.
    echo COMPILE FAILED - см. ошибку выше
    pause
    exit /b 1
)
echo COMPILE OK

echo.
echo === tests.py ===
if exist "venv\Scripts\python.exe" (
    "venv\Scripts\python.exe" tests.py
) else (
    python tests.py
)

echo.
echo === готово ===
pause
