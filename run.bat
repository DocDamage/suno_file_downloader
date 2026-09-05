@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo ==========================================
echo    Suno File Downloader
echo ==========================================
echo.

REM ---- Python 찾기 --------------------------------------------
REM PATH 에 없어도 설치돼 있는 경우가 많아 여러 곳을 살펴본다.
set "PY="
set "PYARGS="

REM 1) PATH 의 python
python --version >nul 2>&1
if not errorlevel 1 (
    set "PY=python"
    goto :found
)

REM 2) py 런처 — python 이 PATH 에 없어도 대개 이건 있다
py -3 --version >nul 2>&1
if not errorlevel 1 (
    set "PY=py"
    set "PYARGS=-3"
    goto :found
)

REM 3) 흔히 설치되는 위치를 직접 뒤진다
for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do (
    if exist "%%D\python.exe" set "PY=%%D\python.exe"
)
if defined PY goto :found
for /d %%D in ("%ProgramFiles%\Python3*") do (
    if exist "%%D\python.exe" set "PY=%%D\python.exe"
)
if defined PY goto :found
for /d %%D in ("%ProgramFiles(x86)%\Python3*") do (
    if exist "%%D\python.exe" set "PY=%%D\python.exe"
)
if defined PY goto :found
goto :nopython

:found
echo  Python: %PY% %PYARGS%
echo.

REM ---- 필요한 패키지가 없을 때만 설치 ---------------------------
"%PY%" %PYARGS% -c "import playwright, imageio_ffmpeg" >nul 2>&1
if errorlevel 1 (
    echo  처음 실행이라 필요한 패키지를 설치합니다. 2~3분 걸립니다...
    echo.
    "%PY%" %PYARGS% -m pip install -r requirements.txt --disable-pip-version-check
    if errorlevel 1 goto :pipfail
    echo.
    echo  설치 완료.
    echo.
)

REM ---- 실행 (인자를 그대로 넘긴다: run.bat sync 등) --------------
"%PY%" %PYARGS% suno.py %*
set RC=%errorlevel%

if not "%RC%"=="0" (
    echo.
    echo  종료 코드 %RC% 로 끝났습니다.
    pause
)
exit /b %RC%

:pipfail
echo.
echo  [!] 패키지 설치에 실패했습니다. 위 메시지를 확인하세요.
echo      사내망이라면 프록시나 방화벽 때문일 수 있습니다.
echo.
pause
exit /b 1

:nopython
echo  [!] Python 을 찾지 못했습니다.
echo.
if exist "%LOCALAPPDATA%\Microsoft\WindowsApps\python.exe" (
    echo      Microsoft Store 연결용 가짜 python.exe 만 있습니다.
    echo      설정 ^> 앱 ^> 고급 앱 설정 ^> 앱 실행 별칭 에서
    echo      "python.exe" 를 끄고, 아래에서 정식 Python 을 설치하세요.
    echo.
)
echo      https://www.python.org/downloads/  에서 설치하세요.
echo      설치 화면에서 "Add python.exe to PATH" 를 반드시 체크해야 합니다.
echo.
echo      Python 을 깔 수 없는 PC 라면, 집에서 build.bat 으로 만든
echo      SunoDownloader.exe 와 _internal 폴더를 복사해 오면 됩니다.
echo.
pause
exit /b 1
