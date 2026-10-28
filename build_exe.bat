@echo off
rem Builds dist\MailSift.exe: one file your users double-click. No Python needed
rem on their PCs, and no black console window (use the Quit button in the page).
cd /d "%~dp0"
if not exist .build-venv (
    py -3 -m venv .build-venv || python -m venv .build-venv
)
.build-venv\Scripts\python -m pip install --upgrade pip >nul
.build-venv\Scripts\python -m pip install -r requirements-build.txt || goto :fail

echo Running tests...
.build-venv\Scripts\python -m pytest -q || goto :fail

.build-venv\Scripts\python -c "from mailsift.app import bundled_client_id; import sys; sys.exit(0 if bundled_client_id() else 1)"
if errorlevel 1 (
    echo.
    echo Put your Application ^(client^) ID in mailsift\client_id.txt first, then run this again.
    goto :fail
)

.build-venv\Scripts\pyinstaller --noconfirm --clean --onefile --windowed --name MailSift ^
    --icon mailsift\icon.ico ^
    --add-data "mailsift\static;mailsift\static" ^
    --add-data "mailsift\client_id.txt;mailsift" ^
    mailsift_app.py || goto :fail

echo.
echo Built dist\MailSift.exe. Share that one file (plus USER_GUIDE.txt if you like).
pause
exit /b 0

:fail
echo Build failed.
pause
exit /b 1
