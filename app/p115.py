"""115 侧封装：目录（保留相对结构）、秒传、分块上传（断点续传）、节流与瞬态重试。

- 秒传：upload_file_init(filename, filesize, filesha1, read_range_bytes_or_hash, pid)
  判定 reuse is True and state is True 才算命中；file_id 可能为 0，退回 pickcode。
- 目录：fs_makedirs 建整条链拿 cid（内存 + SQLite 双缓存 + 全局锁）
- 分块：P115MultipartUpload(url, path, callback, upload_id) + list_parts 校验断点
  + oss_multipart_upload_part_iter（不能用 iter_upload，它内部 url= 关键字会撞 positional-only）
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import time

logger = logging.getLogger("upilot.p115")

TRANSIENT_HTTP_CODES = (401, 403, 429, 500, 502, 503, 504)


def is_transient(exc: Exception) -> bool:
    from urllib.error import HTTPError
    return isinstance(exc, HTTPError) and exc.code in TRANSIENT_HTTP_CODES


def normalize_cloud_path(p: str) -> str:
    return "/" + "/".join(x for x in (p or "").replace("\\", "/").split("/") if x)


def resolve_upload_target(file_path: str, source_path: str, target_root: str,
                          mapping: list | None = None) -> str:
    """本地相对目录结构 → 115 目标目录。

    mapping 形如 [{"from": "Anime", "to": "/OtherAnime"}, ...]：按「本机相对路径」的
    最长前缀命中（逐段、大小写敏感），命中就用 to —— 所以支持改名与大小写差异；
    其下结构原样保留。没命中则落到 target_root + 原相对目录。
    """
    root = (target_root or "").rstrip("/").rstrip("\\")
    try:
        rel = os.path.relpath(file_path, source_path)
    except ValueError:
        rel = os.path.basename(file_path)
    if rel.startswith(".."):
        rel = os.path.basename(file_path)
    rel = rel.replace("\\", "/")
    parts = [x for x in os.path.dirname(rel).split("/") if x and x != "."]

    best_n, best_to = -1, None
    for item in (mapping or []):
        if not isinstance(item, dict):
            continue
        src = str(item.get("from") or "").strip().strip("/")
        dst = str(item.get("to") or "").strip()
        if not dst:
            continue
        if not src:
            n = 0
        else:
            segs = [x for x in src.split("/") if x]
            if parts[:len(segs)] != segs:                 # 逐段全等，大小写敏感
                continue
            n = len(segs)
        if n > best_n:
            best_n, best_to = n, dst
    if best_to is not None:
        rest = "/".join(parts[best_n:])
        return normalize_cloud_path(f"{best_to}/{rest}" if rest else best_to)

    if parts:
        joined = "/".join(parts)
        root = f"{root}/{joined}" if root else joined
    return normalize_cloud_path(root)


class P115:
    """真实 115 客户端。"""

    def __init__(self, cfg: dict, store):
        from p115client import P115Client

        self.cfg = cfg
        self.store = store
        cookie = (cfg["p115"].get("cookie") or "").strip()
        if not cookie:
            raise RuntimeError("未配置 115 cookie：填 /config/config.yaml 的 p115.cookie，"
                               "或设置环境变量 P115_COOKIE，或先执行 login 子命令扫码登录")
        self.client = P115Client(cookies=cookie, app=cfg["p115"].get("app", "qandroid"),
                                console_qrcode=False)
        self.min_interval = float(cfg["upload"]["init_min_interval"])
        self.transient_retries = int(cfg["upload"]["transient_retries"])
        self.partsize = int(cfg["upload"]["partsize_mb"]) * 1024 * 1024
        self._init_lock = asyncio.Lock()
        self._last_init = 0.0
        self._makedirs_lock = asyncio.Lock()
        self._cid_mem: dict[str, int] = {}

    # ---------- 节流 + 瞬态重试 ----------

    async def _throttle(self) -> None:
        async with self._init_lock:
            wait = self._last_init + self.min_interval - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_init = time.monotonic()

    async def _retry_transient(self, fn, what: str):
        for attempt in range(self.transient_retries + 1):
            await self._throttle()
            try:
                return await fn()
            except Exception as exc:  # noqa: BLE001
                if attempt < self.transient_retries and is_transient(exc):
                    backoff = 2.0 * (2 ** attempt) + random.uniform(0, 0.5)
                    logger.warning("%s 瞬态错误（第 %d 次重试，等待 %.1fs）：%s",
                                   what, attempt + 1, backoff, exc)
                    await asyncio.sleep(backoff)
                    continue
                raise

    # ---------- 目录 ----------

    async def ensure_pid(self, cloud_dir: str) -> int:
        path = normalize_cloud_path(cloud_dir)
        if path == "/":
            return 0
        if path in self._cid_mem:
            return self._cid_mem[path]
        cached = self.store.cid_get(path)
        if cached is not None:
            self._cid_mem[path] = cached
            return cached
        async with self._makedirs_lock:                     # 全局锁防并发重复建目录
            if path in self._cid_mem:
                return self._cid_mem[path]
            resp = await self.client.fs_makedirs(path, async_=True)
            data = resp.get("data") or []
            cid = None
            if isinstance(data, list) and data:
                last = data[-1]                              # 路径段列表，最后一段是目标目录
                if isinstance(last, dict):
                    cid = last.get("id") or last.get("cid")
            if cid is None:
                raise RuntimeError(f"fs_makedirs 无法解析 cid：{path} / {str(resp)[:200]}")
            cid = int(cid)
            self._cid_mem[path] = cid
            self.store.cid_set(path, cid)
            logger.info("目录就绪 %s (cid=%s)", path, cid)
            return cid

    async def forget_pid(self, cloud_dir: str) -> None:
        """秒传报 10014（云端目录不存在）时清缓存。"""
        self._cid_mem.clear()

    # ---------- 秒传 ----------

    async def rapid(self, file_path: str, size: int, sha1: str, pid: int) -> tuple[bool, dict]:
        fname = os.path.basename(file_path)

        async def read_range(range_spec: str) -> bytes:
            spec = range_spec.strip().removeprefix("bytes=")
            start_s, _, end_s = spec.partition("-")
            start = int(start_s or 0)
            end = int(end_s) if end_s else None
            with open(file_path, "rb") as f:
                f.seek(start)
                return f.read() if end is None else f.read(end - start + 1)

        async def call():
            return await self.client.upload_file_init(
                filename=fname, filesize=size, filesha1=sha1,
                read_range_bytes_or_hash=read_range, pid=pid, async_=True,
            )

        resp = await self._retry_transient(call, f"秒传初始化 {fname}")
        data = resp.get("data") or {}
        hit = resp.get("reuse") is True and resp.get("state") is True
        info = {
            "status": resp.get("status"),
            "file_id": str(data.get("file_id") or data.get("id") or data.get("pickcode")
                           or resp.get("pickcode") or ""),
        }
        if not hit:
            info["reason"] = (resp.get("message") or resp.get("error") or resp.get("msg")
                              or f"status={resp.get('status')}")
        return hit, info

    # ---------- 分块上传（断点续传） ----------

    async def multipart(self, file_path: str, pid: int, sha1: str, record: dict) -> dict:
        from p115client.tool import P115MultipartUpload
        from p115oss import (oss_multipart_upload_init, oss_multipart_upload_part_iter,
                             upload_file_init as oss_upload_file_init)
        from p115oss.oss import oss_url

        endpoint = "https://oss-cn-shenzhen.aliyuncs.com"
        headers = {"cookie": self.client.cookies}

        up, parts = None, []
        if record.get("upload_id") and record.get("upload_oss_url") and record.get("upload_callback"):
            try:
                up = P115MultipartUpload(record["upload_oss_url"], file_path,
                                         json.loads(record["upload_callback"]), record["upload_id"])
                parts = list(await up.list_parts(async_=True, headers=headers) or [])
                logger.info("断点续传：%s 已有 %d 个分块", os.path.basename(file_path), len(parts))
            except Exception as exc:  # noqa: BLE001
                logger.warning("断点失效（%s），从头开始：%s", os.path.basename(file_path), exc)
                up, parts = None, []

        if up is None:
            async def init():
                return await oss_upload_file_init(
                    file=file_path, pid=pid, filename=os.path.basename(file_path),
                    filesha1=sha1, filesize=os.path.getsize(file_path),
                    endpoint=endpoint, async_=True, headers=headers,
                )
            resp = await self._retry_transient(init, f"上传初始化 {os.path.basename(file_path)}")
            data = resp.get("data") or {}
            url = resp.get("url") or data.get("url") or ""
            if not url:
                url = oss_url(data.get("object"), bucket=data.get("bucket"), endpoint=endpoint)
            callback = data.get("callback") or resp.get("callback")
            upload_id = await oss_multipart_upload_init(url, async_=True, headers=headers)
            up = P115MultipartUpload(url, file_path, callback, upload_id)
            self.store.set(record["path"], upload_id=upload_id, upload_oss_url=url,
                           upload_callback=json.dumps(callback), upload_partsize=self.partsize)
            parts = []

        done = list(parts)
        total = max(os.path.getsize(file_path), 1)
        sent = sum(int(p.get("size") or 0) for p in done)

        def reporthook(delta: int):
            nonlocal sent
            sent += int(delta or 0)
            self.store.set(record["path"], progress=round(min(sent / total * 100, 99.9), 2))

        with open(file_path, "rb") as fh:
            async for part in oss_multipart_upload_part_iter(
                up.url, up.upload_id, fh,
                part_number_start=len(parts) + 1, partsize=self.partsize,
                endpoint=endpoint, reporthook=reporthook, async_=True, headers=headers,
            ):
                done.append(part)

        result = await up.complete(done, async_=True, headers=headers)
        return result if isinstance(result, dict) else {"state": True}


class StubP115:
    """--dry-run：不联网，模拟 115 行为，用于验证扫描/状态机/目录映射。"""

    def __init__(self, cfg: dict, store, fail_first: int = 2):
        self.cfg = cfg
        self.store = store
        self.partsize = int(cfg["upload"]["partsize_mb"]) * 1024 * 1024
        self.fail_first = fail_first
        self._cid = 700
        self._mem: dict[str, int] = {}
        self._rapid_calls: dict[str, int] = {}

    async def ensure_pid(self, cloud_dir: str) -> int:
        path = normalize_cloud_path(cloud_dir)
        if path not in self._mem:
            self._cid += 1
            self._mem[path] = self._cid
            logger.info("[dry-run] fs_makedirs(%s) → cid=%s", path, self._cid)
        return self._mem[path]

    async def forget_pid(self, cloud_dir: str) -> None:
        return None

    async def rapid(self, file_path: str, size: int, sha1: str, pid: int) -> tuple[bool, dict]:
        n = self._rapid_calls.get(file_path, 0) + 1
        self._rapid_calls[file_path] = n
        await asyncio.sleep(0)
        if n > self.fail_first:
            logger.info("[dry-run] 秒传命中 %s（pid=%s，仅发过 %d 次校验请求）",
                        os.path.basename(file_path), pid, n)
            return True, {"status": 2, "file_id": f"stub{n}"}
        logger.info("[dry-run] 秒传未命中 %s（第 %d 次，未上传任何数据）",
                    os.path.basename(file_path), n)
        return False, {"status": 1, "reason": "dry-run 模拟未命中"}

    async def multipart(self, file_path: str, pid: int, sha1: str, record: dict) -> dict:
        size = os.path.getsize(file_path)
        blocks = max(1, (size + self.partsize - 1) // self.partsize)
        logger.info("[dry-run] 分块上传 %s：%d 块 × %dMB → pid=%s",
                    os.path.basename(file_path), blocks, self.partsize // 1024 // 1024, pid)
        for i in range(blocks):
            self.store.set(record["path"], progress=round((i + 1) / blocks * 100, 2))
            await asyncio.sleep(0)
        return {"state": True, "file_id": f"stub-upload-{blocks}"}
