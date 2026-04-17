#!/bin/bash
# ══════════════════════════════════════════════════════════════════
# Voice Guard — GCP 인프라 1회 부트스트랩
# 실행: bash scripts/bootstrap_gcp.sh
# 결과: GCP 프로젝트/SA/Secret/Artifact Registry 모두 자동 생성
#       GitHub Actions에 필요한 2개 시크릿을 자동 등록
# ══════════════════════════════════════════════════════════════════

set -euo pipefail

# ── 설정 ──────────────────────────────────────────────────────────
GCP_PROJECT="upbeat-aura-484502-r2"
GCP_REGION="asia-northeast3"
FIREBASE_PROJECT="upbeat-aura-484502-r2"
SA_NAME="voice-guard-deployer"
SA_EMAIL="${SA_NAME}@${GCP_PROJECT}.iam.gserviceaccount.com"
REPO_NAME="voice-guard"
ENV_FILE="$(dirname "$0")/../backend/.env"
GITHUB_REPO="yongwonseo25-create/voiceguard-back-end-done"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
ok()   { echo -e "${GREEN}[OK]${NC} $1"; }
warn() { echo -e "${YELLOW}[!!]${NC} $1"; }
fail() { echo -e "${RED}[ERR]${NC} $1"; exit 1; }

echo ""
echo "════════════════════════════════════════════════════════════"
echo "  Voice Guard GCP 인프라 부트스트랩 개시"
echo "════════════════════════════════════════════════════════════"
echo ""

# ── 도구 확인 ────────────────────────────────────────────────────
command -v gcloud    >/dev/null || fail "gcloud CLI 미설치. https://cloud.google.com/sdk 에서 설치"
command -v firebase  >/dev/null || fail "firebase CLI 미설치. npm install -g firebase-tools"
command -v gh        >/dev/null || warn "gh CLI 미설치 — GitHub 시크릿 자동 등록 건너뜀 (수동 등록 필요)"

# ── GCP 인증 확인 ────────────────────────────────────────────────
if ! gcloud auth list --filter=status:ACTIVE --format="value(account)" 2>/dev/null | grep -q "@"; then
    echo "🔐 GCP 로그인 필요 (브라우저가 열립니다)..."
    gcloud auth login --no-launch-browser 2>/dev/null || gcloud auth login
fi
ok "GCP 인증 확인"

# ── 프로젝트 생성/선택 ───────────────────────────────────────────
if ! gcloud projects describe "${GCP_PROJECT}" &>/dev/null; then
    warn "프로젝트 '${GCP_PROJECT}' 없음 → 생성 중..."
    gcloud projects create "${GCP_PROJECT}" --name="Voice Guard Pilot"
    ok "프로젝트 생성: ${GCP_PROJECT}"
else
    ok "프로젝트 확인: ${GCP_PROJECT}"
fi
gcloud config set project "${GCP_PROJECT}" --quiet

# ── 빌링 확인 (자동화 불가 — 안내만) ────────────────────────────
BILLING=$(gcloud billing projects describe "${GCP_PROJECT}" --format="value(billingEnabled)" 2>/dev/null || echo "false")
if [[ "$BILLING" != "True" ]]; then
    echo ""
    echo "════════════════════════════════════════════════════════════"
    warn "빌링 미연결 상태입니다."
    echo "   아래 URL에서 프로젝트에 빌링 계정을 연결하십시오 (30초):"
    echo "   https://console.cloud.google.com/billing/linkedaccount?project=${GCP_PROJECT}"
    echo ""
    read -p "   빌링 연결 완료 후 Enter를 누르십시오..." _
    echo "════════════════════════════════════════════════════════════"
fi

# ── API 활성화 ────────────────────────────────────────────────────
echo "API 활성화 중..."
gcloud services enable \
    run.googleapis.com \
    secretmanager.googleapis.com \
    artifactregistry.googleapis.com \
    cloudbuild.googleapis.com \
    iam.googleapis.com \
    --quiet
ok "API 활성화 완료"

# ── Artifact Registry 저장소 ─────────────────────────────────────
if ! gcloud artifacts repositories describe "${REPO_NAME}" \
    --location="${GCP_REGION}" &>/dev/null; then
    gcloud artifacts repositories create "${REPO_NAME}" \
        --repository-format=docker \
        --location="${GCP_REGION}" \
        --description="Voice Guard API" \
        --quiet
    ok "Artifact Registry 생성: ${REPO_NAME}"
else
    ok "Artifact Registry 확인: ${REPO_NAME}"
fi

# ── 서비스 계정 생성 ─────────────────────────────────────────────
if ! gcloud iam service-accounts describe "${SA_EMAIL}" &>/dev/null; then
    gcloud iam service-accounts create "${SA_NAME}" \
        --display-name="Voice Guard Deployer" \
        --quiet
    ok "서비스 계정 생성: ${SA_EMAIL}"
else
    ok "서비스 계정 확인: ${SA_EMAIL}"
fi

