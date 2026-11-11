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
from uploader import Uploader, take_rapid_summary

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

# 同集巡检的间隔（秒）。用户要的是「上传到 115 以后校验那个文件夹里有没有同集的」，
# 那就得**主动定期去查**，而不是只在上传完成那一刻顺带做一次 ——
# 云端已经存在的重复，光等上传事件是永远等不到的。
#
# 开销模型（实测，不是估算）：请求数 ≈ **遍历到的云端目录数**，每目录 1 页
# `fs_files`（limit=1150 足够一页拿全）。真机现状 3 个目录 → 3 次请求；
# 递归最深 3 层。列的是文件名+元数据，不碰文件内容、零流量；CPU 侧是对条目名做
# 正则分组，毫秒级。清完之后每集只剩 1 条，分组处 `len < 2` 直接跳过、不发删除请求。
# 开关 `upload.cleanup_previous_versions` 关着时入口直接返回，一个请求都不发。
# 5 分钟一次（≈ 每轮 3 次请求）对 115 来说可以忽略；剧集变多后请求数线性增长，
# 到时候把这里拉长即可（比如 30 分钟）。
# ★ 巡检**必须 spawn 到后台**（见 `Uploader.spawn_sweep`）：前台 await 会把主循环按住。
SWEEP_INTERVAL = 300.0

# 状态库每隔多久备份一份（秒）。备份文件放在库旁边：state.db.bak，只留一份。
# 库丢了 = 所有文件 SHA1 重算、分块断点作废。备份用 SQLite 在线备份 API，在线程里跑。
BACKUP_INTERVAL = 86400.0


def backup_path(cfg: dict) -> str:
    return str(cfg["state"]["db"]) + ".bak"


def backup_due(cfg: dict, now: float | None = None) -> bool:
    """备份文件不存在或已经超过 BACKUP_INTERVAL 就该备份了（重启也认文件时间，不会每次启动都拷）。"""
    try:
        age = (time.time() if now is None else now) - os.path.getmtime(backup_path(cfg))
    except OSError:
        return True
    return age >= BACKUP_INTERVAL


def beat(state: dict) -> None:
    """主循环心跳（monotonic 秒）。/health 据此判断主循环是不是卡住了（见 webui.health_status）。

    思路参考 https://github.com/a-moiseev/chat_bot_calendar/blob/4674042d30b6ba184925a0f60d4a32c5b0eb7b30/bot/health.py
    （各子系统 tick、探针按「多久没 tick」判 stale）。HTTP 服务是独立线程，
    主循环卡死（115 请求挂住、事件循环被同步调用按住）时它照样回 ok —— 原来的
    /health 发现不了这种「容器 healthy、上传却早停了」的情况。
    """
    state["heartbeat"] = time.monotonic()


async def _wait_next_round(stop: asyncio.Event, state: dict, interval: float) -> None:
    """等到下一轮扫描；期间界面下了指令就立即返回。

    认两个口径：
      1) `scan_now` / `reload` —— 历史标志，向后兼容；
      2) `wake_seq` 变化 —— webui._wake() 每次界面动作都会 +1。
    第 2 条是根本修法：以后新增界面动作只要调 _wake，就不必再来这里补标志名。
    """
    deadline = time.monotonic() + max(0.0, float(interval))
    seen_seq = state.get("wake_seq")
    while not stop.is_set():
        beat(state)                      # 等待期间也在「活着」：心跳不能只在每轮开头打
        if state.get("scan_now") or state.get("reload"):
            return                       # 界面刚下的指令，别等满整个间隔
        if state.get("wake_seq") != seen_seq:
            return                       # 界面动作叫醒（见 webui._wake）
        remain = deadline - time.monotonic()
        if remain <= 0:
            return
        try:
            await asyncio.wait_for(stop.wait(), timeout=min(_WAKE_POLL, remain))
        except asyncio.TimeoutError:
            pass


# 退出时最多等在途任务多少秒（秒传请求、正在算的 SHA1、正在传的分块）。
# 取 8 秒：docker stop 默认 10 秒后 SIGKILL，留 2 秒给收尾。等不完的就取消 ——
# 状态都已落库，分块有断点，下次启动 dispatch 会从中间态接着做。
SHUTDOWN_GRACE = 8.0


async def _drain_background(grace: float) -> tuple[int, int]:
    """等后台任务（上传/秒传/巡检/通知）自然结束，超时的取消。返回 (正常结束数, 被取消数)。"""
    from uploader import _SHARED

    pending = {t for t in _SHARED["bg"] if not t.done()}
    if not pending:
        return 0, 0
    logger.info("等待 %d 个在途任务结束（最多 %.0f 秒）…", len(pending), grace)
    done, late = await asyncio.wait(pending, timeout=max(0.0, grace))
    for t in late:
        t.cancel()
    if late:
        await asyncio.gather(*late, return_exceptions=True)
        logger.warning("%d 个任务没在 %.0f 秒内结束，已取消（状态已落库，下次启动接着做）",
                       len(late), grace)
    return len(done), len(late)


