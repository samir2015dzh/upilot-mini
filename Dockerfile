FROM python:3.12-slim

# MALLOC_ARENA_MAX=2：glibc 默认每个线程最多建 8×CPU 个内存 arena，Web 线程 + 哈希/扫描线程
# 一多，空闲内存还回不去、RSS 慢慢涨。限成 2 个对这种 I/O 密集的小服务没有性能影响。
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai \
    MALLOC_ARENA_MAX=2

WORKDIR /app

COPY requirements.txt constraints.txt /app/
RUN pip install --no-cache-dir -r /app/requirements.txt -c /app/constraints.txt

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
