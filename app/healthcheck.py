"""容器健康检查：读配置里的 HTTP 端口，请求 /health。"""
from __future__ import annotations

import sys
import urllib.request

import config as cfgmod


def main() -> int:
    cfg = cfgmod.load()
    if not cfg.get("http", {}).get("enabled", True):
        print("http disabled; skip")
        return 0
    port = int(cfg["http"].get("port", 9844))
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=5) as resp:
            body = resp.read()
        return 0 if body.strip() == b"ok" else 1
    except Exception as exc:  # noqa: BLE001
        print("health check failed:", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