async def run_service(cfg: dict, dry_run: bool, stop: asyncio.Event | None = None) -> int:
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
    beat(state)
    http_srv = start_http(cfg, store, state)

    stop = stop or asyncio.Event()
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
    last_sweep = 0.0          # 上次同集巡检的时间（monotonic）
    last_backup_try = -1e9    # 上次尝试备份状态库的时间（monotonic）
    cookie_sig = None        # (mtime_ns, size)：文件没动就连读都不读，只做一次 stat
    try:
        while not stop.is_set():
            beat(state)
            summary = take_rapid_summary()
            if summary["checked"]:
                # 逐个文件的「未命中」已降为 DEBUG，这里每轮一行汇总，INFO 里照样看得出在干活
                logger.info("秒传校验汇总：上一轮校验 %d 个，命中 %d 个，未命中 %d 个",
                            summary["checked"], summary["hit"], summary["miss"])
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
                # 扫描放到线程里：os.walk + 逐个 stat 在几万个文件 / 网络挂载上要几秒到
                # 几十秒，原来同步跑在事件循环里，期间在途的秒传、SHA1、巡检全被按住。
                # 仍然 await 它跑完再 dispatch，所以「先扫后派发」的顺序不变。
                await asyncio.to_thread(scanner.scan_once, store, cfg)
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
                # 失败（磁盘满 / 权限）每小时最多重试一次，别每轮都拷
                if time.monotonic() - last_backup_try >= 3600 and backup_due(cfg):
                    last_backup_try = time.monotonic()
                    try:
                        size = await asyncio.to_thread(store.backup, backup_path(cfg))
                        logger.info("状态库已备份：%s（%.1f MB）", backup_path(cfg), size / 1024 / 1024)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("状态库备份失败（不影响上传）：%s", exc)
                # 同集巡检（用户诉求：「上传到 115 以后校验那个文件夹里有没有相同集数的视频」）。
                # **纯云端视角**：只用「库里出现过的 target_dir + paths.target_root」当
                # 入口路径，靠 115 条目自身的时间字段判「谁是后传的」—— 因为用户明确
                # 「本地肯定没有，本地文件在秒传完已经被删掉了」。
                # 独立于上传事件，每 SWEEP_INTERVAL 秒主动核一次；幂等。
                # 放在 paused 分支外面：巡检是「把云端已有的重复收敛掉」，不是派发上传任务。
                # ★ 必须 spawn 到后台：原来这里是 `await`，巡检一跑就把主循环按住
                # （实测 202 个目录时扫描间隔从 2s 变成 105s），其间不扫新文件、不派发任务。
                if time.monotonic() - last_sweep >= SWEEP_INTERVAL:
                    last_sweep = time.monotonic()
                    uploader.spawn_sweep()
                # 「界面动作立刻巡检」：点「立即扫描 / 核对同集」也顺手核一遍，
                # 免得用户刚传完还要等满 5 分钟才看到旧版被清掉。
                if state.pop("sweep_now", False):
                    if uploader.spawn_sweep():
                        logger.info("同集巡检（界面触发）：已开始核对云端同集")
                    else:
                        logger.info("同集巡检（界面触发）：上一轮还在跑，本次跳过")
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
        # 原来这里只 sleep 0.5 秒、日志却写「等待在途任务结束」，然后 asyncio.run 收尾
        # 把所有任务直接取消：算到一半的 SHA1 白算、正在传的分块白传。
        # 现在：主循环已经停了（不再派发新任务），在途的给 SHUTDOWN_GRACE 秒自然结束。
        # 被外部取消（不是收到退出信号）时不等，直接走取消。
        if stop.is_set():
            logger.info("收到退出信号，停止派发新任务")
            try:
                await _drain_background(SHUTDOWN_GRACE)
            except Exception as exc:  # noqa: BLE001
                logger.warning("等待在途任务时出错：%s", exc)
        if http_srv:
            http_srv.shutdown()
    logger.info("已停止。统计：%s", store.counts())
    return 0


QR_IMAGE_URL = "https://qrcodeapi.115.com/api/1.0/web/1.0/qrcode?uid={uid}"
QR_MAX_ROUNDS = 3          # 二维码过期后自动换新的次数上限
QR_ROUND_SECONDS = 300     # 单张二维码最多等这么久（115 一般更早就判过期）


