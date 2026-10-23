@echo off
title LPX AI video review
cd /d "%~dp0"
set "PYTHONIOENCODING=utf-8"
if exist ".ai-venv\ready.txt" if exist "models\clip\ready.txt" goto ai
if exist ".venv\Scripts\python.exe" goto installed
if not exist "vendor\cv2" goto setup
if not exist "%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" goto setup
set "PYTHONPATH=%~dp0vendor"
"%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" application.py --open
if errorlevel 1 pause
exit /b
:setup
echo Pierwsze uruchomienie: przygotowanie bibliotek aplikacji.
call setup.cmd
if errorlevel 1 (
  echo Zainstaluj Python 3.11 lub 3.12 z python.org i uruchom ponownie.
  pause
  exit /b 1
)
:installed
set "PYTHONPATH="
.venv\Scripts\python.exe application.py --open
if errorlevel 1 pause

exit /b
:ai
set "PYTHONPATH="
.ai-venv\Scripts\python.exe application.py --open
if errorlevel 1 pause
