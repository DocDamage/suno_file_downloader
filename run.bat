@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo ==========================================
echo    Suno File Downloader
echo ==========================================
echo.

REM ---- Python 설치 확인 ----------------------------------------
python --version >nul 2>&1
if errorlevel 1 (
    echo  [!] Python 이 설치되어 있지 않습니다.
    echo.
    echo      https://www.python.org/downloads/  에서 설치하세요.
    echo      설치 화면에서 "Add python.exe to PATH" 를 반드시 체크해야 합니다.
    echo.
    pause
    exit /b 1
)

REM ---- 필요한 패키지가 없을 때만 설치 ---------------------------
python -c "import playwright" >nul 2>&1
if errorlevel 1 (
    echo  처음 실행이라 필요한 패키지를 설치합니다. 1~2분 걸립니다...
    echo.
    python -m pip install -r requirements.txt --disable-pip-version-check
    if errorlevel 1 (
        echo.
        echo  [!] 패키지 설치에 실패했습니다. 위 메시지를 확인하세요.
        pause
        exit /b 1
    )
    echo.
    echo  설치 완료.
    echo.
)

REM ---- 실행 (인자를 그대로 넘긴다: run.bat sync 등) --------------
python suno.py %*
set RC=%errorlevel%

if not "%RC%"=="0" (
    echo.
    echo  종료 코드 %RC% 로 끝났습니다.
    pause
)
exit /b %RC%
