@echo off
cd /d "%~dp0"
set "PYTHONIOENCODING=utf-8"
echo Instalacja bibliotek i modelu kategorii. Wymagany internet i kilka GB miejsca.
if exist ".ai-venv\Scripts\python.exe" goto install
if exist "%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" (
  "%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" -m venv .ai-venv
) else (
  py -3 -m venv .ai-venv
)
if errorlevel 1 goto error
:install
set "PYTHONPATH="
.ai-venv\Scripts\python.exe -m pip install -r requirements.txt "torch>=2.6,<3" "transformers>=4.50,<5" pillow safetensors
if errorlevel 1 goto error
.ai-venv\Scripts\python.exe install_categories.py
if errorlevel 1 goto error
echo ready> .ai-venv\ready.txt
pause
exit /b 0
:error
echo Instalacja nie powiodla sie. Sprawdz polaczenie i komunikat powyzej.
pause
exit /b 1
