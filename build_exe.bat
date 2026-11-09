@echo off
rem Builds dist\MailSift.exe locally: tests, security scans, then one windowed .exe
rem with version info and a SHA-256 checksum. (GitHub Actions does the same on every push.)
cd /d "%~dp0"
if not exist .build-venv (
    py -3 -m venv .build-venv || python -m venv .build-venv
)
rem No "pip install --upgrade pip" here on purpose: that fetches pip
rem itself unpinned, and pip is what enforces --require-hashes on
rem everything else. The bundled pip is new enough.
rem --require-hashes: refuses any package whose hash isn't in the lock file.
.build-venv\Scripts\python -m pip install --require-hashes -r requirements.lock || goto :fail

echo Running tests...
.build-venv\Scripts\python -m pytest -q || goto :fail
echo Static analysis...
.build-venv\Scripts\python -m bandit -r mailsift -ll -q || goto :fail
echo Dependency audit...
.build-venv\Scripts\python -m pip_audit -r requirements.lock --disable-pip || goto :fail

.build-venv\Scripts\python -c "from mailsift.app import bundled_client_id; import sys; sys.exit(0 if bundled_client_id() else 1)"
if errorlevel 1 (
    echo.
    echo Put your Application ^(client^) ID in mailsift\client_id.txt first, then run this again.
    goto :fail
)

.build-venv\Scripts\python tools\make_version_info.py || goto :fail
.build-venv\Scripts\pyinstaller --noconfirm --clean --onefile --windowed --name MailSift ^
    --icon mailsift\icon.ico --version-file build\version_info.txt ^
    --add-data "mailsift\static;mailsift\static" ^
    --add-data "mailsift\client_id.txt;mailsift" ^
    --add-data "mailsift\support_url.txt;mailsift" ^
    mailsift_app.py || goto :fail

powershell -NoProfile -Command "$h=(Get-FileHash dist\MailSift.exe -Algorithm SHA256).Hash.ToLower(); \"$h  MailSift.exe\" | Out-File -Encoding ascii dist\MailSift.exe.sha256; Write-Host SHA-256: $h"
copy /y USER_GUIDE.txt dist\ >nul

echo.
echo Built dist\MailSift.exe. Share it with USER_GUIDE.txt and MailSift.exe.sha256.
pause
exit /b 0

:fail
echo Build failed.
pause
exit /b 1