def _qr_show(token: dict) -> None:
    """把二维码打到终端，同时给出官方图片链接（终端里的字符码扫不上时用）。"""
    content = token.get("qrcode") or ("https://115.com/scan/dg-" + token["uid"])
    try:
        from qrcode import QRCode

        qr = QRCode(border=2)
        qr.add_data(content)
        # tty 下用 ANSI 强制「白底黑码」；深色终端里不带颜色的字符码是反色的，手机常识别不了
        qr.print_ascii(tty=sys.stdout.isatty())
    except Exception as exc:  # noqa: BLE001
        print(f"（终端画不出二维码：{exc}）")
    print("终端里的码扫不上（发虚 / 反色 / 被折行）就用浏览器打开这张官方图片再扫：")
    print("  " + QR_IMAGE_URL.format(uid=token["uid"]))
    sys.stdout.flush()


def _qr_wait(client_cls, token: dict) -> str:
    """轮询到有结论为止，返回 signed / expired / canceled / timeout / error:<说明>。"""
    deadline = time.monotonic() + QR_ROUND_SECONDS
    seen = None
    fails = 0
    while time.monotonic() < deadline:
        try:
            resp = client_cls.login_qrcode_scan_status(
                {k: token[k] for k in ("uid", "time", "sign")}, timeout=60)
            fails = 0
        except Exception as exc:  # noqa: BLE001  长轮询超时是常态，连续失败才算网络有问题
            fails += 1
            if fails >= 10:
                return f"error:连不上 115 的扫码状态接口（{type(exc).__name__}: {str(exc)[:120]}）"
            time.sleep(1)
            continue
        status = (resp.get("data") or {}).get("status", 0)
        if status != seen:
            seen = status
            if status == 1:
                print("已扫码 → 请在手机上点「确认登录」…", flush=True)
        if status == 2:
            return "signed"
        if status == -1:
            return "expired"
        if status == -2:
            return "canceled"
        if status not in (0, 1):
            return f"error:115 返回了没见过的扫码状态：{str(resp)[:200]}"
    return "timeout"


def do_login(cfg: dict) -> int:
    from p115client import P115Client

    app = cfg["p115"].get("app") or "115ipad"
    label = cfgmod.app_label(app)
    print(f"登录身份：{label}（p115.app={app}）。登录成功会把这个端上原有的登录挤下线。")
    for rnd in range(1, QR_MAX_ROUNDS + 1):
        try:
            resp = P115Client.login_qrcode_token(timeout=30)
            token = resp["data"]
            token["uid"], token["time"], token["sign"]  # noqa: B018  缺字段就当取码失败
        except Exception as exc:  # noqa: BLE001
            print(f"[第 1 步·取二维码] 失败：{type(exc).__name__}: {str(exc)[:200]}")
            print("容器连不上 qrcodeapi.115.com，先检查容器的网络 / 代理 / DNS。")
            return 1
        print(f"\n请用 115 App 扫码（第 {rnd}/{QR_MAX_ROUNDS} 张，约几分钟内有效）：")
        _qr_show(token)
        result = _qr_wait(P115Client, token)
        if result == "signed":
            break
        if result == "canceled":
            print("[第 2 步·手机确认] 手机上点了取消，登录中止。")
            return 1
        if result.startswith("error:"):
            print("[第 2 步·等待扫码] " + result[6:])
            return 1
        print("二维码已过期，换一张新的…" if rnd < QR_MAX_ROUNDS else "二维码多次过期，登录中止。")
    else:
        return 1

    try:
        resp = P115Client.login_qrcode_scan_result(token["uid"], app=app, timeout=30)
    except Exception as exc:  # noqa: BLE001
        print(f"[第 3 步·换取 cookie] 请求失败：{type(exc).__name__}: {str(exc)[:200]}")
        return 1
    data = resp.get("data") or {}
    cookie = data.get("cookie")
    if isinstance(cookie, dict):
        cookie = "; ".join(f"{k}={v}" for k, v in cookie.items())
    if not cookie:
        print(f"[第 3 步·换取 cookie] 115 拒绝了：code={resp.get('code') or resp.get('errno')} "
              f"{resp.get('message') or resp.get('error') or ''}")
        print(f"手机已确认但身份「{label}」换不到 cookie，可在设置页换一个客户端身份再试。")
        return 1
    cfgmod.save_cookie(str(cookie))
    print("登录成功，cookie 已写入", cfgmod.CONFIG_PATH, "，重启容器生效：docker compose restart")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="UPilot-mini：秒传优先的 115 上传器")
    parser.add_argument("command", nargs="?", default="run", choices=["run", "login", "scan-once"])
    parser.add_argument("--dry-run", action="store_true", help="离线自检：不联网，模拟 115")
    args = parser.parse_args()

    try:
        cfg = cfgmod.load()
    except cfgmod.ConfigError as exc:
        print(exc, file=sys.stderr)
        return 2
    setup_logging(cfg.get("log_level", "INFO"))
    for msg in cfgmod.startup_warnings(cfg):
        logging.getLogger("upilot").warning("%s", msg)

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
