"""单文件状态机：预处理(SHA1) → 秒传(可重试) → 分块上传。状态、SHA1、断点全部落库。

待处理 →(稳定+算 SHA1)→ 待秒传 → 秒传中 ─┬ 命中 → 已上传
                                        ├ 小文件未命中 → 待上传
                                        └ 大文件未命中 → 待重试 →(到期)秒传中 …
重试耗尽 → 待上传 → 上传中 → 已上传 / 错误
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import mmap
import os
import threading
import time

from notify import Notifier
from p115 import P115 as _P115, episode_key, is_auth_error, resolve_upload_target

logger = logging.getLogger("upilot.upload")
# SHA1 每次读多少。实测（2GB 热缓存，带 SHA-NI）1MB 与 8MB 吞吐一样（都 1.60 GB/s，
# 卡在 OpenSSL），但 8MB 每路哈希峰值多占约 16MB 内存（bytes 对象 + 一次临时拷贝），
# 默认并发 2 就是 32MB。改成 1MB 并复用同一块缓冲区（readinto）。
CHUNK = 1024 * 1024

# 「同一批」容差（秒）：另一个版本与当前版本的**内容入队时间**（`created_at`）
# 相差在范围内，才认为两者是「同一批新下的正当版本」，需要互相保护。
#
# 它是把「洗板重下」与「同批两个版本」分开的那把尺：
#   - 同批（第 37 处的场景）：A、B 在同一轮扫描里稳定、并发传完，`created_at` 只差
#     几十秒 ⇒ 两个都是正当版本 ⇒ 都保护（否则互删，云端同集一个不剩）；
#   - 洗板重下：旧版的本地文件**可能还在**（用户「重新下了一遍」时常见），但它是几天前
#     入队的 ⇒ 老版本 ⇒ **不保护**，照旧清理。
#
# 【坑】必须用 `created_at`，**不能**用 `updated_at`：后者在每次状态写入（scanner 推进
# `stable_count`、秒传命中改 message、用户点「重新排队」…）时都会被刷新成「现在」，
# 于是老版本的 updated_at 看起来和新的差不多 ⇒ 判据失效、旧版又被保护住。
# `created_at` 只在 `store.add()`（scanner 发现新内容 → INSERT OR REPLACE）时写入，
# 语义正是「这条记录代表的那份内容是什么时候到本地的」。
SAME_BATCH_SECONDS = 1800.0

# 同集巡检列云端目录时的分页硬上限（页数）。
# 【坑】别用 p115.MAX_DIR_PAGES —— 那个常量不存在（p115 里叫 MAX_DIR_ITEMS，语义是
# 「最多收多少条目」）；写成它会让 `_cloud_page` 一被调用就 NameError。
# py_compile 查不出这类错误（运行时才炸），改完这类代码必须真跑一次。
MAX_SWEEP_PAGES = 200

# 跨实例共享的调度状态。
#
# main.py 在改配置时（upload./retry./notify. 任意一项、或 115 cookie 同步）会重建
# Uploader，而**旧实例已经派发的后台任务仍在事件循环里跑**。这几样如果留在实例上：
#   - _active 为空     → 同一个文件被新旧两个协程同时处理（实测秒传请求发 2 次）；
#   - sem 各算一份     → 并发上限直接翻倍（旧 1 / 新 2）；
#   - _bg 为空         → 旧实例被覆盖后，它的后台任务失去强引用，可能被 GC 回收；
#   - _err_streak 清空 → 连续异常计数作废，退避重试永远到不了阈值。
# 所以统一放模块级共享。sem 只在并发数真的变了时重建（容量要跟着配置走）。
_SHARED: dict = {"active": set(), "bg": set(), "err_streak": {}, "sem": None,
                 "sem_n": -1, "sweep_running": False,
                 # 本轮秒传校验计数（主循环每轮取走一次、打一条汇总；见 take_rapid_summary）
                 "rapid_round": {"checked": 0, "hit": 0, "miss": 0},
                 # 115 登录失效断路器：None = 正常；否则 {"since","message","next_probe"}。
                 # 失效期间 dispatch 只放行「不联网」的状态，每 AUTH_PROBE_SECONDS 才试探一次。
                 "auth_fail": None}

# 登录失效后多久再试探一次 115（同时也是该文件下次重试的间隔）
AUTH_PROBE_SECONDS = 300.0
# 失效期间仍然照常派发的状态：只算 SHA1、不碰 115
_OFFLINE_STATES = ("待处理", "处理中")


def auth_status() -> dict | None:
    """给面板用：115 登录当前是否失效（None = 正常）。"""
    cur = _SHARED["auth_fail"]
    return None if not cur else {"since": cur["since"], "message": cur["message"]}


def take_rapid_summary() -> dict:
    """取走并清零上一轮的秒传校验计数。主循环每轮调一次，有校验才打一行汇总日志 ——
    逐个文件的「未命中」降成 DEBUG 之后，INFO 里仍能看出调度在正常工作。"""
    cur = _SHARED["rapid_round"]
    _SHARED["rapid_round"] = {"checked": 0, "hit": 0, "miss": 0}
    return cur


def _normalize_backoff(steps) -> list[tuple[float, int]]:
    """retry.long_backoff → [(after_seconds, interval_seconds), …]，按年龄升序。

    接受两种写法：[{after_hours: 24, interval_seconds: 3600}, …] 或 [[24, 3600], …]。
    非法条目（非数字、负数、0 间隔）直接跳过 —— 配置写错不能把重试停掉。
    """
    out: list[tuple[float, int]] = []
    for item in steps or []:
        try:
            if isinstance(item, dict):
                hours, sec = item.get("after_hours"), item.get("interval_seconds")
            else:
                hours, sec = item[0], item[1]
            hours, sec = float(hours), int(sec)
        except (TypeError, ValueError, IndexError, KeyError):
            continue
        if hours >= 0 and sec > 0 and hours == hours:      # hours == hours：排除 NaN
            out.append((hours * 3600.0, sec))
    return sorted(out)


def long_retry_interval(base: int, steps, age_seconds: float) -> int:
    """纯秒传长间隔：按「第一次未命中至今」的时长逐级放慢。

    默认 15 分钟 →（满 24 小时）1 小时 →（满 7 天）6 小时。只会比 base 更慢：
    用户自己把 long_interval_seconds 调得比档位还长时，以用户的为准。
    retry.long_backoff 设成 [] 即恢复旧行为（永远按 long_interval_seconds）。
    """
    interval = int(base)
    for after, sec in _normalize_backoff(steps):
        if age_seconds >= after:
            interval = max(interval, sec)
    return interval


def _sha1_file(path: str, total: int, on_progress, cancel: threading.Event) -> str | None:
    """同步读盘 + 算 SHA1（在线程池里跑，不占事件循环）。

    `cancel` 被置位（等它的协程被取消，比如容器退出）就尽快收手返回 None，
    免得 asyncio.run 收尾时 shutdown_default_executor 被一个 20GB 的哈希拖住。
    """
    h = hashlib.sha1()
    read = 0
    last = time.monotonic()
    # 缓冲区用匿名 mmap，不用 bytearray：1MB 级的 malloc 会被 glibc 的动态 mmap 阈值
    # 收进线程池各线程的 arena，用完也不还给系统（实测线程池里常驻多出 5~15MB）；
    # 匿名 mmap 用完 munmap，内存立刻归还。
    buf = mmap.mmap(-1, max(1, min(CHUNK, int(total or 1))))
    view = memoryview(buf)
    try:
        with open(path, "rb", buffering=0) as f:
            while True:
                if cancel.is_set():
                    return None
                n = f.readinto(buf)
                if not n:
                    break
                h.update(view[:n])
                read += n
                now = time.monotonic()
                if now - last >= 1.0:
                    on_progress(round(read / total * 100, 2))
                    last = now
    finally:
        view.release()
        buf.close()
    return h.hexdigest()


def _same_file(path: str, record: dict) -> bool:
    """磁盘上的文件是否还是 record 记下的那份（size + mtime 都对得上）。"""
    try:
        st = os.stat(path)
    except OSError:
        return False
    size, mtime = record.get("size"), record.get("mtime")
    if size is None or mtime is None:
        return True                      # 记录里没有可比的值：不拦（与旧行为一致）
    try:
        # 注意别写成 `size or -1`：0 字节文件会被误判成「变了」，永远算不完 SHA1
        return int(size) == int(st.st_size) and abs(float(mtime) - st.st_mtime) <= 1e-6
    except (TypeError, ValueError):
        return True


def _reuse_sha1(path: str, record: dict, store) -> str | None:
    """同一个文件换了路径（改名 / 移动目录 / 硬链接）：直接用另一条记录里算好的 SHA1。

    只在磁盘上的文件此刻仍是记录里那份（设备:inode、size、mtime 全对得上）时复用。
    """
    ino = record.get("ino")
    if not ino or not hasattr(store, "find_sha1"):
        return None
    try:
        st = os.stat(path)
    except OSError:
        return None
    if f"{st.st_dev}:{st.st_ino}" != ino or not _same_file(path, record):
        return None
    digest = store.find_sha1(ino, int(record["size"]), float(record["mtime"]), path)
    if not digest:
        return None
    store.set(path, sha1=digest, progress=0.0)
    logger.info("SHA1 复用（同一文件换了路径/硬链接，不再读盘）：%s → %s",
                os.path.basename(path), digest)
    return digest


async def sha1_of(path: str, record: dict, store) -> str | None:
    """算过就直接复用（重启/重试都不再读盘）；没算过则 1MB 分块流式算；同一文件换了路径时复用已有结果。

    读盘 + 哈希放到线程池里跑：原来是 async 函数里的同步循环，算一个 20GB 原盘时
    整个事件循环停住（秒传、巡检、主循环扫描全部停摆，机械盘上约 3 分钟）。

    返回 None 表示**算的过程中文件被换掉了**（洗板重下 / 还在写）：算出来的值
    不属于库里那条记录，绝不能落库；调用方应把记录按新内容重新入队。
    """
    if record.get("sha1"):
        return record["sha1"]
    reused = _reuse_sha1(path, record, store)
    if reused:
        return reused
    total = record["size"] or 1
    cancel = threading.Event()

    def on_progress(pct: float) -> None:
        store.set(path, progress=pct)

    try:
        digest = await asyncio.to_thread(_sha1_file, path, total, on_progress, cancel)
    except asyncio.CancelledError:
        cancel.set()
        raise
    if digest is None:
        return None
    # 线程里算的这段时间，主循环可能已经扫描过、发现文件变了并重建了记录：
    # 磁盘或库里的 size/mtime 任何一个对不上，都说明这个摘要不属于当前记录。
    cur = store.get(path) or {}
    if not _same_file(path, record) or not _same_file(path, cur or record):
        logger.warning("算 SHA1 期间文件被改动，丢弃结果：%s", os.path.basename(path))
        return None
    store.set(path, sha1=digest, progress=0.0)
    logger.info("SHA1 计算完成：%s → %s", os.path.basename(path), digest)
    return digest


class Uploader:
    def __init__(self, cfg: dict, store, manager):
        self.cfg = cfg
        self.store = store
        self.mgr = manager
        # 与旧实例共享（见模块顶部 _SHARED 的说明）：改配置重建 Uploader 时，
        # 旧任务还在跑，实例级状态会让同一文件被处理两遍。
        _n = max(1, int(cfg["upload"]["concurrency"]))
        if _SHARED["sem"] is None or _SHARED["sem_n"] != _n:
            _SHARED["sem"] = asyncio.Semaphore(_n)
            _SHARED["sem_n"] = _n
        self.sem = _SHARED["sem"]
        self._active: set[str] = _SHARED["active"]
        up = cfg["upload"]
        self.small_bytes = float(up["small_file_mb"]) * 1024 * 1024
        rt = cfg["retry"]
        self.retry_interval = int(rt["interval_seconds"])
        self.retry_max_count = int(rt["max_count"])
        self.retry_max_hours = float(rt["max_hours"])
        # scan.* 不在 RELOAD_PREFIXES 里（改扫描参数没必要重建 115 客户端），
        # 所以这里绝不能缓存 scan 段的值：面板把它调小之后，扫描器会按新值把
        # stable_count 停在 2，而上传器还拿着旧的 5 比较，文件会永远卡在「待处理」。
        # 一律现读，与 scanner.py 保持一致。
        # 纯秒传：只发校验请求，命中与否取决于云端 hash 索引；未命中就等下一轮，永不普通上传
        self.mode = str(up.get("mode") or "rapid_then_upload")
        self.rapid_only = self.mode == "rapid_only"
        self.long_interval = int(rt.get("long_interval_seconds") or 900)
        # 长间隔按文件「等了多久」逐级放慢（见 long_retry_interval）。缺省档位见 config.DEFAULTS
        self.long_backoff = rt.get("long_backoff", [])
        # 传完就删本地源文件（破坏性操作，默认关）
        self.delete_after = bool(up.get("delete_after_upload"))
        # 洗板：云端同一集只留这次传上去的，旧版本移进 115 回收站（破坏性，默认关）
        self.cleanup_prev = bool(up.get("cleanup_previous_versions"))
        self.cleanup_max = int(up.get("cleanup_max_delete") or 3)
        # 普通上传的单文件上限（字节，0 = 不限）。见 config.DEFAULTS["upload"]["max_file_gb"]
        try:
            self.max_upload_bytes = int(float(up.get("max_file_gb", 115) or 0) * 1024 ** 3)
        except (TypeError, ValueError):
            self.max_upload_bytes = 115 * 1024 ** 3
        self.notifier = Notifier(cfg)
        # 改配置 / 换 cookie 会重建 Uploader：把断路器放开，让下一轮立刻重新试探一次
        _SHARED["auth_fail"] = None
        # 同样跨实例共享：旧实例被覆盖后，它派发的后台任务若只被实例持有的 set 引用，
        # 会被 GC 回收（跑到一半消失）；异常计数清空则让退避重试永远到不了阈值。
        self._bg: set = _SHARED["bg"]          # 后台任务强引用，防止跑到一半被回收
        # 未预期异常（不是 115 的瞬态错误，而是 fs_makedirs 500、文件读不了这类
        # 真实故障）的连续次数。一次就判死刑太狠 —— ensure_pid 是裸调、没被
        # _retry_transient 包住，一次网络抖动也会冒到这里；无人值守时把文件永久
        # 标成「错误」等于要人工介入。改成退避重试，连续 error_max_attempts 次才停。
        self._err_streak: dict[str, int] = _SHARED["err_streak"]
        self.error_max_attempts = 3

    # ---------- 调度 ----------

    def spawn(self, path: str) -> None:
        if path in self._active:
            return
        self._active.add(path)

        async def run():
            try:
                async with self.sem:
                    if self._hold_for_auth(path):
                        return
                    await self._process(path)
                self._err_streak.pop(path, None)       # 跑通一轮 → 清掉异常计数
            except Exception as exc:  # noqa: BLE001
                if is_auth_error(exc):
                    # 登录失效不是文件的错：不累计异常次数（否则 3 次就把一整库文件标成「错误」），
                    # 也不当「秒传未命中」（会推高 retry_count、把长间隔越拖越长）。
                    # 记下断路器，文件 5 分钟后再来；cookie 更新后 dispatch 立刻放开。
                    self._on_auth_failure(path, exc)
                    return
                # 只记 message、不改状态是不行的：文件会停在「处理中」「待处理」这类
                # 中间态，而 dispatch 每轮又把它捞回来 → 每轮重试一次、日志一直刷、
                # 并发名额一直被占，界面上却看不到「错误」，用户无从下手。
                # 但一次就判「错误」同样不行：ensure_pid 是裸调、没被 _retry_transient
                # 包住，115 抖一下也会走到这里。所以退避重试，连续多次才停。
                n = self._err_streak.get(path, 0) + 1
                self._err_streak[path] = n
                if n >= self.error_max_attempts:
                    self._err_streak.pop(path, None)
                    logger.error("处理 %s 连续 %d 次异常，停止自动重试：%s", path, n, exc)
                    self.store.set(path, status="错误",
                                   message=f"连续 {n} 次异常，已停止自动重试：{exc}")
                else:
                    backoff = min(600.0, 30.0 * n)
                    logger.warning("处理 %s 第 %d 次异常，%d 秒后重试：%s",
                                   path, n, int(backoff), exc)
                    # 用「待重试」实现退避（只有它在 dispatch 里带 due 过滤，会看
                    # next_retry_at）。_process 见到 sha1 为空会先补算，所以退避回来
                    # 不会拿空 sha1 去秒传。
                    self.store.set(path, status="待重试",
                                   next_retry_at=time.time() + backoff,
                                   message=f"第 {n} 次异常，{int(backoff)} 秒后重试：{exc}")
            finally:
                self._active.discard(path)

        t = asyncio.create_task(run())
        self._bg.add(t)
        t.add_done_callback(self._bg.discard)

    def _hold_for_auth(self, path: str) -> bool:
        """断路器合上时，排队等着的联网任务（dispatch 一次派了一大批）别再逐个去撞 115：
        除了指定的试探文件，其余推迟到下次试探时间。返回 True 表示已推迟、不要处理。"""
        af = _SHARED["auth_fail"]
        if not af or path == af.get("probe_path"):
            return False
        rec = self.store.get(path)
        if not rec or rec["status"] in _OFFLINE_STATES or rec["status"] in ("已上传", "错误"):
            return False
        self.store.set(path, status="待重试", next_retry_at=max(af["next_probe"], time.time()),
                       message="115 登录失效（cookie 过期？）：已暂停向 115 发请求，更新 cookie 后自动继续")
        return True

    def _on_auth_failure(self, path: str, exc: BaseException) -> None:
        now = time.time()
        first = _SHARED["auth_fail"] is None
        msg = str(exc)[:200]
        _SHARED["auth_fail"] = {"since": (_SHARED["auth_fail"] or {}).get("since", now),
                                "message": msg, "next_probe": now + AUTH_PROBE_SECONDS}
        if first:
            logger.error("115 登录失效（cookie 过期或被踢下线）：%s。已暂停向 115 发请求，"
                         "请在面板「115 账号」更新 cookie；每 %d 秒试探一次",
                         msg, int(AUTH_PROBE_SECONDS))
        self.store.set(path, status="待重试", next_retry_at=now + AUTH_PROBE_SECONDS,
                       message="115 登录失效（cookie 过期？）：请在面板「115 账号」更新 cookie；"
                               "更新后会自动继续，不计入重试次数")

    def _auth_ok(self) -> None:
        """联网请求成功过一次 = 登录有效，放开断路器。"""
        if _SHARED["auth_fail"] is not None:
            _SHARED["auth_fail"] = None
            logger.info("115 登录已恢复")

    def _file_changed_on_disk(self, path: str, rec: dict) -> bool:
        """上传结束前确认磁盘上还是当初那个文件。

        MoviePilot 洗板重下会在**同一个路径**写入新内容；而上传（尤其分块传几 GB）
        要跑很久，中间完全可能被换掉。这时 _process 手里还是旧 rec，跑完若照常写
        「已上传」，就会盖掉 scanner 刚做的重置 —— 库里成终态，云端是旧内容、本地是
        新内容，而终态让 scanner 与 dispatch 都不再碰它：**新内容永远不会上传**。
        """
        try:
            st = os.stat(path)
        except OSError:
            return True                                  # 文件没了，也按「已变」处理
        try:
            if int(rec.get("size") or -1) != int(st.st_size):
                return True
            return abs(float(rec.get("mtime") or 0) - st.st_mtime) > 1e-6
        except (TypeError, ValueError):
            return False

    def _requeue_changed_file(self, path: str) -> None:
        """上传期间文件被替换：重置成新文件重新入队（不是「已上传」）。"""
        try:
            st = os.stat(path)
            self.store.set(path, status="待处理", size=st.st_size, mtime=st.st_mtime,
                           sha1=None, stable_count=0, file_id=None, progress=0.0,
                           # 事实作废：文件是被换掉的，不是本程序收尾删的
                           source_deleted=0,
                           message="上传期间文件被替换（洗板重下），已按新内容重新入队")
            logger.info("上传期间文件被替换，已重新入队：%s", os.path.basename(path))
        except OSError:
            self.store.set(path, status="错误", message="上传期间文件消失")

    def _notify(self, cloud_dir: str, mode: str, local_path: str) -> None:
        """秒传命中 / 上传完成后，把通知丢到后台去发（不等它）。

        对方收到通知后会先干活再回执，慢的时候十几秒；在这里等会把「同时处理几个文件」
        的名额占住，后面的文件只能干等。通知失败不影响上传结果，所以后台跑就好。
        """
        if not self.notifier.ready or not self.notifier.should_fire(mode):
            return
        cloud_path = f"{cloud_dir.rstrip('/')}/{os.path.basename(local_path)}"

        async def run():
            try:
                ok, info = await asyncio.to_thread(self.notifier.send, cloud_path, mode)
            except Exception as exc:  # noqa: BLE001
                logger.warning("通知出错（不影响上传）：%s", exc)
                ok, info = False, f"{type(exc).__name__}: {exc}"
            # 结果写回这条记录的说明，「任务明细」里直接能看到通知有没有发出去
            cur = self.store.get(local_path) or {}
            tail = f"｜已通知下游（{info}）" if ok else f"｜通知下游失败：{info}"
            self.store.set(local_path, message=(cur.get("message") or "") + tail)

        t = asyncio.create_task(run())
        self._bg.add(t)
        t.add_done_callback(self._bg.discard)

    def _delete_source(self, path: str, rec: dict) -> None:
        """上传成功后删掉监控目录里的源文件本体（文件夹一律保留）。

        护栏：只删监控目录内部的路径；大小和入库记录对不上就跳过（说明文件被换过）；
        任何失败只记警告，绝不改状态机——上传已经成功了，删除只是收尾。
        """
        if not self.delete_after:
            return
        try:
            root = os.path.realpath(str(self.cfg["paths"]["source"])).rstrip(os.sep)
            real = os.path.realpath(path)
            if not real.startswith(root + os.sep):
                logger.warning("不删（不在监控目录内）：%s", path)
                return
            if not os.path.isfile(real):
                return
            size = int(rec.get("size") or 0)
            if size and os.path.getsize(real) != size:
                logger.warning("不删（大小已变，可能被换过）：%s", path)
                return
            # 光比大小不够：洗板重下可能恰好同 size（同一集的不同版本往往只差几 MB，
            # 也存在大小完全相同的编码）。删是破坏性的，必须连 mtime 一起对上才动手。
            try:
                same_mtime = abs(float(rec.get("mtime") or 0) - os.path.getmtime(real)) <= 1e-6
            except OSError:
                same_mtime = False
            if not same_mtime:
                logger.warning("不删（修改时间已变，文件被换过）：%s", path)
                self._requeue_changed_file(path)
                return
            os.remove(real)
            logger.info("已删除本地源文件：%s", path)
            # 落一条「本地文件是本程序上传成功后删的」的持久事实（一次 set 同时更新 message）。
            # 洗板清理要靠它区分「正当版本的本地文件被收尾删掉」与「旧版本地被换掉」——
            # 只看 os.path.exists() 分不开这两者，会导致互删（见 _same_episode_keeps）。
            cur = self.store.get(path) or {}
            self.store.set(path, source_deleted=1,
                           message=(cur.get("message") or "") + "｜已删除本地源文件")
        except Exception as exc:  # noqa: BLE001
            logger.warning("删除本地源文件失败（不影响上传）：%s：%s", path, exc)

    def _same_episode_keeps(self, cloud_dir: str, local_path: str) -> tuple[list[str], list[str]]:
        """同集里「仍在本地、且已上传成功」的**其它**版本 —— 这些不许被当成旧版清掉。

        不这么做会互删：同一集两个版本几乎同时传完时，A 的清理看到 B（同集、已完成）
        就把它当旧版删掉，B 的清理又把 A 删掉 ⇒ **云端同集一个不剩**，而本地两条都是
        「已上传」终态、symedia 的 strm 指向不存在的文件。本地已复现（串行与并发都会中）。

        判据是「这个版本还是不是**这一批**的正当新版本」：
          - 本地文件还在、**且上传完成时间与当前文件同批**（差 ≤ SAME_BATCH_SECONDS）
            ⇒ 正当新版本 ⇒ 保护；
          - 本地文件已被**本程序**在上传成功后收尾删掉（`source_deleted=1`）
            ⇒ 同样是正当版本 ⇒ 保护；
          - 本地文件不在、且不是本程序删的 ⇒ 旧版本地被换掉了 ⇒ **不保护**；
          - **上传完成时间明显更早**（差 > SAME_BATCH_SECONDS）⇒ 上一轮洗板遗留的老版本，
            即使本地文件还在也**不保护** ⇒ 照旧清理。

        为什么必须加时间这一条（第 42 处，用户真机报障）：用户洗板重下后**两个版本的本地
        文件都在**（`HDR` 与无 `HDR` 的两条 S01E33），只按「本地在不在」判定会把旧版当正当
        版本保护住 ⇒ `cleanup_previous` 返回「没有旧版本」⇒ 云端两条都留着，
        表现就是「开关开着、点了重新排队、还是一样，没删」。
        本地已复现：NEW(刚传)/OLD(3 天前) 两条本地都在 → 保护集合含 OLD → deleted=[]。

        为什么必须保留 `source_deleted` 那一条：只看 `os.path.exists()` 分不开
        「洗板把旧版换掉了」与「`upload.delete_after_upload` 开关把刚传完的版本收尾删了」
        —— 后者开着时先传完那条本地是程序自己删的，后传那条就把它当旧版删掉（互删重现）。
        """
        key = episode_key(os.path.basename(local_path))
        if key is None:
            return [], []
        cur = self.store.get(local_path) or {}
        try:
            cur_at = float(cur.get("created_at") or 0.0)
        except (TypeError, ValueError):
            cur_at = 0.0
        fids: list[str] = []
        names: list[str] = []
        try:
            rows = self.store.by_status("已上传", limit=1000)
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取同集已上传记录失败（不影响清理）：%s", exc)
            return [], []
        for rec in rows or []:
            path = str(rec.get("path") or "")
            if not path or path == local_path:
                continue
            if rec.get("target_dir") != cloud_dir:
                continue
            base = os.path.basename(path)
            if episode_key(base) != key:
                continue
            if not os.path.exists(path) and not int(rec.get("source_deleted") or 0):
                continue                    # 旧版本地被换掉了（不是程序收尾删的）→ 不保护
            # 时间判据：明显不是同一批 ⇒ 洗板遗留的老版本，本地还在也不保护。
            # cur_at 为 0（拿不到当前记录时间）时退化为「全保护」，保守不出错。
            if cur_at:
                try:
                    at = float(rec.get("created_at") or 0.0)
                except (TypeError, ValueError):
                    at = 0.0
                if at and abs(cur_at - at) > SAME_BATCH_SECONDS:
                    logger.info("同集另有一版本 %s（内容入队于 %.0f 秒前）不是本轮同批，按旧版清理",
                                base, abs(cur_at - at))
                    continue
            names.append(base)
            fid = str(rec.get("file_id") or "").strip()
            if fid and fid not in ("0", "None"):
                fids.append(fid)
        return fids, names

    def _newer_episode_sibling(self, cloud_dir: str, local_path: str) -> str:
        """同集里是否存在**比当前文件更新**的已上传版本（且本地还在）—— 返回其文件名。

        为什么要这道护栏（第 42 处的另一半）：清理的语义是「同集只留最新版」，由**谁**来
        裁决「留谁」很关键。如果用户点的是**老版本**那条的「重新排队」，它会重新上传成功
        （秒传命中），然后以「刚上传那条」的身份去清理 —— 于是**真正的最新版被当旧版删掉**，
        结果与用户想要的相反（他差点就删错了）。

        有这个更新的版本在时，本次交给它，自己不动手（日志写明原因）。
        """
        key = episode_key(os.path.basename(local_path))
        if key is None:
            return ""
        cur = self.store.get(local_path) or {}
        try:
            cur_at = float(cur.get("created_at") or 0.0)
        except (TypeError, ValueError):
            return ""
        if not cur_at:
            return ""
        newest_at, newest_name = cur_at, ""
        try:
            rows = self.store.by_status("已上传", limit=1000)
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取同集已上传记录失败（不影响清理）：%s", exc)
            return ""
        for rec in rows or []:
            path = str(rec.get("path") or "")
            if not path or path == local_path:
                continue
            if rec.get("target_dir") != cloud_dir:
                continue
            base = os.path.basename(path)
            if episode_key(base) != key:
                continue
            if not os.path.exists(path) and not int(rec.get("source_deleted") or 0):
                continue                    # 本地已不在（且非程序删）→ 它自己就是被换掉的旧版
            try:
                at = float(rec.get("created_at") or 0.0)
            except (TypeError, ValueError):
                continue
            if at - newest_at > SAME_BATCH_SECONDS:
                newest_at, newest_name = at, base
        return newest_name

    # ---------- 同集巡检（**纯云端**视角） ----------
    # 用户明确的话（原话）：「你只能在 115 云端去核对，本地肯定是没有的，因为本地的文件
    # 在秒传完已经被删掉了」。所以判据必须**全部**落在 115 返回的条目上 ——
    # 不依赖本地文件、不依赖本地库里那条记录还在不在。
    # 做法：父目录内按「剧名 + SxxExx + 扩展名」分组 → 同组多于一条时，
    # 保留「115 侧时间最新」的那条，其余 `fs_delete` 移入回收站（可恢复）。

    @staticmethod
    def _cloud_ts(row: dict) -> float:
        """取云端条目的时间戳（越大越新）。

        【单一真相源】直接委托 `p115.P115.cloud_ts` —— 别在这里再写一份字段名列表：
        真机实测字段是 `te`/`tp`/`tu`（epoch 秒）与 `t`（`YYYY-MM-DD HH:MM`），
        两份实现一旦漂移，就会出现「上传后清理用一套字段、巡检用另一套」的诡异不一致。
        """
        return _P115.cloud_ts(row)

    async def _cloud_page(self, cid: int) -> list[dict] | None:
        """列一个云端目录的**全部条目**（含子目录），分页取全。只读。

        返回 `None` 表示**列目录失败**（与「目录是空的」= `[]` 区分开）——
        调用方据此不把这目录算进「已扫目录数」，否则 note 里的计数会骗人
        （真机实测：3 条列目录失败的 warning，却报「扫了 3 个云端目录」）。

        【必须走 `self.mgr.client`】—— `Uploader` 只持有 `self.mgr`（115 客户端在
        `mgr.client` 上），写 `self.client` 会抛
        `AttributeError: 'Uploader' object has no attribute 'client'`；
        而巡检的 `except` 会把它降级成一行 warning、`removed` 恒为 0
        —— 真机实测正是这个现象。`fs_files` 的 `data` 形状实测是 **list**，
        不是 dict（写成 `data.get("list")` 会 `AttributeError: 'list' object has no attribute 'get'`），
        所以下面三种形状都要兼容。
        """
        out: list[dict] = []
        offset, page = 0, 1150
        prev_sig: tuple | None = None
        for _ in range(MAX_SWEEP_PAGES):
            try:
                resp = await self.mgr.client.fs_files(
                    {"cid": str(cid), "offset": offset, "limit": page,
                     "show_dir": 1, "cur": 1}, async_=True)
            except Exception as exc:
                logger.warning("同集巡检：列云端目录 %s 失败：%s", cid, exc)
                return None                                # 失败 ≠ 空目录
            d = resp if isinstance(resp, dict) else {}
            # ★ 115 的业务错误是 **HTTP 200 + `state: false`（带 `errno`/`error`）**，
            # 不抛异常。不判这一层，就会把「被拒绝」当成「目录是空的」——
            # 实测五种形状（`state:false`、`errno` 风控、只有 `error`、空响应体、
            # `state:false` 且带 `data:[]`）**全部**落进「空目录」分支：
            # 巡检于是报「扫了 N 个目录、清掉 0 个」，**一行日志都没有** ——
            # 限流/风控/登录失效全都表现成「一切正常」（与第 44 处同一个病根：
            # 失败 ≠ 空目录，那次只堵住了「抛异常」这条路，业务错误这条路漏了）。
            if d.get("state") is False or (
                    d.get("error") and "data" not in d and "list" not in d):
                logger.warning(
                    "同集巡检：列云端目录 %s 被拒绝（state=%s errno=%s）：%s",
                    cid, d.get("state"), d.get("errno"), d.get("error"))
                return None                                # 业务错误 ≠ 空目录
            data = d.get("data")
            if isinstance(data, dict):
                rows = data.get("list") or []
            elif isinstance(data, list):
                rows = data
            elif "list" in d:
                rows = d.get("list") or []
            elif data is None and not d:
                logger.warning("同集巡检：列云端目录 %s 返回空响应体", cid)
                return None                                # 空响应体 ≠ 空目录
            else:
                rows = []
            rows = [r for r in rows if isinstance(r, dict)]
            if not rows:
                break
            sig = tuple(str(r.get("fid") or r.get("cid")) for r in rows)
            if sig and sig == prev_sig:
                logger.warning("同集巡检：目录 %s 反复返回同一批，已停止", cid)
                break
            prev_sig = sig
            out.extend(rows)
            if len(rows) < page:
                break
            offset += len(rows)
        return out

    async def _cloud_walk(self, cid: int, depth: int = 0, max_depth: int = 3,
                          seen: set | None = None) -> list[tuple[int, list[dict]]]:
        """递归列出云端目录树，返回 [(cid, 条目列表), …]。只读。"""
        seen = seen if seen is not None else set()
        out: list[tuple[int, list[dict]]] = []
        try:
            key = int(cid)
        except (TypeError, ValueError):
            return out
        if depth > max_depth or key in seen:
            return out
        seen.add(key)
        rows = await self._cloud_page(key)
        if rows is None:
            return out                                   # 列目录失败 → 不算「已扫」
        out.append((key, rows))
        if depth >= max_depth:
            return out
        for r in rows:
            sub = r.get("cid")
            if r.get("fid") not in (None, "") or sub in (None, "", 0, "0"):
                continue                                   # 只要子目录
            try:
                out.extend(await self._cloud_walk(int(sub), depth + 1, max_depth, seen))
            except (TypeError, ValueError):
                continue
        return out

    async def sweep_cloud_episode_duplicates(self, max_delete: int = 0) -> dict:
        """同集巡检：**只看 115 云端**，同集多条时只留最近上传的那条。

        用户诉求（原话）：「上传到115以后校验115的那个文件夹里面有没有相同集数的视频，
        就拿第33集为例，现在115里面有两个第33集的甲剧，但是一个是hdr的版本（后秒传上去的），
        一个普通4k版本（先秒传上去的），我需要的是后秒传上去这个hdr的版本后，把先秒传上去的
        普通4k版本删掉」；并补充「你只能在 115 云端去核对，本地肯定是没有的，因为本地的文件
        在秒传完已经被删掉了」。

        所以本巡检**不读本地文件、不依赖本地记录状态**，只用两样东西：
          1) 该扫哪些云端目录 —— 库里出现过的 `target_dir`，外加 `paths.target_root`
             （库里记录全没了也还能从根往下找）；都只是**路径字符串**，不是本地文件；
          2) 谁是「最新的那条」—— 115 条目自身的时间字段（见 `_CLOUD_TS_KEYS`）。
        拿不到时间字段时**保守不动手**，但日志必须可见（不再静默）。

        返回 {"removed": n, "dirs": 扫过的目录数, "note": 说明}；幂等（清完只剩一条）。
        """
        if not self.cleanup_prev:
            return {"removed": 0, "dirs": 0, "note": "开关 upload.cleanup_previous_versions 关着"}
        limit = max_delete or self.cleanup_max
        # ① 要扫的云端目录（只是路径字符串）
        roots: list[str] = []
        try:
            recs, _ = self.store.list_tasks(limit=2000)
        except Exception as exc:  # noqa: BLE001
            logger.warning("同集巡检：读取记录失败：%s", exc)
            recs = []
        for r in recs or []:
            td = str(r.get("target_dir") or "").strip()
            if td and td not in roots:
                roots.append(td)
        base = str((self.cfg.get("paths") or {}).get("target_root") or "").strip()
        if base and base not in roots:
            roots.append(base)
        if not roots:
            return {"removed": 0, "dirs": 0, "note": "没有可扫的云端目录"}

        removed = 0
        scanned = 0
        for td in roots:
            # ★ 只查缓存、**不建目录**：巡检对「云端早已不存在的 target_dir」不能
            # 触发 fs_makedirs（那会凭空建出空目录树）。拿不到 cid 说明这目录从没被
            # 本程序传过东西，跳过即可。
            getter = getattr(self.mgr, "cached_pid", None)
            try:
                cid = (await getter(td)) if callable(getter) else await self.mgr.ensure_pid(td)
            except Exception as exc:  # noqa: BLE001
                logger.warning("同集巡检：解析目录 %s 失败：%s", td, exc)
                continue
            if cid is None:
                logger.debug("同集巡检：目录 %s 没有 cid 缓存，跳过（不建目录）", td)
                continue
            try:
                tree = await self._cloud_walk(cid, 0, 3)
            except Exception as exc:  # noqa: BLE001
                logger.warning("同集巡检：遍历 %s 失败：%s", td, exc)
                continue
            for _dcid, items in tree:
                scanned += 1
                files = [r for r in items if r.get("fid") not in (None, "")]
                groups: dict[str, list[dict]] = {}
                for r in files:
                    k = episode_key(str(r.get("n") or r.get("name") or ""))
                    if k:
                        groups.setdefault(k, []).append(r)
                for k, grp in groups.items():
                    if len(grp) < 2:
                        continue                            # 该目录里这一集只有一条，正常
                    ts = [(self._cloud_ts(r), r) for r in grp]
                    newest = max(t for t, _ in ts)
                    if newest <= 0:
                        logger.info("同集巡检：%s（目录 %s）有 %d 条同集文件，"
                                    "但 115 条目没有可用时间字段，分不清哪条是后传的，本次不动手",
                                    k, td, len(grp))
                        continue
                    victims = [r for t, r in ts if t < newest]
                    if not victims:
                        continue                            # 时间戳都一样（同批），不动手
                    if limit > 0 and len(victims) > limit:
                        logger.info("同集巡检：%s 同集旧版本 %d 个，超过上限 %d，本次不动手",
                                    k, len(victims), limit)
                        continue
                    for r in victims:
                        fid = r.get("fid")
                        name = str(r.get("n") or r.get("name") or "")
                        try:
                            if await self.mgr.delete_file(fid):
                                removed += 1
                                logger.info("同集巡检：%s 保留最后上传的那条，已移入 115 回收站 %s",
                                            k, name)
                            else:
                                logger.warning("同集巡检：删除失败 %s", name)
                        except Exception as exc:  # noqa: BLE001
                            logger.warning("同集巡检：删除出错 %s：%s", name, exc)
        if removed:
            cache = getattr(self, "_dir_cache", None)
            if isinstance(cache, dict):
                cache.clear()                               # 内容变了，缓存作废
        return {"removed": removed, "dirs": scanned,
                "note": f"扫了 {scanned} 个云端目录，清掉 {removed} 个同集旧版"}

    async def _cleanup_prev_versions(self, cloud_dir: str, pid: int, local_path: str) -> None:
        """上传成功后：云端同一集只留**刚上传的那条**，其余移入 115 回收站。

        ★ 判据只用 115 云端条目自身的时间字段（`p115.P115.cleanup_episode_newest`）。
        为什么不能按本地文件判（第 43 处，用户真机报障的最终根因）：
          用户原话：「你只能在 115 云端去核对，本地肯定是没有的，因为本地的文件在秒传完
          已经被删掉了」。生产实测 `upload.delete_after_upload = True`
          ⇒ 每个上传成功的版本本地文件都被 `_delete_source()` 收尾删掉、库里
          `source_deleted=1`。而第 37/38 处为防「同集互删」加的保护判据是
          「本地文件还在 **或** 是本程序收尾删的（source_deleted=1）⇒ 正当版本 ⇒ 保护」
          —— 在这个配置下**每个版本都命中**，于是 keep_names 收进了同集所有条目、
          `cleanup_previous` 返回「没有旧版本」、清理永久空转。
          真机实测：2 小时内 `清理|洗板|旧版` 日志**零行**，云端 E33 一直是
          18:11（普通版）与 19:15（HDR 版）两条。
        新判据与本地完全无关，所以「本地已被删掉」「本地文件还在」「库里记录丢了」
        这些情况一律不影响判定。
        """
        if not self.cleanup_prev:
            return
        name = os.path.basename(local_path)
        cur = self.store.get(local_path) or {}
        keep_fid = str(cur.get("file_id") or "")
        try:
            res = await self.mgr.cleanup_episode_newest(pid, name, self.cleanup_max,
                                                        keep_fid=keep_fid)
        except AttributeError:
            # 兼容测试替身 / 旧实现（没有纯云端判据时退回老路径）
            try:
                size = os.path.getsize(local_path)
            except OSError:
                size = 0
            keep_fids, keep_names = self._same_episode_keeps(cloud_dir, local_path)
            try:
                res = await self.mgr.cleanup_previous(pid, name, size, self.cleanup_max,
                                                      keep_fid=keep_fid,
                                                      keep_fids=keep_fids,
                                                      keep_names=keep_names)
            except TypeError:
                res = await self.mgr.cleanup_previous(pid, name, size, self.cleanup_max,
                                                      keep_fid=keep_fid)
        except Exception as exc:  # noqa: BLE001
            logger.warning("洗板清理出错（不影响上传）：%s", exc)
            return
        deleted = list((res or {}).get("deleted") or [])
        note = str((res or {}).get("note") or "")
        if deleted:
            logger.info("洗板清理：%s 就位，云端同集旧版本已清 %s", name, "、".join(deleted))
        elif note and note != "没有旧版本":
            logger.info("洗板清理：%s —— %s", name, note)
        tail = ""
        if deleted:
            tail = f"｜云端同集旧版本已清 {len(deleted)} 个：" + "、".join(deleted)
        elif note and note != "没有旧版本":
            tail = f"｜洗板清理：{note}"
        if tail:
            cur = self.store.get(local_path) or {}
            self.store.set(local_path, message=(cur.get("message") or "") + tail)

    def spawn_sweep(self) -> bool:
        """把同集巡检丢到**后台**跑，不阻塞主循环。返回是否真的起了任务。

        为什么必须后台（实测数据，不是推测）：`scanner.scan_once()` / `uploader.dispatch()`
        与巡检原本在**同一个协程**里 `await` —— 巡检一跑就把主循环按住，那期间**不扫新文件、
        不派发上传任务**。忠实复现主循环调用形态的实测（RTT 0.5s、扫描间隔 2s）：
          - 巡检 3 个目录（耗时 1.5s）：相邻两轮扫描间隔 `[3.56, 2.0, 2.02, 2.0]`
            —— 第一轮被拖延 1.56s；
          - 巡检 202 个目录（耗时 101s）：间隔 `[105.4, 2.0]` —— **被按住 105 秒**。
        改后台后两种情况间隔都稳定在 2.0s。
        （真机现状只有 3 个目录、耗时约 1.5s，影响很小；但剧集积累后会线性变差，
        而「巡检是只读兜底」不该拖慢上传主链路。）

        防重入：上一次巡检还没跑完就直接返回 False（巡检幂等，叠加没意义）。
        强引用：任务存进 `_SHARED["bg"]`，防止跑到一半被 GC 回收（与 `spawn` 同款）。
        """
        if _SHARED.get("sweep_running"):
            return False
        _SHARED["sweep_running"] = True

        async def run():
            try:
                r = await self.sweep_cloud_episode_duplicates()
                if (r or {}).get("removed"):
                    logger.info("同集巡检：%s", (r or {}).get("note"))
                elif (r or {}).get("note"):
                    logger.debug("同集巡检：%s", (r or {}).get("note"))
            except Exception as exc:  # noqa: BLE001
                logger.warning("同集巡检出错（不影响上传）：%s", exc)
            finally:
                _SHARED["sweep_running"] = False

        task = asyncio.get_running_loop().create_task(run())
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)
        return True

    def dispatch(self) -> None:
        # 这里必须覆盖**全部**非终态，否则那个状态的文件就永久卡住：
        #   - 「待上传」：非纯秒传时走分块；纯秒传时 _process 开头把它拉回重试队列。
        #     库里残留的（切模式前入队的）只能靠人被点「立即处理」救，无人值守就一直卡。
        #   - 「处理中」「秒传中」「上传中」：这三个是 _process 内部先置状态、再去干活的。
        #     容器被强杀时 except 不执行，记录就悬在这三个状态上；没人派发 → 永久卡死。
        #     （这台机器上跑着十几个容器，docker restart / OOM 都是常态。）
        # 「待重试」是唯一带 due 过滤的：只有它需要等 next_retry_at（秒传重试 / 异常退避）。
        todo = [("待处理", False), ("处理中", False), ("待秒传", False),
                ("秒传中", False), ("待上传", False), ("上传中", False),
                ("待重试", True)]
        af = _SHARED["auth_fail"]
        limit_net = 100
        if af:
            # 登录失效断路器：平时只放行不联网的状态（算 SHA1）；到了试探时间只放 1 个文件去试，
            # 它成功（_auth_ok）就整体放开，失败则继续等下一个 AUTH_PROBE_SECONDS。
            todo = [(st, d) for st, d in todo if st in _OFFLINE_STATES]
            if time.time() >= af["next_probe"]:
                af["next_probe"] = time.time() + AUTH_PROBE_SECONDS
                todo.append(("待重试", True))
                limit_net = 1
        for status, due in todo:
            n = 100 if status in _OFFLINE_STATES else limit_net
            for rec in self.store.by_status(status, limit=n, due=due):
                if af and status not in _OFFLINE_STATES:
                    af["probe_path"] = rec["path"]           # 指定这一个去试探
                self.spawn(rec["path"])

    # ---------- 状态机 ----------

    async def _process(self, path: str) -> None:
        rec = self.store.get(path)
        if not rec or rec["status"] in ("已上传", "错误"):
            return
        if not os.path.exists(path):
            self.store.set(path, status="错误", message="源文件已不存在")
            return
        if self.rapid_only and rec["status"] == "待上传":
            # 纯秒传下没有普通上传这条路：历史遗留/手动标记的「待上传」拉回重试队列
            self.store.set(path, status="待重试", next_retry_at=time.time(),
                           message="纯秒传模式：不走普通上传，回到秒传重试队列")
            logger.info("纯秒传：把「待上传」拉回重试队列：%s", os.path.basename(path))
            return

        # 1) 预处理：等稳定 + 算 SHA1
        if (rec["status"] == "待处理"
                and rec["stable_count"] < int(self.cfg["scan"]["stable_rounds"])):
            return                                       # 还没稳定，等下一轮扫描

        if not rec.get("sha1"):
            # sha1 是下面秒传的必需入参，任何状态下都不能为空。正常路径是
            # 「待处理 →（算）→ 待秒传」；但异常退避可能把它停在「待重试」而
            # SHA1 还没算出来 —— 这时若不补算，秒传会拿到空 sha1，永远不可能
            # 命中，还会白刷 retry_count。所以这里统一补算。
            # 算 SHA1 要几十秒到几分钟，这段必须让它在界面上「看得见」：置成
            # 「处理中」（active() 会捞它、前端渲染「计算 SHA1 xx%」，sha1_of
            # 每秒回写 progress）。否则它既不进「正在处理」区、也不计入概览的
            # 正在处理数，看起来像卡死。
            self.store.set(path, status="处理中")
            rec = self.store.get(path)
            sha1 = await sha1_of(path, rec, self.store)
            if not sha1:
                # 算的过程中文件被换掉 / 还在写：按新内容重新入队、重新等稳定
                self._requeue_changed_file(path)
                return
            self.store.set(path, status="待秒传", sha1=sha1)
            rec = self.store.get(path)
        elif rec["status"] == "处理中":
            # SHA1 上一轮已经算完（算完就退避了 / 容器重启时残留）→ 直接推进，
            # 不必白算一遍。
            self.store.set(path, status="待秒传")
            rec = self.store.get(path)

        # 115 登录失效期间，刚算完 SHA1 的文件也别急着联网（见 _hold_for_auth）
        if self._hold_for_auth(path):
            return

        # 2) 目录：先建好（保留本地相对结构），秒传/分块上传共用这个 pid
        cloud_dir = resolve_upload_target(path, self.cfg["paths"]["source"],
                                          self.cfg["paths"]["target_root"],
                                          self.cfg["paths"].get("map"))
        pid = await self.mgr.ensure_pid(cloud_dir)
        self.store.set(path, target_dir=cloud_dir, pid=pid)

        # 3) 秒传
        store = self.store
        # 容器被强杀（OOM / docker restart / 宿主机重启）时，_process 已经把状态置成
        # 「上传中」再去传分块，SIGKILL 下 except 不会执行 —— 那条记录就永远停在
        # 「上传中」。重新被派发时若重走秒传：云端不会有完整文件（本来就传一半），
        # 必然未命中 → 被推回「待重试」白等好几轮才转回分块。断点齐全就直接续传。
        # 分块上传中途失败会经 spawn() 的退避落到「待重试」（见下面 4) 的说明），
        # 这种带完整断点的「待重试」到期后同样直接续传，不再先绕一圈秒传。
        has_breakpoint = all(rec.get(k) for k in
                             ("upload_id", "upload_oss_url", "upload_callback"))
        resume_upload = has_breakpoint and (
            rec["status"] == "上传中"
            or (rec["status"] == "待重试" and not self.rapid_only))
        if resume_upload:
            logger.info("恢复被打断的分块上传（断点续传）：%s", os.path.basename(path))
        while not resume_upload:
            store.set(path, status="秒传中")
            try:
                hit, info = await self.mgr.rapid(path, rec["size"], rec["sha1"] or "", pid)
            except Exception as exc:  # noqa: BLE001
                if is_auth_error(exc):
                    raise                          # 登录失效不是「未命中」：交给 spawn 的断路器
                msg = str(exc)
                if "10014" in msg or "目录不存在" in msg:
                    await self.mgr.forget_pid(cloud_dir)
                    pid = await self.mgr.ensure_pid(cloud_dir)
                hit, info = False, {"reason": msg}
            else:
                self._auth_ok()                    # 秒传请求正常返回（命中或未命中）= 登录有效
            tally = _SHARED["rapid_round"]
            tally["checked"] += 1
            tally["hit" if hit else "miss"] += 1
            if hit:
                if self._file_changed_on_disk(path, rec):
                    # 命中的是旧内容；本地已是新内容。绝不能写「已上传」——
                    # 终态会让新内容再也上不去（洗板重下的典型后果）。
                    self._requeue_changed_file(path)
                    return
                store.set(path, status="已上传", progress=100.0,
                          file_id=info.get("file_id", ""), upload_mode="rapid",
                          message=f"秒传命中 → {cloud_dir}")
                logger.info("秒传命中：%s → %s（未消耗上行流量）",
                            os.path.basename(path), cloud_dir)
                self._notify(cloud_dir, "rapid_hit", path)
                await self._cleanup_prev_versions(cloud_dir, pid, path)
                self._delete_source(path, rec)
                return

            if not self.rapid_only and rec["size"] < self.small_bytes:
                store.set(path, status="待上传",
                          message=f"秒传未命中（{info.get('reason', '')}），小文件直接上传")
                logger.info("秒传未命中，小文件直接上传：%s", os.path.basename(path))
                break

            cur = store.get(path)
            rc = (cur["retry_count"] or 0) + 1
            first = cur["first_failed_at"] or time.time()
            aged = (time.time() - first) / 3600.0

            if self.rapid_only:
                # 纯秒传：不设上限、不转普通上传。前 max_count 轮按短间隔试，之后放长间隔一直等
                # （115 秒传是全站 hash 索引，等别人把同 hash 的片源传上云，这里就能零上行命中）
                # 短间隔用完后的长间隔再按「等了多久」逐级放慢：挂了几周的老片还按
                # 15 分钟一问，1000 个文件就是持续不断的请求流，最容易招 115 风控。
                if rc <= self.retry_max_count:
                    interval = self.retry_interval
                else:
                    interval = long_retry_interval(self.long_interval, self.long_backoff,
                                                   time.time() - first)
                store.set(path, status="待重试", retry_count=rc, first_failed_at=first,
                          next_retry_at=time.time() + interval,
                          message=f"纯秒传：未命中（{info.get('reason', '')}），"
                                  f"{interval}s 后第 {rc} 次校验（只发校验请求）")
                # 只有第一次未命中打 INFO；之后每次都一样的「还没有」降成 DEBUG，
                # 由主循环每轮一行汇总代替（否则 1000 个待命中文件每天约 10 万行）。
                logger.log(logging.INFO if rc == 1 else logging.DEBUG,
                           "纯秒传：未命中，%ds 后第 %d 次校验：%s",
                           interval, rc, os.path.basename(path))
                return

            if rc > self.retry_max_count or aged >= self.retry_max_hours:
                store.set(path, status="待上传", retry_count=rc, first_failed_at=first,
                          message=f"秒传重试耗尽（{rc - 1} 次 / {aged:.1f}h），转普通上传")
                logger.info("秒传重试耗尽，转普通上传：%s", os.path.basename(path))
                break
            nxt = time.time() + self.retry_interval
            store.set(path, status="待重试", retry_count=rc, first_failed_at=first,
                      next_retry_at=nxt,
                      message=f"第 {rc} 次重试已排入队列（只重发校验请求）")
            logger.log(logging.INFO if rc == 1 else logging.DEBUG,
                       "秒传未命中，安排第 %d 次重试（%ds 后）：%s",
                       rc, self.retry_interval, os.path.basename(path))
            return                                       # 到期由调度器重新拾起

        # 4) 分块上传（带断点续传）
        # 超过 115 单文件上限的，传多少都会在最后被拒：先拦住，别白占几十上百 GB 上行。
        if self.max_upload_bytes and rec["size"] > self.max_upload_bytes:
            gb = rec["size"] / 1024 ** 3
            store.set(path, status="错误",
                      message=f"文件 {gb:.1f} GiB，超过普通上传上限 {self.max_upload_bytes / 1024 ** 3:.0f} GiB"
                              "（115 单文件限制），已跳过；秒传不受此限")
            logger.warning("不普通上传（超过 115 单文件上限 %.0f GiB）：%s（%.1f GiB）",
                           self.max_upload_bytes / 1024 ** 3, os.path.basename(path), gb)
            return
        store.set(path, status="上传中", message="分块上传中")
        try:
            result = await self.mgr.multipart(path, pid, rec["sha1"] or "", store.get(path))
            if self._file_changed_on_disk(path, rec):
                # 上传期间文件被替换（洗板重下）：云端那半截旧内容没有意义，
                # 按「新内容」重新入队；绝不能写「已上传」把 scanner 的重置盖掉。
                self._requeue_changed_file(path)
                return
            self._auth_ok()
            store.set(path, status="已上传", progress=100.0, upload_mode="normal",
                      file_id=str((result or {}).get("file_id", "")),
                      message=f"分块上传完成 → {cloud_dir}")
            logger.info("分块上传完成：%s → %s", os.path.basename(path), cloud_dir)
            self._notify(cloud_dir, "uploaded", path)
            await self._cleanup_prev_versions(cloud_dir, pid, path)
            self._delete_source(path, rec)
        except Exception as exc:
            # 原来这里一次失败就直接判「错误」，绕过了 spawn() 的退避：几 GB 的上传
            # 中途网络抖一下就得人工点重试。现在交给 spawn() 统一退避（30s/60s…，
            # 连续 error_max_attempts 次才判「错误」）；断点还在库里，到期后从断点续传。
            logger.warning("分块上传失败（断点已保留，将退避后续传）：%s：%s", path, exc)
            raise RuntimeError(f"分块上传失败：{exc}") from exc
