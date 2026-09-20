"""秒传命中 / 上传完成后，通知别的程序（webhook）。

默认按 CloudDrive2 的 file_notify 契约发：symedia 的「链接同步」在 Webhook 模式下
就是靠这个端点收事件（/api/v1/webhook/clouddrive2/file_notify），收到即对该文件做
目录树同步、建 strm，不必等它自己的监控轮询。

一个要点：symedia 认的是 CD2 挂载视图的路径（例如
/CloudNAS/CloudDrive/115open/TVs/…），而我们知道的是 115 网盘路径（/TVs/…），
所以有个 path_prefix 做前缀改写。

发送是同步阻塞的，调用方用 asyncio.to_thread 包一下；失败只记日志，绝不影响上传本身。
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request

logger = logging.getLogger("upilot.notify")

MODES = ("rapid_hit", "uploaded", "both")


class Notifier:
    def __init__(self, cfg: dict):
        n = dict(cfg.get("notify") or {})
        self.enabled = bool(n.get("enabled"))
        self.url = str(n.get("url") or "").strip()
        self.query = str(n.get("query") or "").strip().lstrip("?")
        self.prefix = str(n.get("path_prefix") or "").strip().rstrip("/")
        self.auth = str(n.get("auth") or "").strip()
        self.on = str(n.get("on") or "rapid_hit").strip()
        if self.on not in MODES:
            self.on = "rapid_hit"
        try:
            self.timeout = float(n.get("timeout") or 10)
        except (TypeError, ValueError):
            self.timeout = 10.0
        try:
            self.retries = max(0, int(n.get("retries") or 0))
        except (TypeError, ValueError):
            self.retries = 0
        self.device = str(n.get("device_name") or "upilot-mini").strip() or "upilot-mini"

    @property
    def ready(self) -> bool:
        return self.enabled and bool(self.url)

    # ---------- 内容构造 ----------

    def map_path(self, cloud_path: str) -> str:
        """115 网盘路径 → 对方认识的路径（加前缀）。"""
        p = "/" + str(cloud_path or "").lstrip("/")
        return (self.prefix + p) if self.prefix else p

    def should_fire(self, mode: str) -> bool:
        """mode 取 'rapid_hit'（秒传命中）或 'uploaded'（分块上传完成）。"""
        return self.on == "both" or self.on == mode

    def build_body(self, cloud_path: str, action: str = "create") -> dict:
        """CloudDrive2 file_notify 的 body：data 里一项一个文件变更。"""
        now = str(int(time.time()))
        return {
            "device_name": self.device,
            "user_name": self.device,
            "version": "upilot-mini",
            "event_category": "file",
            "event_name": "notify",
            "event_time": now,
            "send_time": now,
            "data": [{
                "action": action,                 # create / delete / rename
                "is_dir": "false",                # 我们只报文件
                "source_file": self.map_path(cloud_path),
                "destination_file": "",
            }],
        }

    def full_url(self) -> str:
        return self.url + (("?" + self.query) if self.query else "")

    # ---------- 发送 ----------

    def send(self, cloud_path: str, mode: str) -> tuple[bool, str]:
        """同步发送；返回 (是否成功, 说明)。调用方负责包进线程。"""
        if not self.ready:
            return False, "未启用"
        if not self.should_fire(mode):
            return False, f"当前只通知 on={self.on}"
        url = self.full_url()
        body = json.dumps(self.build_body(cloud_path), ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json", "User-Agent": "upilot-mini"}
        if self.auth:
            headers["Authorization"] = self.auth
        last = ""
        for attempt in range(self.retries + 1):
            try:
                req = urllib.request.Request(url, data=body, headers=headers, method="POST")
                # 内网通知不该走代理：容器里若设了 http_proxy，这里显式绕开
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                with opener.open(req, timeout=self.timeout) as resp:
                    code = resp.status
                    resp.read()
                if 200 <= code < 300:
                    target = urllib.parse.urlsplit(url).netloc or url
                    logger.info("已通知 %s：%s（%s）", target, self.map_path(cloud_path), mode)
                    return True, f"HTTP {code}"
                last = f"HTTP {code}"
            except urllib.error.HTTPError as exc:
                try:
                    detail = exc.read()[:160].decode("utf-8", "replace")
                except Exception:  # noqa: BLE001
                    detail = ""
                last = f"HTTP {exc.code} {detail}".strip()
            except Exception as exc:  # noqa: BLE001
                # 读超时说明请求已经送到了、只是对方忙着处理没及时回执（symedia 收到后
                # 会立刻建 strm + 刮削，回得比较慢）—— 这种情况既不算失败也不重发，
                # 否则它会为同一个文件白跑一遍。
                if isinstance(exc, TimeoutError) or "timed out" in str(exc).lower():
                    target = urllib.parse.urlsplit(url).netloc or url
                    logger.info("已通知 %s：%s（%s，对方未及时回执）",
                                target, self.map_path(cloud_path), mode)
                    return True, "sent(no ack)"
                last = f"{type(exc).__name__}: {exc}"
            if attempt < self.retries:
                time.sleep(1.5 * (attempt + 1))
        logger.warning("通知失败（%s）：%s", self.map_path(cloud_path), last)
        return False, last
