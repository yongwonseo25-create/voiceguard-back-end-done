@echo off
REM ══════════════════════════════════════════════════════════════════
REM Voice Guard E2E 파일럿 점화 스크립트 (Windows Batch)
REM 백엔드(포트 8000) + 프론트엔드(포트 3000) 동시 기동
REM ══════════════════════════════════════════════════════════════════

setlocal enabledelayedexpansion

cd /d "%~dp0"

echo.
echo ════════════════════════════════════════════════════════════
echo   Voice Guard E2E 파일럿 점화 개시
echo ════════════════════════════════════════════════════════════
echo.

REM ──────────────────────────────────────────────────────────────
REM [STEP 0] 좀비 프로세스 자동 폭파 (포트 8000 / 3000)
REM ──────────────────────────────────────────────────────────────
echo [0/2] 좀비 포트 자동 분쇄 중...
echo        포트 8000 (백엔드) 점유 프로세스 탐색...

for /f "tokens=5" %%P in ('netstat -ano ^| findstr ":8000 " ^| findstr "LISTENING" 2^>nul') do (
    echo        -> PID %%P 강제 폭파!
    taskkill /PID %%P /F >nul 2>&1
)

echo        포트 3000 (프론트엔드) 점유 프로세스 탐색...
for /f "tokens=5" %%P in ('netstat -ano ^| findstr ":3000 " ^| findstr "LISTENING" 2^>nul') do (
    echo        -> PID %%P 강제 폭파!
    taskkill /PID %%P /F >nul 2>&1
)

echo        [OK] 좀비 프로세스 완전 사살 완료
echo.
timeout /t 1 /nobreak >nul

REM ──────────────────────────────────────────────────────────────
REM [STEP 1] 백엔드 기동 (Postgres WAL 기반 poll_fallback 모드)
REM ──────────────────────────────────────────────────────────────
echo [1/2] 백엔드 서버(포트 8000) 기동 중...
echo        Postgres 연결 확인, WAL 기반 poll_fallback 준비...
cd backend
start "Voice Guard Backend" cmd /k "uvicorn main:app --reload --port 8000"
cd ..
echo        [OK] 백엔드 터미널 윈도우 생성 완료

REM ──────────────────────────────────────────────────────────────
REM [STEP 2] 프론트엔드 기동 (포트 3000 강제 고정)
REM ──────────────────────────────────────────────────────────────
echo [2/2] 프론트엔드 서버(포트 3000) 기동 중...
cd "FRONT END"
start "Voice Guard Frontend" cmd /k "npm run dev"
cd ..
echo        [OK] 프론트엔드 터미널 윈도우 생성 완료

REM 서버 시작 대기
timeout /t 3 /nobreak >nul

echo.
echo ════════════════════════════════════════════════════════════
echo.
echo   [SUCCESS] 터미널 복구 및 좀비 프로세스 폭파 완료!
echo   브라우저에서 즉시 http://localhost:3000 을 여십시오!
echo.
echo ════════════════════════════════════════════════════════════
echo.
echo   프론트엔드:  http://localhost:3000
echo   백엔드 API:  http://localhost:8000
echo.
echo ════════════════════════════════════════════════════════════
echo.

pause
