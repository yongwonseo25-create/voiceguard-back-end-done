#!/bin/bash
# ══════════════════════════════════════════════════════════════════
# Voice Guard — 원클릭 완전 자동 배포 마스터 스크립트
# 실행: bash auto_deploy_master.sh
# 결과: Cloud Run(백엔드) + Firebase(프론트) 동시 배포 → URL 출력
# ══════════════════════════════════════════════════════════════════

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${PROJECT_ROOT}"

GCP_PROJECT="voice-guard-pilot"
GCP_REGION="asia-northeast3"
SERVICE_NAME="voice-guard-api"
REPO_NAME="voice-guard"
IMAGE="${GCP_REGION}-docker.pkg.dev/${GCP_PROJECT}/${REPO_NAME}/api"
FIREBASE_PROJECT="${GCP_PROJECT}"
TIMESTAMP=$(date +%Y%m%d%H%M%S)

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
ok()   { echo -e "${GREEN}[✓]${NC} $1"; }
info() { echo -e "${CYAN}[→]${NC} $1"; }
warn() { echo -e "${YELLOW}[!]${NC} $1"; }
fail() {
    echo -e "${RED}[✗] 배포 실패: $1${NC}"
    echo ""
    echo "═══ 에러 분석 ════════════════════════════════════════════"
    case "$1" in
        *"gcloud"*)   echo "  원인: Google Cloud SDK 미설치 또는 미인증"
                      echo "  해결: https://cloud.google.com/sdk/docs/install 설치 후 재실행" ;;
        *"docker"*)   echo "  원인: Docker Desktop 미실행"
                      echo "  해결: Docker Desktop을 실행한 후 재시도" ;;
        *"firebase"*) echo "  원인: Firebase CLI 미설치"
                      echo "  해결: npm install -g firebase-tools 실행 후 재시도" ;;
        *"billing"*)  echo "  원인: GCP 빌링 미연결"
                      echo "  해결: https://console.cloud.google.com/billing 에서 연결" ;;
        *"npm"*)      echo "  원인: Node.js 의존성 문제"
                      echo "  해결: cd Directer_Dashboard && npm install 실행 후 재시도" ;;
        *)            echo "  로그를 확인하십시오: /tmp/vg_deploy_${TIMESTAMP}.log" ;;
    esac
    echo "══════════════════════════════════════════════════════════"
    exit 1
}

LOG_FILE="/tmp/vg_deploy_${TIMESTAMP}.log"
exec > >(tee -a "${LOG_FILE}") 2>&1

echo ""
echo "════════════════════════════════════════════════════════════"
echo "  Voice Guard 완전 자동 배포 개시 (${TIMESTAMP})"
echo "════════════════════════════════════════════════════════════"
echo ""

# ── STEP 0: 도구 확인 ────────────────────────────────────────────
info "도구 확인 중..."
command -v gcloud   >/dev/null || fail "gcloud CLI 미설치"
command -v docker   >/dev/null || fail "docker 미설치 또는 미실행"
command -v firebase >/dev/null || fail "firebase CLI 미설치 (npm install -g firebase-tools)"
command -v npm      >/dev/null || fail "npm 미설치"
ok "모든 도구 확인 완료"

# ── STEP 1: GCP 인증 확인 ────────────────────────────────────────
info "GCP 인증 확인 중..."
ACTIVE_ACCOUNT=$(gcloud auth list --filter=status:ACTIVE --format="value(account)" 2>/dev/null | head -1)
if [[ -z "${ACTIVE_ACCOUNT}" ]]; then
    warn "GCP 미로그인 → 브라우저 인증 시작..."
    gcloud auth login || fail "gcloud auth login 실패"
fi
ok "GCP 인증: $(gcloud auth list --filter=status:ACTIVE --format='value(account)' | head -1)"

gcloud config set project "${GCP_PROJECT}" --quiet 2>/dev/null || {
    warn "프로젝트 '${GCP_PROJECT}' 없음 → bootstrap_gcp.sh를 먼저 실행하십시오"
    fail "GCP 프로젝트 미설정"
}

# ── STEP 2: Docker 인증 ──────────────────────────────────────────
info "Artifact Registry Docker 인증 중..."
gcloud auth configure-docker "${GCP_REGION}-docker.pkg.dev" --quiet || fail "docker 인증 실패"
ok "Docker 인증 완료"

# ── STEP 3: 백엔드 이미지 빌드 ──────────────────────────────────
info "백엔드 Docker 이미지 빌드 중..."
docker build \
    -t "${IMAGE}:${TIMESTAMP}" \
    -t "${IMAGE}:latest" \
    ./backend \
    || fail "docker build 실패"
ok "이미지 빌드 완료: ${IMAGE}:${TIMESTAMP}"

