"""容器健康检查：读配置里的 HTTP 端口，请求 /health。"""
from __future__ import annotations

import sys
import urllib.error
import urllib.request

import config as cfgmod


def _candidates(cfg: dict) -> list[int]:
    """要探测的端口，按优先级排列。

    先试「进程实际绑定的端口」（webui.serve 写下的 .http_port），再试配置里的端口。
    理由：http.port 是「改了要重建容器」的项，但保存会立刻落盘，而绑定不会变 ——
    只按配置查的话，用户一改端口（还没重建容器）容器就立刻 unhealthy，
    而服务其实好好地在旧端口跑。
    """
    out: list[int] = []
    try:
        real = int(cfgmod.PORT_FILE.read_text(encoding="utf-8").strip())
        if 0 < real < 65536:
            out.append(real)
    except (OSError, ValueError):
        pass
    try:
        want = int(cfg["http"].get("port", 9844))
        if 0 < want < 65536 and want not in out:
            out.append(want)
    except (TypeError, ValueError):
        pass
    return out or [9844]


def main() -> int:
    cfg = cfgmod.load()
    if not cfg.get("http", {}).get("enabled", True):
        print("http disabled; skip")
        return 0
    last = ""
    for port in _candidates(cfg):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=5) as resp:
                body = resp.read()
            if body.strip() == b"ok":
                return 0
            last = f"端口 {port} 返回了非 ok 内容：{body[:60]!r}"
        except urllib.error.HTTPError as exc:
            # 503 = 服务在、主循环卡住了（见 webui.health_status）；换端口也没用，直接判失败
            detail = exc.read()[:120].decode("utf-8", "replace")
            print(f"health check failed: 端口 {port} 返回 {exc.code}：{detail}")
            return 1
        except Exception as exc:  # noqa: BLE001
            last = f"端口 {port} 连不上：{exc}"
    print("health check failed:", last)
    return 1


if __name__ == "__main__":
    sys.exit(main())
