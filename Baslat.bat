@echo off
chcp 65001 >nul
cd /d "%~dp0"

if exist "%LOCALAPPDATA%\Programs\Python\Python314\pythonw.exe" (
    start "" "%LOCALAPPDATA%\Programs\Python\Python314\pythonw.exe" "%~dp0gui_app.py"
    exit /b 0
)

where pyw >nul 2>nul
if %ERRORLEVEL% EQU 0 (
    start "" pyw -3 "%~dp0gui_app.py"
    exit /b 0
)

where pythonw >nul 2>nul
if %ERRORLEVEL% EQU 0 (
    start "" pythonw "%~dp0gui_app.py"
    exit /b 0
)

python "%~dp0gui_app.py"
