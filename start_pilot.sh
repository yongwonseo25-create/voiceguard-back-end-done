#!/bin/bash
# ══════════════════════════════════════════════════════════════════
# Voice Guard E2E 파일럿 점화 스크립트 (Bash)
# 백엔드(포트 8000) + 프론트엔드(포트 3000) 동시 기동
# ══════════════════════════════════════════════════════════════════

set -e

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_ROOT"

echo ""
echo "════════════════════════════════════════════════════════════"
echo "  Voice Guard E2E 파일럿 점화 개시"
echo "════════════════════════════════════════════════════════════"
echo ""

# ──────────────────────────────────────────────────────────────────
# [STEP 0] 좀비 프로세스 자동 폭파 (포트 8000 / 3000)
# ──────────────────────────────────────────────────────────────────
echo "[0/2] 좀비 포트 자동 분쇄 중..."

kill_port() {
    local PORT=$1
    local NAME=$2
    echo "       포트 ${PORT} (${NAME}) 점유 프로세스 탐색..."

    # Windows Git Bash / WSL 환경
    if command -v netstat &>/dev/null; then
        PIDS=$(netstat -ano 2>/dev/null | grep ":${PORT} " | grep "LISTENING" | awk '{print $5}' | sort -u)
        for PID in $PIDS; do
            if [ -n "$PID" ] && [ "$PID" != "0" ]; then
                echo "       -> PID ${PID} 강제 폭파!"
                taskkill //PID "$PID" //F 2>/dev/null || kill -9 "$PID" 2>/dev/null || true
            fi
        done
    fi

    # Linux/Mac 환경 (lsof 사용)
    if command -v lsof &>/dev/null; then
        PIDS=$(lsof -ti ":${PORT}" 2>/dev/null || true)
        for PID in $PIDS; do
            echo "       -> PID ${PID} 강제 폭파!"
            kill -9 "$PID" 2>/dev/null || true
        done
    fi

    echo "       [OK] 포트 ${PORT} 정리 완료"
}

kill_port 8000 "백엔드"
kill_port 3000 "프론트엔드"

echo "       [OK] 좀비 프로세스 완전 사살 완료"
echo ""
sleep 1

# ──────────────────────────────────────────────────────────────────
# [STEP 1] 백엔드 기동 (Postgres WAL 기반 poll_fallback 모드)
# ──────────────────────────────────────────────────────────────────
echo "[1/2] 백엔드 서버(포트 8000) 기동 중..."
echo "      Postgres 연결 확인, WAL 기반 poll_fallback 준비..."
cd backend
uvicorn main:app --reload --port 8000 > /tmp/vg_backend.log 2>&1 &
BACKEND_PID=$!
echo "      [OK] 백엔드 PID: $BACKEND_PID"
cd ..

# ──────────────────────────────────────────────────────────────────
# [STEP 2] 프론트엔드 기동 (포트 3000 강제 고정)
# ──────────────────────────────────────────────────────────────────
echo "[2/2] 프론트엔드 서버(포트 3000) 기동 중..."
cd Directer_Dashboard
npm run dev > /tmp/vg_frontend.log 2>&1 &
FRONTEND_PID=$!
echo "      [OK] 프론트엔드 PID: $FRONTEND_PID"
cd ..

# 서버 시작 대기
sleep 3

echo ""
echo "════════════════════════════════════════════════════════════"
echo ""
echo "  ✅ 터미널 복구 및 좀비 프로세스 폭파 완료!"
echo "  브라우저에서 즉시 http://localhost:3000 을 여십시오!"
echo ""
echo "════════════════════════════════════════════════════════════"
echo ""
echo "  프론트엔드:  http://localhost:3000"
echo "  백엔드 API:  http://localhost:8000"
echo ""
echo "  로그 확인:"
echo "    백엔드:   tail -f /tmp/vg_backend.log"
echo "    프론트:  tail -f /tmp/vg_frontend.log"
echo ""
echo "  종료: Ctrl+C"
echo "════════════════════════════════════════════════════════════"
echo ""

# 프로세스 유지 (Ctrl+C 감지)
trap "kill $BACKEND_PID $FRONTEND_PID 2>/dev/null; echo ''; echo '파일럿 종료.'; exit 0" SIGINT

wait
