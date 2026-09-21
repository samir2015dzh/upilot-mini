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
from p115 import resolve_upload_target

logger = logging.getLogger("upilot.upload")
CHUNK = 8 * 1024 * 1024


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
        self.sem = asyncio.Semaphore(max(1, int(cfg["upload"]["concurrency"])))
        self._active: set[str] = set()
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
        self._bg: set = set()          # 持有后台通知任务的引用，防止跑到一半被回收

    # ---------- 调度 ----------

    def spawn(self, path: str) -> None:
        if path in self._active:
            return
        self._active.add(path)

        async def run():
            try:
                async with self.sem:
                    await self._process(path)
            except Exception as exc:  # noqa: BLE001
                logger.error("处理 %s 出错：%s", path, exc)
                self.store.set(path, message=f"异常：{exc}")
            finally:
                self._active.discard(path)

        t = asyncio.create_task(run())
        self._bg.add(t)
        t.add_done_callback(self._bg.discard)

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
            os.remove(real)
            logger.info("已删除本地源文件：%s", path)
            cur = self.store.get(path) or {}
            self.store.set(path, message=(cur.get("message") or "") + "｜已删除本地源文件")
        except Exception as exc:  # noqa: BLE001
            logger.warning("删除本地源文件失败（不影响上传）：%s：%s", path, exc)

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
        try:
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
        # 「待上传」两种模式都得派发：非纯秒传时 _process 走分块上传；纯秒传时
        # _process 开头那段兜底会把它拉回「待重试」。这里若按模式筛掉，库里残留的
        # 「待上传」（切换模式前入队的、或 rapid_then_upload 时期转过来的）就永远不会
        # 被自动派发 —— 只有人在界面点「立即处理」才救得回来，无人值守时一直卡着。
        # 「处理中」同理必须捞：容器重启时若有文件正卡在算 SHA1，没人派发它就永远
        # 停在「处理中」；派发后 _process 接着算完（已算好的直接复用）。
        todo = [("待处理", False), ("处理中", False), ("待秒传", False),
                ("待上传", False), ("待重试", True)]
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
        if rec["status"] == "待处理":
            if rec["stable_count"] < int(self.cfg["scan"]["stable_rounds"]):
                return                                   # 还没稳定，等下一轮扫描
            # 大文件算 SHA1 要几十秒到几分钟，这段必须让它在界面上「看得见」：
            # 置成「处理中」（active() 会捞它、前端渲染「计算 SHA1 xx%」，sha1_of
            # 每秒回写 progress）。否则它一直是「待处理」，既不进「正在处理」区、
            # 也不计入概览的正在处理数，看起来像卡死。
            self.store.set(path, status="处理中")
            rec = self.store.get(path)
        if rec["status"] == "处理中":
            # 已算过就秒回（sha1_of 复用落库的 sha1）；容器重启后残留的「处理中」
            # 也落到这里，被 dispatch 捞起来接着算完，不会永久悬着。
            sha1 = await sha1_of(path, rec, self.store)
            self.store.set(path, status="待秒传", sha1=sha1)
            rec = self.store.get(path)

        # 2) 目录：先建好（保留本地相对结构），秒传/分块上传共用这个 pid
        cloud_dir = resolve_upload_target(path, self.cfg["paths"]["source"],
                                          self.cfg["paths"]["target_root"],
                                          self.cfg["paths"].get("map"))
        pid = await self.mgr.ensure_pid(cloud_dir)
        self.store.set(path, target_dir=cloud_dir, pid=pid)

        # 3) 秒传
        store = self.store
        while True:
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
