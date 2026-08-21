@echo off
chcp 65001 >nul
REM ============================================================
REM  SunoDownloader.exe 다시 빌드
REM  suno.py / ui.html / download.py 를 고친 뒤 이 파일을 실행하면 된다.
REM ============================================================
setlocal
cd /d "%~dp0"

echo [1/3] 빌드 중...
python -m PyInstaller --noconfirm --onedir --name SunoDownloader ^
    --add-data "ui.html;." --collect-all playwright suno.py
if errorlevel 1 goto :fail

echo.
echo [2/3] exe 배치 중...
xcopy /E /Y /I "dist\SunoDownloader\*" "." >nul
if errorlevel 1 goto :fail

echo.
echo [3/3] 중간 산출물 정리 중...
rmdir /S /Q build 2>nul
rmdir /S /Q dist 2>nul

echo.
echo 완료 - SunoDownloader.exe 가 갱신됐습니다.
goto :end

:fail
echo.
echo 빌드에 실패했습니다. 위 메시지를 확인하세요.
echo (필요한 패키지: pip install pyinstaller playwright)

:end
pause
