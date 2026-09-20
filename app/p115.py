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
import re
import time

logger = logging.getLogger("upilot.p115")

TRANSIENT_HTTP_CODES = (401, 403, 429, 500, 502, 503, 504)

# 视频扩展名 + 「同一集」归一化。洗板（重下同集不同版本）后云端会同时留下旧版与
# 新版，清理时按「剧名 + SxxExx + 扩展名」判同一集 —— 画质标签
# （DV / HDR / WEB-DL / 站点名…）与集号之后的内容一律不参与比较。
VIDEO_SUFFIXES = {".mkv", ".mp4", ".ts", ".m2ts", ".mts", ".avi", ".mov", ".wmv",
                  ".flv", ".rmvb", ".iso", ".vob", ".mpg", ".mpeg", ".divx", ".webm"}
_EP_RE = re.compile(r"[Ss](\d{1,2})\s*[Ee](\d{1,3})")
_EP_CN_RE = re.compile(r"第\s*(\d{1,3})\s*[集话話]")


def episode_key(name: str) -> str | None:
    """把文件名归一成「同一集」的键；认不出季集信息就返回 None（不参与清理）。

    '甲剧 (2026) [tmdbid=100001] S01E31 [2160p H265 DDP 5.1 WEB-DL DV TX DemoWeb].mkv'
    '甲剧 (2026) [tmdbid=100001] S01E31 [2160p H265 DDP 5.1 WEB-DL TX DemoWeb].mkv'
      → 同一个键 '甲剧 (2026)|s01e031|.mkv'

    点分命名同理：'Show.S01E31.2160p.DV.mkv' → 'show|s01e031|.mkv'
    """
    stem, ext = os.path.splitext(name or "")
    ext = ext.lower()
    if ext not in VIDEO_SUFFIXES:
        return None
    text = re.sub(r"\[[^\]]*\]", " ", stem)       # 剥掉 [tmdbid=…] / [2160p … DV …] 这类标签
    text = re.sub(r"[._]+", " ", text)            # 点分命名也归一成空格
    text = re.sub(r"\s+", " ", text).strip().lower()
    m = _EP_RE.search(text)
    if m:
        show = text[:m.start()].strip()
        return f"{show}|s{int(m.group(1)):02d}e{int(m.group(2)):03d}|{ext}"
    m = _EP_CN_RE.search(text)
    if m:
        show = text[:m.start()].strip()
        return f"{show}|e{int(m.group(1)):03d}|{ext}"
    return None


def is_transient(exc: Exception) -> bool:
    from urllib.error import HTTPError
    return isinstance(exc, HTTPError) and exc.code in TRANSIENT_HTTP_CODES


def normalize_cloud_path(p: str) -> str:
    return "/" + "/".join(x for x in (p or "").replace("\\", "/").split("/") if x)


