@echo off
rem MailSift launcher for Windows. Pass --demo or --dry-run to try it safely first.
cd /d "%~dp0"
if not exist .venv (
    echo Setting up MailSift for the first time...
    py -3 -m venv .venv || python -m venv .venv
    .venv\Scripts\python -m pip install --upgrade pip >nul
    .venv\Scripts\python -m pip install -r requirements.txt
)
.venv\Scripts\python -m mailsift %*
pause