# ── STEP 4: 이미지 푸시 ──────────────────────────────────────────
info "Artifact Registry에 이미지 푸시 중..."
docker push "${IMAGE}:${TIMESTAMP}" || fail "docker push 실패"
docker push "${IMAGE}:latest"       || fail "docker push latest 실패"
ok "이미지 푸시 완료"

# ── STEP 5: Cloud Run 배포 ───────────────────────────────────────
info "Cloud Run 배포 중... (약 1-2분 소요)"

# Secret Manager에 등록된 시크릿 목록 자동 감지
AVAILABLE_SECRETS=$(gcloud secrets list --format="value(name)" --quiet 2>/dev/null)
SECRET_FLAGS=""
for SECRET in DATABASE_URL SECRET_KEY B2_KEY_ID B2_APPLICATION_KEY B2_BUCKET_NAME \
    B2_ENDPOINT_URL GEMINI_API_KEY GEMINI_MODEL NOTION_API_KEY NOTION_DATABASE_ID \
    NOTION_HANDOVER_DB_ID NOTION_CARE_RECORD_DB_ID NOTION_HANDOVER_TITLE_PROP \
    SOLAPI_API_KEY SOLAPI_API_SECRET KAKAO_SENDER_KEY \
    ALIMTALK_TPL_NT1 ALIMTALK_TPL_NT2 ALIMTALK_TPL_NT3 \
    ADMIN_PHONE DEFAULT_FACILITY_PHONE ALIMTALK_OVERDUE_MINUTES \
    APP_ENV ALLOWED_ORIGINS; do
    if echo "${AVAILABLE_SECRETS}" | grep -q "^${SECRET}$"; then
        SECRET_FLAGS="${SECRET_FLAGS}${SECRET}=${SECRET}:latest,"
    fi
done
SECRET_FLAGS="${SECRET_FLAGS%,}"  # 끝 쉼표 제거

DEPLOY_CMD="gcloud run deploy ${SERVICE_NAME} \
    --image ${IMAGE}:${TIMESTAMP} \
    --region ${GCP_REGION} \
    --platform managed \
    --allow-unauthenticated \
    --memory 1Gi \
    --cpu 1 \
    --min-instances 0 \
    --max-instances 5 \
    --timeout 60 \
    --quiet"

if [[ -n "${SECRET_FLAGS}" ]]; then
    DEPLOY_CMD="${DEPLOY_CMD} --set-secrets \"${SECRET_FLAGS}\""
fi

eval "${DEPLOY_CMD}" || fail "Cloud Run 배포 실패 (billing 연결 확인)"

# URL 추출
API_URL=$(gcloud run services describe "${SERVICE_NAME}" \
    --region "${GCP_REGION}" \
    --format "value(status.url)" 2>/dev/null)
ok "백엔드 배포 완료: ${API_URL}"

# ── STEP 6: 프론트엔드 빌드 ─────────────────────────────────────
info "프론트엔드 빌드 중 (API URL 주입: ${API_URL})..."
cd Directer_Dashboard
npm ci --silent || fail "npm install 실패"
echo "VITE_API_BASE_URL=${API_URL}" > .env.production
VITE_API_BASE_URL="${API_URL}" npm run build || fail "npm run build 실패"
cd ..
ok "프론트엔드 빌드 완료 (dist/)"

# ── STEP 7: Firebase 인증 확인 ──────────────────────────────────
info "Firebase 인증 확인 중..."
if ! firebase projects:list --json 2>/dev/null | grep -q "${FIREBASE_PROJECT}"; then
    warn "Firebase 미로그인 → 브라우저 인증 시작..."
    firebase login || fail "firebase login 실패"
fi
ok "Firebase 인증 확인"

# ── STEP 8: Firebase 배포 ────────────────────────────────────────
info "Firebase Hosting 배포 중..."
firebase use "${FIREBASE_PROJECT}" --non-interactive 2>/dev/null || \
    firebase use --add 2>/dev/null || true
firebase deploy --only hosting --non-interactive || fail "firebase deploy 실패"
ok "프론트엔드 Firebase 배포 완료"

# ── 완료 보고 ────────────────────────────────────────────────────
echo ""
echo "════════════════════════════════════════════════════════════"
echo ""
echo "  ✅ 터미널 복구 및 좀비 프로세스 폭파 완료!"
echo "  ✅ Cloud Run + Firebase 정식 배포 완료!"
echo ""
echo "  🌐 프론트엔드 (외부망 접속):                             "
echo "     https://voice-guard-pilot.web.app                     "
echo ""
echo "  📡 백엔드 API:                                           "
echo "     ${API_URL}                                            "
echo ""
echo "  📋 API 문서:                                             "
echo "     ${API_URL}/docs                                       "
echo ""
echo "  📄 배포 로그: ${LOG_FILE}                                "
echo ""
echo "════════════════════════════════════════════════════════════"
