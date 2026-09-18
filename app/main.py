"""UPilot-mini 入口：只做一件事 —— 监控本地目录，秒传优先地传到 115，保留目录结构。

用法：
  python main.py                # 跑服务（默认）
  python main.py login          # 扫码登录，cookie 写回 /config/config.yaml
  python main.py --dry-run      # 离线自检：不联网，模拟 115，验证扫描/SHA1/目录/状态机
  python main.py scan-once      # 只扫一遍目录后退出
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
import time

import config as cfgmod
import scanner
import webui
from p115 import P115, StubP115
from store import Store
from uploader import Uploader

logger = logging.getLogger("upilot")


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, str(level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        stream=sys.stdout,
    )


# ---------------- Web 界面 ----------------

def start_http(cfg: dict, store, state: dict):
    """单页仪表盘 + JSON API（实现见 app/webui.py 与 app/static/index.html）。"""
    return webui.serve(cfg, store, state)


# ---------------- 主循环 ----------------

async def run_service(cfg: dict, dry_run: bool) -> int:
    store = Store(cfg["state"]["db"])
    if dry_run:
        manager = StubP115(cfg, store)
        logger.warning("dry-run 模式：不联网、不消耗流量，只验证扫描/去重/SHA1/目录映射/状态机")
    else:
        manager = P115(cfg, store)
    uploader = Uploader(cfg, store, manager)

    state = {"dry_run": dry_run, "scan_rounds": 0}
    http_srv = start_http(cfg, store, state)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:      # Windows 上不可用，忽略
            pass

    interval = max(2, int(cfg["scan"]["interval_seconds"]))
    logger.info("开始监控 %s → 115:%s（扫描间隔 %ds，并发 %d）",
                cfg["paths"]["source"], cfg["paths"]["target_root"], interval,
                int(cfg["upload"]["concurrency"]))

    try:
        while not stop.is_set():
            try:
                if state.pop("scan_now", False):
                    logger.info("收到界面指令：立即扫描")
                scanner.scan_once(store, cfg)
                state["scan_rounds"] += 1
                state["last_scan"] = time.time()
                if state.get("paused"):
                    pass          # 暂停调度：不再派发新任务，在途任务自然跑完
                else:
                    uploader.dispatch()
            except Exception as exc:  # noqa: BLE001
                logger.error("本轮扫描/调度出错：%s", exc)
            # 界面点了「立即扫描」时缩短等待，让操作立刻生效
            wait_s = 0.5 if state.get("scan_now") else interval
            try:
                await asyncio.wait_for(stop.wait(), timeout=wait_s)
            except asyncio.TimeoutError:
                pass
    finally:
        logger.info("收到退出信号，等待在途任务结束…")
        await asyncio.sleep(0.5)
        if http_srv:
            http_srv.shutdown()
    logger.info("已停止。统计：%s", store.counts())
    return 0


def do_login(cfg: dict) -> int:
    from p115client import P115Client

    app = cfg["p115"].get("app") or "qandroid"
    print(f"请用 115 App 扫描终端里的二维码（登录槽位 app={app}）…")
    resp = P115Client.login_with_qrcode(app=app, console_qrcode=True)
    data = resp.get("data") or {}
    cookie = data.get("cookie")
    if isinstance(cookie, dict):
        cookie = "; ".join(f"{k}={v}" for k, v in cookie.items())
    if not cookie:
        print("登录失败，响应：", str(resp)[:300])
        return 1
    cfgmod.save_cookie(str(cookie))
    print("cookie 已写入", cfgmod.CONFIG_PATH, "，重启容器生效：docker compose restart")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="UPilot-mini：秒传优先的 115 上传器")
    parser.add_argument("command", nargs="?", default="run", choices=["run", "login", "scan-once"])
    parser.add_argument("--dry-run", action="store_true", help="离线自检：不联网，模拟 115")
    args = parser.parse_args()

    cfg = cfgmod.load()
    setup_logging(cfg.get("log_level", "INFO"))

    if args.command == "login":
        return do_login(cfg)

    if args.command == "scan-once":
        store = Store(cfg["state"]["db"])
        n = scanner.scan_once(store, cfg)
        print(f"本轮触及 {n} 个文件，当前统计：{store.counts()}")
        return 0

    try:
        return asyncio.run(run_service(cfg, args.dry_run))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
