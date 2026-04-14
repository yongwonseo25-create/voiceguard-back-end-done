@echo off
REM ══════════════════════════════════════════════════════════════════
REM Voice Guard — 원클릭 완전 자동 배포 (Windows)
REM 더블클릭하면 Cloud Run + Firebase 자동 배포 후 URL 출력
REM ══════════════════════════════════════════════════════════════════

setlocal enabledelayedexpansion

cd /d "%~dp0"

echo.
echo ════════════════════════════════════════════════════════════
echo   Voice Guard 완전 자동 배포 개시
echo ════════════════════════════════════════════════════════════
echo.

REM Git Bash(bash.exe)를 통해 auto_deploy_master.sh 실행
REM Git for Windows 설치 경로 자동 탐색

set BASH_EXE=
for %%P in (
    "C:\Program Files\Git\bin\bash.exe"
    "C:\Program Files (x86)\Git\bin\bash.exe"
    "%LOCALAPPDATA%\Programs\Git\bin\bash.exe"
) do (
    if exist %%P (
        set BASH_EXE=%%P
        goto :found_bash
    )
)

echo [ERR] Git Bash를 찾을 수 없습니다.
echo       https://git-scm.com 에서 Git for Windows를 설치하십시오.
pause
exit /b 1

:found_bash
echo [OK] Git Bash 발견: !BASH_EXE!
echo.

REM auto_deploy_master.sh를 Bash로 실행
!BASH_EXE! -c "cd '%~dp0' && bash auto_deploy_master.sh"

if %ERRORLEVEL% NEQ 0 (
    echo.
    echo ════════════════════════════════════════════════════════════
    echo [ERR] 배포 실패. 위 에러 메시지를 확인하십시오.
    echo ════════════════════════════════════════════════════════════
    pause
    exit /b 1
)

pause
