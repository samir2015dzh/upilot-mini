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

# 等待下一轮时的轮询粒度（秒）。
#
# 为什么要切片：`state["scan_now"]` / `state["reload"]` 是**别的线程**写的
# （webui 用 ThreadingHTTPServer，每个请求一个线程），置进去**不会**唤醒主循环
# 里的 `await stop.wait()`。原来直接 await 整个 interval，用户在等待期间点
# 「立即扫描 / 马上再试」，要等满一整个间隔才动；而面板把
# `scan.interval_seconds` 上限开放到 86400 秒（24 小时），界面提示却写着「马上」。
# 切成 0.5 秒一片，每片醒来看一眼有没有指令，开销只是一次 dict.get。
_WAKE_POLL = 0.5


async def _wait_next_round(stop: asyncio.Event, state: dict, interval: float) -> None:
    """等到下一轮扫描；期间界面下了指令（scan_now / reload）就立即返回。"""
    deadline = time.monotonic() + max(0.0, float(interval))
    while not stop.is_set():
        if state.get("scan_now") or state.get("reload"):
            return                       # 界面刚下的指令，别等满整个间隔
        remain = deadline - time.monotonic()
        if remain <= 0:
            return
        try:
            await asyncio.wait_for(stop.wait(), timeout=min(_WAKE_POLL, remain))
        except asyncio.TimeoutError:
            pass


async def run_service(cfg: dict, dry_run: bool) -> int:
    store = Store(cfg["state"]["db"])

    def build_manager():
        """按当前配置新建 115 客户端；界面改了 cookie 后重建它即可生效。"""
        if dry_run:
            return StubP115(cfg, store)
        return P115(cfg, store)

    manager = build_manager()
    if dry_run:
        logger.warning("dry-run 模式：不联网、不消耗流量，只验证扫描/去重/SHA1/目录映射/状态机")
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

    logger.info("开始监控 %s → 115:%s（扫描间隔 %ds，并发 %d）",
                cfg["paths"]["source"], cfg["paths"]["target_root"],
                max(2, int(cfg["scan"]["interval_seconds"])),
                int(cfg["upload"]["concurrency"]))

    last_cookie_sync = 0.0
    cookie_sig = None        # (mtime_ns, size)：文件没动就连读都不读，只做一次 stat
    try:
        while not stop.is_set():
            try:
                # 从外部程序（symedia 之类）同步 115 cookie：最多 60 秒看一次，变了才写盘重建
                src = str((cfg.get("p115") or {}).get("cookie_from") or "").strip()
                if src and time.monotonic() - last_cookie_sync >= 60:
                    last_cookie_sync = time.monotonic()
                    try:
                        st = os.stat(src)
                        sig = (st.st_mtime_ns, st.st_size)
                    except OSError as exc:
                        sig = None
                        logger.debug("cookie 来源读不到（%s）：%s", src, exc)
                    if sig is not None and sig != cookie_sig:
                        cookie_sig = sig                      # 只有文件真的变了才解析
                        try:
                            fresh = cfgmod.load_cookie_from(src)
                        except Exception as exc:  # noqa: BLE001
                            logger.debug("解析 cookie 来源失败（%s）：%s", src, exc)
                            fresh = ""
                        if fresh and fresh != (cfg["p115"].get("cookie") or ""):
                            cfg["p115"]["cookie"] = fresh
                            cfgmod.save(cfg)
                            state["reload"] = True
                            logger.info("已从 %s 同步 115 cookie（长度 %d），115 客户端将重建", src, len(fresh))
                # 消费界面指令。必须 pop：它表示「上一轮等待期间用户点过」，
                # 本轮已经会立刻扫，留着会让下一轮的空转判断也命中。
                scan_now = state.pop("scan_now", False)
                if scan_now:
                    logger.info("收到界面指令：立即扫描")
                if state.pop("reload", False):
                    logger.info("配置已更新：重建 115 客户端与调度器")
                    manager = build_manager()
                    uploader = Uploader(cfg, store, manager)
                scanner.scan_once(store, cfg)
                state["scan_rounds"] += 1
                state["last_scan"] = time.time()
                if state.get("paused"):
                    pass          # 暂停调度：不再派发新任务，在途任务自然跑完
                else:
                    one = state.pop("process_one", None)
                    if one:
                        logger.info("界面指定立即处理：%s", one)
                        # 必须走 spawn：它带 _active 去重、semaphore 并发上限、
                        # 以及异常兜底（退避/置错误）。直接 await _process 会绕过
                        # 这一整套 —— 文件正在跑时再点「立即处理」，同一文件会被
                        # 两个协程同时走状态机（重复发秒传请求、重复写状态；分块
                        # 场景下两路同时上传同一个文件），且不受并发上限约束。
                        uploader.spawn(one)
                    uploader.dispatch()
            except Exception as exc:  # noqa: BLE001
                logger.error("本轮扫描/调度出错：%s", exc)
            # 间隔每轮重读：界面上改了立刻生效。
            # 「点过立即扫描就缩短等待」不能靠读 state 判断 —— scan_now 在上一处
            # 就被 pop 掉了（同一标志先 pop 后 get ⇒ 那个分支永远不可达），
            # 而且它是别的线程写的，置进去也唤不醒这里的 await。改由
            # _wait_next_round 每 0.5 秒醒一次自己看。
            interval = max(2, int(cfg["scan"]["interval_seconds"]))
            await _wait_next_round(stop, state, interval)
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
