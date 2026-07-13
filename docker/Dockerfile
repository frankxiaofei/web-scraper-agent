# web_scraper 前后端一体镜像（FastAPI + Jinja2，三端口 UI + 可选调度器）
# 基础镜像须来自 reg.hdec.com（路径以院内 registry 为准，可通过 --build-arg BASE_IMAGE= 覆盖）
ARG BASE_IMAGE=reg.hdec.com/library/python:3.11-slim
FROM ${BASE_IMAGE}

WORKDIR /app

# 运行时工具 + Playwright Chromium 系统依赖
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        curl \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY pip.conf /etc/pip.conf
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

ENV PLAYWRIGHT_DOWNLOAD_HOST=https://npmmirror.com/mirrors/playwright \
    PLAYWRIGHT_BROWSERS_PATH=/app/.playwright \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOST_UI=0.0.0.0

RUN python -m playwright install-deps chromium \
    && python -m playwright install chromium

COPY . .

RUN useradd --create-home --uid 1000 --shell /bin/bash appuser \
    && mkdir -p logs data \
    && chown -R appuser:appuser /app

COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

USER appuser

EXPOSE 8090 8091 8092

HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8090/ >/dev/null || exit 1

ENTRYPOINT ["/entrypoint.sh"]
