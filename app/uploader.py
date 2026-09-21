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
import os
import time

from notify import Notifier
from p115 import episode_key, resolve_upload_target

logger = logging.getLogger("upilot.upload")
CHUNK = 8 * 1024 * 1024

# 跨实例共享的调度状态。
#
# main.py 在改配置时（upload./retry./notify. 任意一项、或 115 cookie 同步）会重建
# Uploader，而**旧实例已经派发的后台任务仍在事件循环里跑**。这几样如果留在实例上：
#   - _active 为空     → 同一个文件被新旧两个协程同时处理（实测秒传请求发 2 次）；
#   - sem 各算一份     → 并发上限直接翻倍（旧 1 / 新 2）；
#   - _bg 为空         → 旧实例被覆盖后，它的后台任务失去强引用，可能被 GC 回收；
#   - _err_streak 清空 → 连续异常计数作废，退避重试永远到不了阈值。
# 所以统一放模块级共享。sem 只在并发数真的变了时重建（容量要跟着配置走）。
_SHARED: dict = {"active": set(), "bg": set(), "err_streak": {}, "sem": None, "sem_n": -1}


async def sha1_of(path: str, record: dict, store) -> str:
    """算过就直接复用（重启/重试都不再读盘）；没算过则 8MB 分块流式算。"""
    if record.get("sha1"):
        return record["sha1"]
    h = hashlib.sha1()
    read = 0
    total = record["size"] or 1
    last = time.monotonic()
    with open(path, "rb") as f:
        while True:
            block = f.read(CHUNK)
            if not block:
                break
            h.update(block)
            read += len(block)
            now = time.monotonic()
            if now - last >= 1.0:
                store.set(path, progress=round(read / total * 100, 2))
                last = now
    digest = h.hexdigest()
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
        # 传完就删本地源文件（破坏性操作，默认关）
        self.delete_after = bool(up.get("delete_after_upload"))
        # 洗板：云端同一集只留这次传上去的，旧版本移进 115 回收站（破坏性，默认关）
        self.cleanup_prev = bool(up.get("cleanup_previous_versions"))
        self.cleanup_max = int(up.get("cleanup_max_delete") or 3)
        self.notifier = Notifier(cfg)
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
                    await self._process(path)
                self._err_streak.pop(path, None)       # 跑通一轮 → 清掉异常计数
            except Exception as exc:  # noqa: BLE001
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
            cur = self.store.get(path) or {}
            self.store.set(path, message=(cur.get("message") or "") + "｜已删除本地源文件")
        except Exception as exc:  # noqa: BLE001
            logger.warning("删除本地源文件失败（不影响上传）：%s：%s", path, exc)

    def _same_episode_keeps(self, cloud_dir: str, local_path: str) -> tuple[list[str], list[str]]:
        """同集里「仍在本地、且已上传成功」的**其它**版本 —— 这些不许被当成旧版清掉。

        不这么做会互删：同一集两个版本几乎同时传完时，A 的清理看到 B（同集、已完成）
        就把它当旧版删掉，B 的清理又把 A 删掉 ⇒ **云端同集一个不剩**，而本地两条都是
        「已上传」终态、symedia 的 strm 指向不存在的文件。本地已复现（串行与并发都会中）。

        判据是「本地文件是否还在」：
          - 洗板场景：旧版的本地文件已被换掉/删除 ⇒ 不保护 ⇒ 照旧被清理（行为不变）；
          - 同集两个都还在本地 ⇒ 都是正当新版本 ⇒ 都保护。
        """
        key = episode_key(os.path.basename(local_path))
        if key is None:
            return [], []
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
            if not os.path.exists(path):
                continue                    # 本地已不在（旧版被换掉了）→ 不保护
            names.append(base)
            fid = str(rec.get("file_id") or "").strip()
            if fid and fid not in ("0", "None"):
                fids.append(fid)
        return fids, names

    async def _cleanup_prev_versions(self, cloud_dir: str, pid: int, local_path: str) -> None:
        """洗板（重下同集不同版本）后，把云端同一集的旧版本移进 115 回收站。

        只认「剧名 + SxxExx + 扩展名」这个键：画质标签（DV / HDR / WEB-DL / 站点名…）
        与集号之后的内容都不参与比较，所以 [.. DV ..] 与 [.. ..] 会被判成同一集。
        任何失败都只记警告，绝不影响上传结果本身。
        """
        if not self.cleanup_prev:
            return
        name = os.path.basename(local_path)
        try:
            size = os.path.getsize(local_path)
        except OSError:
            size = 0
        # 刚上传那条的标识（秒传/分块成功时已落库；实测是 115 的 pickcode，形如 abcd0demo0pick001）——
        # 比文件名可靠：115 遇到同名会给新版改名成 "xxx (1).mkv"，只靠名字排除不掉它。
        cur = self.store.get(local_path) or {}
        keep_fid = str(cur.get("file_id") or "")
        # 同集「仍在本地、且已上传成功」的其它版本也必须保护，否则两个版本几乎同时传完时
        # 会互删（A 清 B、B 清 A ⇒ 云端同集一个不剩）。见 _same_episode_keeps 的说明。
        keep_fids, keep_names = self._same_episode_keeps(cloud_dir, local_path)
        try:
            res = await self.mgr.cleanup_previous(pid, name, size, self.cleanup_max,
                                                  keep_fid=keep_fid,
                                                  keep_fids=keep_fids,
                                                  keep_names=keep_names)
        except TypeError:
            # 兼容测试替身 / 旧签名实现（只接 keep_fid）
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
            self.store.set(local_path, message=(cur.get("message") or "") + tail)

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
        for status, due in todo:
            for rec in self.store.by_status(status, limit=100, due=due):
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
            self.store.set(path, status="待秒传", sha1=sha1)
            rec = self.store.get(path)
        elif rec["status"] == "处理中":
            # SHA1 上一轮已经算完（算完就退避了 / 容器重启时残留）→ 直接推进，
            # 不必白算一遍。
            self.store.set(path, status="待秒传")
            rec = self.store.get(path)

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
        resume_upload = (rec["status"] == "上传中"
                         and all(rec.get(k) for k in
                                 ("upload_id", "upload_oss_url", "upload_callback")))
        if resume_upload:
            logger.info("恢复被打断的分块上传（断点续传）：%s", os.path.basename(path))
        while not resume_upload:
            store.set(path, status="秒传中")
            try:
                hit, info = await self.mgr.rapid(path, rec["size"], rec["sha1"] or "", pid)
            except Exception as exc:  # noqa: BLE001
                msg = str(exc)
                if "10014" in msg or "目录不存在" in msg:
                    await self.mgr.forget_pid(cloud_dir)
                    pid = await self.mgr.ensure_pid(cloud_dir)
                hit, info = False, {"reason": msg}
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
                interval = self.retry_interval if rc <= self.retry_max_count else self.long_interval
                store.set(path, status="待重试", retry_count=rc, first_failed_at=first,
                          next_retry_at=time.time() + interval,
                          message=f"纯秒传：未命中（{info.get('reason', '')}），"
                                  f"{interval}s 后第 {rc} 次校验（只发校验请求）")
                logger.info("纯秒传：未命中，%ds 后第 %d 次校验：%s",
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
            logger.info("秒传未命中，安排第 %d 次重试（%ds 后）：%s",
                        rc, self.retry_interval, os.path.basename(path))
            return                                       # 到期由调度器重新拾起

        # 4) 分块上传（带断点续传）
        store.set(path, status="上传中", message="分块上传中")
        try:
            result = await self.mgr.multipart(path, pid, rec["sha1"] or "", store.get(path))
            if self._file_changed_on_disk(path, rec):
                # 上传期间文件被替换（洗板重下）：云端那半截旧内容没有意义，
                # 按「新内容」重新入队；绝不能写「已上传」把 scanner 的重置盖掉。
                self._requeue_changed_file(path)
                return
            store.set(path, status="已上传", progress=100.0, upload_mode="normal",
                      file_id=str((result or {}).get("file_id", "")),
                      message=f"分块上传完成 → {cloud_dir}")
            logger.info("分块上传完成：%s → %s", os.path.basename(path), cloud_dir)
            self._notify(cloud_dir, "uploaded", path)
            await self._cleanup_prev_versions(cloud_dir, pid, path)
            self._delete_source(path, rec)
        except Exception as exc:  # noqa: BLE001
            store.set(path, status="错误", message=f"分块上传失败：{exc}")
            logger.error("分块上传失败：%s：%s", path, exc)
