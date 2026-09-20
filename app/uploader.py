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
        self.stable_rounds = int(cfg["scan"]["stable_rounds"])
        # 纯秒传：只发校验请求，命中与否取决于云端 hash 索引；未命中就等下一轮，永不普通上传
        self.mode = str(up.get("mode") or "rapid_then_upload")
        self.rapid_only = self.mode == "rapid_only"
        self.long_interval = int(rt.get("long_interval_seconds") or 900)

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

        asyncio.create_task(run())

    def dispatch(self) -> None:
        todo = [("待处理", False), ("待秒传", False), ("待重试", True)]
        if not self.rapid_only:
            todo.append(("待上传", False))            # 纯秒传模式不派发普通上传
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
            if rec["stable_count"] < self.stable_rounds:
                return                                   # 还没稳定，等下一轮扫描
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
        except Exception as exc:  # noqa: BLE001
            store.set(path, status="错误", message=f"分块上传失败：{exc}")
            logger.error("分块上传失败：%s：%s", path, exc)
