# ══════════════════════════════════════════════════════════════════
# Voice Guard — Cloud Run 배포용 Dockerfile
# 대상: main.py (FastAPI API 서버)
# Whisper/PyTorch는 worker 전용이므로 이 이미지에서 제외
# ══════════════════════════════════════════════════════════════════

FROM python:3.12-slim

# 시스템 의존성 (psycopg2-binary 는 libpq 불필요, binary wheel 사용)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 의존성 먼저 설치 (레이어 캐시 최적화)
COPY requirements-api.txt .
RUN pip install --no-cache-dir -r requirements-api.txt

# 소스 복사 (. env는 제외 — Secret Manager에서 주입)
COPY . .

# Cloud Run은 PORT 환경변수를 자동 주입
ENV PORT=8080

# 비루트 사용자 (보안)
RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app
USER appuser

CMD uvicorn main:app --host 0.0.0.0 --port ${PORT} --workers 2
