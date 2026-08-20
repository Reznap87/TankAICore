FROM python:3.12-slim

WORKDIR /app

RUN useradd -m -u 10001 tankai
COPY requirements.txt ./
RUN pip install --no-cache-dir -U pip \
    && pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir openai anthropic

COPY tankai ./tankai
COPY requirements.txt DEPLOY.md README.md .env.example ./

RUN mkdir -p /app/data && chown -R tankai:tankai /app
USER tankai

ENV TANKAI_HOST=0.0.0.0 \
    TANKAI_PORT=8765 \
    TANKAI_AUTH_MODE=session \
    TANKAI_DATA_ROOT=/app/data \
    TANKAI_AUTH_DB=/app/data/auth.db \
    TANKAI_COOKIE_SECURE=1 \
    TANKAI_ALLOW_REGISTRATION=0 \
    PYTHONUNBUFFERED=1

EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import json,urllib.request; d=json.load(urllib.request.urlopen('http://127.0.0.1:8765/api/health',timeout=3)); assert d['ok']" || exit 1
CMD ["python", "-m", "tankai.web.server"]
