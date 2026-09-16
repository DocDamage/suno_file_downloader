@echo off
chcp 65001 >nul
REM ============================================================
REM  Rebuild SunoDownloader.exe
REM  Run this after changing suno.py, ui.html, or download.py.
REM ============================================================
setlocal
cd /d "%~dp0"

echo [1/3] Building...
python -m PyInstaller --noconfirm --onedir --name SunoDownloader ^
    --add-data "ui.html;." --collect-all playwright --collect-all imageio_ffmpeg suno.py
if errorlevel 1 goto :fail

echo.
echo [2/3] Copying executable files...
xcopy /E /Y /I "dist\SunoDownloader\*" "." >nul
if errorlevel 1 goto :fail

echo.
echo [3/3] Cleaning intermediate files...
rmdir /S /Q build 2>nul
rmdir /S /Q dist 2>nul

echo.
echo Complete - SunoDownloader.exe has been updated.
goto :end

:fail
echo.
echo Build failed. Check the message above.
echo (Required packages: pip install pyinstaller playwright)

:end
pause