def parse_dir_cid(resp) -> int | None:
    """从 fs_makedirs 的响应里解析目标目录 cid。

    115 同一接口有两种返回形态：
    - 整条路径新建 → data 是路径段列表，最后一段的 id 就是目标目录；
    - 目录已存在（没啥可建）→ data 只有一个 {"file_id": "...", "is_private": "0"}。
    只认第一种的话，同一个目录第二次进来（或目录本来就存在）会直接解析失败，
    线上表现就是「fs_makedirs 无法解析 cid」反复报错。这里按上游
    p115_path._parse_cid_from_response 的顺序做三层兜底。
    """
    if not isinstance(resp, dict) or resp.get("state") is False:
        return None
    data = resp.get("data")
    cands: list = []
    if isinstance(data, list) and data and isinstance(data[-1], dict):
        last = data[-1]
        cands += [last.get("id"), last.get("cid"), last.get("file_id")]
    if isinstance(data, dict):
        cands += [data.get("id"), data.get("cid"), data.get("file_id")]
    cands += [resp.get("id"), resp.get("cid"), resp.get("file_id")]
    for cand in cands:
        if cand is None or cand == "":
            continue
        try:
            return int(cand)
        except (TypeError, ValueError):
            continue
    return None


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
        # 目录内容缓存：{pid: (条目, 取回时间)}。上游实测「同一目录被反复列会触发 115 限流（405）」，
        # 洗板清理是「一个文件列一次目录」，整季下载会连打，所以加 TTL 复用。
        self._dir_cache: dict[int, tuple[list, float]] = {}
        self._dir_cache_ttl = 30.0
        # 115 的 fs_delete 文档明确要求「不要并发执行」（删除与从回收站还原互斥），
        # 而 upload.concurrency 允许两个文件同时走到清理，所以删除必须串行化。
        self._delete_lock = asyncio.Lock()

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
            cid = parse_dir_cid(resp)
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

    # ---------- 目录内容 / 删除（洗板清理用） ----------

    async def list_dir(self, pid: int, use_cache: bool = True) -> list[dict]:
        """列出目录里的**文件**条目（分页，跳过子目录）。

        实测（真 115）：文件条目带 `fid`（自身 id）+ `ico`（扩展名，如 "mkv"）+ `s`（字节数）；
        文件夹条目**没有** `fid`/`ico`，它的身份 id 在 `cid` 键上。
        所以判「是文件」只看 `fid` 在不在 —— 拿 `ico == "folder"` 当过滤条件是错的
        （那个键在文件夹条目上压根不存在，等于没过滤）。
        """
        key = int(pid)
        if use_cache:
            hit = self._dir_cache.get(key)
            if hit and time.monotonic() - hit[1] < self._dir_cache_ttl:
                return list(hit[0])
        out: list[dict] = []
        offset = 0
        page = 1150                                     # 115 单页上限
        while True:
            resp = await self.client.fs_files(
                {"cid": str(pid), "offset": offset, "limit": page,
                 "show_dir": 1, "cur": 1}, async_=True)
            data = (resp or {}).get("data")
            if isinstance(data, dict):
                rows = data.get("list") or []
                total = int(data.get("count") or (resp or {}).get("count") or len(rows))
            elif isinstance(data, list):
                rows = data
                total = int((resp or {}).get("count") or len(rows))
            else:
                rows = (resp or {}).get("list") or []
                total = int((resp or {}).get("count") or len(rows))
            for row in rows:
                if isinstance(row, dict) and row.get("fid") not in (None, ""):
                    out.append(row)
            offset += page
            if not rows or offset >= total:
                break
        self._dir_cache[key] = (list(out), time.monotonic())
        return out

    async def delete_file(self, file_id) -> bool:
        """把文件移进 115 回收站（`/rb/delete`，可恢复，非物理删除）。

        115 的接口文档明确要求「不要并发执行」（删除与从回收站还原互斥），
        而 upload.concurrency 允许两个文件同时走到清理，所以用实例锁串行化。
        """
        async with self._delete_lock:
            resp = await self.client.fs_delete({"fid": str(file_id)}, async_=True)
        return bool(isinstance(resp, dict) and resp.get("state"))

    async def cleanup_previous(self, pid: int, keep_name: str, keep_size: int,
                               max_delete: int, keep_fid: str = "") -> dict:
        """洗板清理：同一集只留刚上传那条，其余同集旧版本移进回收站。

        三道「别动它」：标识命中（最可靠，见下）、文件名完全相同、大小完全相同。
        标识那道要注意：秒传落库的 file_id 实测是 **pickcode**（形如 abcd0demo0pick001），
        而目录条目里的 `fid` 是数字串 —— 所以必须同时比对 `fid` / `pc` / `file_id`。
        返回 {"deleted": [文件名…], "note": 说明}；认不出季集 / 没有旧版本 / 多于上限都只回说明。
        """
        key = episode_key(keep_name)
        if key is None:
            return {"deleted": [], "note": "认不出季集（文件名里没有 SxxExx），跳过"}
        keep_fid = str(keep_fid or "").strip()
        if keep_fid in ("0", "None"):
            keep_fid = ""
        rows = await self.list_dir(pid)
        victims: list[tuple[str, str]] = []
        for row in rows:
            name = str(row.get("n") or row.get("name") or "")
            fid = row.get("fid") or row.get("file_id")
            if not name or fid in (None, ""):
                continue                                 # list_dir 已滤掉目录，这里再兜一层
            if keep_fid:
                marks = {str(row.get(k)) for k in ("fid", "pc", "file_id")
                         if row.get(k) not in (None, "")}
                if keep_fid in marks:
                    continue                             # 刚上传那条（按标识，最可靠）
            if name == keep_name:
                continue                                 # 刚上传那条（按名字）
            if episode_key(name) != key:
                continue
            try:
                size = int(row.get("s") or row.get("size") or 0)
            except (TypeError, ValueError):
                size = 0
            if keep_size and size == keep_size:
                continue                                 # 大小一模一样：更像刚传那条被改过名，别动
            victims.append((name, str(fid)))
        if not victims:
            return {"deleted": [], "note": "没有旧版本"}
        if max_delete > 0 and len(victims) > max_delete:
            return {"deleted": [],
                    "note": f"同集旧版本 {len(victims)} 个，超过上限 {max_delete}，本次不动手"}
        done: list[str] = []
        for name, fid in victims:
            try:
                if await self.delete_file(fid):
                    done.append(name)
                    logger.info("洗板清理：已移入回收站 %s", name)
                else:
                    logger.warning("洗板清理：删除失败 %s", name)
            except Exception as exc:  # noqa: BLE001
                logger.warning("洗板清理：删除出错 %s：%s", name, exc)
        if done:
            cache = getattr(self, "_dir_cache", None)    # 目录内容变了，缓存作废
            if isinstance(cache, dict):
                cache.pop(int(pid), None)
        return {"deleted": done, "note": f"清掉 {len(done)} 个同集旧版本"}

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

    # ---------- 洗板清理（dry-run 只打日志，不真删任何东西） ----------

    async def list_dir(self, pid: int) -> list[dict]:
        """dry-run 的「云端内容」：把已上传、且目标目录是本 pid 的记录还原成条目。"""
        cloud = next((p for p, c in self._mem.items() if int(c) == int(pid)), None)
        if cloud is None:
            return []
        rows, _ = self.store.list_tasks(limit=1000)
        return [{"n": os.path.basename(r["path"]), "s": r["size"],
                 "fid": f"stub-{r['path']}", "ico": ""}
                for r in rows
                if r["target_dir"] == cloud and r["status"] == "已上传"]

    async def delete_file(self, file_id) -> bool:
        logger.info("[dry-run] fs_delete(%s) → 移入 115 回收站", file_id)
        return True

    # 判断逻辑与真实客户端共用（只依赖 list_dir / delete_file）
    cleanup_previous = P115.cleanup_previous

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
