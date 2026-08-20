FROM python:3.12-slim

WORKDIR /app

RUN useradd -m -u 10001 tankai
COPY requirements.txt .
RUN pip install --no-cache-dir -U pip \
    && pip install --no-cache-dir -r requirements.txt

COPY tankai ./tankai
COPY requirements.txt DEPLOY.md README.md ./

RUN mkdir -p /app/data && chown -R tankai:tankai /app
USER tankai

ENV TANKAI_HOST=0.0.0.0
ENV TANKAI_PORT=8765
ENV TANKAI_LLM=mock
ENV TANKAI_EMBEDDER=hashing
ENV TANKAI_RUN_STORE=/tmp/tankai_runs.jsonl
ENV PYTHONUNBUFFERED=1

EXPOSE 8765
CMD ["python", "-m", "tankai.web.server"]
