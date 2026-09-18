FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY app/ /app/app/
COPY config/config.example.yaml /app/config/config.example.yaml

# 非 root 运行；/config 与 /media 由 compose 挂载
RUN useradd -m -u 1000 appuser && mkdir -p /config /media/source && chown -R appuser:appuser /app /config /media
USER appuser

VOLUME ["/config", "/media"]
EXPOSE 9844

HEALTHCHECK --interval=60s --timeout=10s --start-period=20s --retries=3 \
    CMD ["python", "/app/app/healthcheck.py"]

ENTRYPOINT ["python", "-u", "/app/app/main.py"]