# IAM 역할 부여
for ROLE in \
    "roles/run.admin" \
    "roles/artifactregistry.admin" \
    "roles/secretmanager.secretAccessor" \
    "roles/iam.serviceAccountUser" \
    "roles/cloudbuild.builds.builder" \
    "roles/serviceusage.serviceUsageAdmin"; do
    gcloud projects add-iam-policy-binding "${GCP_PROJECT}" \
        --member="serviceAccount:${SA_EMAIL}" \
        --role="${ROLE}" \
        --quiet 2>/dev/null || true
done
ok "IAM 역할 부여 완료"

# SA 키 발급
SA_KEY_FILE="/tmp/vg_sa_key.json"
gcloud iam service-accounts keys create "${SA_KEY_FILE}" \
    --iam-account="${SA_EMAIL}" \
    --quiet
ok "서비스 계정 키 발급: ${SA_KEY_FILE}"

# ── .env → Secret Manager 자동 등록 (idempotent) ─────────────────
echo ""
echo "Secret Manager 등록 중..."

# .env 파싱: 마지막 비어있지 않은 값 우선
declare -A ENV_VARS
while IFS='=' read -r KEY REST; do
    [[ -z "$KEY" || "$KEY" =~ ^# ]] && continue
    VAL="${REST}"
    # 따옴표 제거
    VAL="${VAL#\"}" ; VAL="${VAL%\"}"
    VAL="${VAL#\'}" ; VAL="${VAL%\'}"
    [[ -n "$VAL" ]] && ENV_VARS["$KEY"]="$VAL"
done < "${ENV_FILE}"

for KEY in "${!ENV_VARS[@]}"; do
    VAL="${ENV_VARS[$KEY]}"
    if gcloud secrets describe "${KEY}" --quiet &>/dev/null; then
        # 기존 시크릿 → 새 버전 추가
        echo -n "${VAL}" | gcloud secrets versions add "${KEY}" --data-file=- --quiet
    else
        # 신규 시크릿 생성
        echo -n "${VAL}" | gcloud secrets create "${KEY}" --data-file=- --quiet
    fi
    echo "  [secret] ${KEY}"
done
ok "Secret Manager 등록 완료 (${#ENV_VARS[@]}개)"

# ── Firebase 서비스 계정 생성 ─────────────────────────────────────
FB_SA_NAME="firebase-deployer"
FB_SA_EMAIL="${FB_SA_NAME}@${FIREBASE_PROJECT}.iam.gserviceaccount.com"

if ! gcloud iam service-accounts describe "${FB_SA_EMAIL}" --project="${FIREBASE_PROJECT}" &>/dev/null; then
    gcloud iam service-accounts create "${FB_SA_NAME}" \
        --project="${FIREBASE_PROJECT}" \
        --display-name="Firebase Deployer" \
        --quiet
fi
for ROLE in \
    "roles/firebase.admin" \
    "roles/firebasehosting.admin"; do
    gcloud projects add-iam-policy-binding "${FIREBASE_PROJECT}" \
        --member="serviceAccount:${FB_SA_EMAIL}" \
        --role="${ROLE}" \
        --quiet 2>/dev/null || true
done

FB_KEY_FILE="/tmp/vg_fb_key.json"
gcloud iam service-accounts keys create "${FB_KEY_FILE}" \
    --iam-account="${FB_SA_EMAIL}" \
    --project="${FIREBASE_PROJECT}" \
    --quiet
ok "Firebase 서비스 계정 키 발급 (프로젝트: ${FIREBASE_PROJECT})"

# ── GitHub 시크릿 자동 등록 ──────────────────────────────────────
if command -v gh &>/dev/null && gh auth status &>/dev/null; then
    echo ""
    echo "GitHub 시크릿 자동 등록 중..."
    gh secret set GCP_SA_KEY \
        --repo "${GITHUB_REPO}" \
        --body "$(base64 -w0 "${SA_KEY_FILE}")"
    gh secret set FIREBASE_SERVICE_ACCOUNT \
        --repo "${GITHUB_REPO}" \
        --body "$(cat "${FB_KEY_FILE}")"
    ok "GitHub 시크릿 자동 등록 완료 (GCP_SA_KEY, FIREBASE_SERVICE_ACCOUNT)"
else
    echo ""
    warn "gh CLI 미인증 — 아래 2개 값을 GitHub에 수동 등록하십시오:"
    echo "   https://github.com/${GITHUB_REPO}/settings/secrets/actions"
    echo ""
    echo "── GCP_SA_KEY (아래 내용 전체 복사) ──"
    base64 -w0 "${SA_KEY_FILE}"
    echo ""
    echo "── FIREBASE_SERVICE_ACCOUNT (아래 내용 전체 복사) ──"
    cat "${FB_KEY_FILE}"
    echo ""
fi

# 임시 키 파일 삭제
rm -f "${SA_KEY_FILE}" "${FB_KEY_FILE}"

echo ""
echo "════════════════════════════════════════════════════════════"
ok "부트스트랩 완료!"
echo ""
echo "  이제부터 main 브랜치에 git push 하는 순간"
echo "  Cloud Run + Firebase가 자동 배포됩니다."
echo ""
echo "  프론트엔드: https://voice-guard-app.web.app"
echo "  백엔드 API: https://voice-guard-api-[hash]-an.a.run.app"
echo "════════════════════════════════════════════════════════════"
