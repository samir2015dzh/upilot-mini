"""115 侧封装：目录（保留相对结构）、秒传、分块上传（断点续传）、节流与瞬态重试。

- 秒传：upload_file_init(filename, filesize, filesha1, read_range_bytes_or_hash, pid)
  判定 reuse is True and state is True 才算命中；file_id 可能为 0，退回 pickcode。
- 目录：fs_makedirs 建整条链拿 cid（内存 + SQLite 双缓存 + 全局锁）
- 分块：P115MultipartUpload(url, path, callback, upload_id) + list_parts 校验断点
  + oss_multipart_upload_part_iter（不能用 iter_upload，它内部 url= 关键字会撞 positional-only）
"""
from __future__ import annotations

import asyncio
import datetime
import errno as _errno
import json
import logging
import os
import random
import re
import time

logger = logging.getLogger("upilot.p115")

# 「瞬态错误」判据（第 46~47 处，实测修正）。
#
# 证据链 = 拆 `p115client` 0.0.9.6.5.1 wheel 全源码（`pip download --python-version 312
# --no-deps`）：`HTTPError` / `urllib.error` / `requests.` 命中数**全为 0**。
# 它实际 `from urllib3_future_request import request`，而 115 的业务错误由
# `check_response()` 转成 **`P115OSError(P115Error, OSError)`** 抛出 ——
# 「稍后再试」的语义全落在 `OSError.errno` 上（其源码 errno 映射：
# EAGAIN=操作过于频繁、EBUSY=忙（115 的复制/移动/删除/还原同时只允许一个）、
# EAUTH=请重新登录/登录失效）。
#
# ⇒ 原先只认 `urllib.error.HTTPError` 的 `is_transient` **从未命中过**（恒 False），
#   瞬态错误一律顶到任务级重试；且旧 `TRANSIENT_HTTP_CODES` 把 **401/403 也算瞬态**
#   —— 认证失败被拒后原地自动重试，方向恰好反了，正是风控最忌讳的行为。
#
# 认证类（EAUTH）**不在**瞬态集合里：登录失效/凭据错误立即上抛，走任务级错误处理。
TRANSIENT_ERRNOS = tuple(
    getattr(_errno, name) for name in (
        "EAGAIN", "EWOULDBLOCK", "EBUSY", "ETIMEDOUT", "ECONNRESET",
        "ECONNABORTED", "EPIPE", "EHOSTUNREACH", "ENETUNREACH", "ENETRESET",
    ) if getattr(_errno, name, None) is not None
)

# 兼容分支：万一将来传输层真换成 urllib，按 HTTP 状态码判；
# **401/403 已剔除** —— 认证失败绝不自动重试。
TRANSIENT_HTTP_CODES = (429, 500, 502, 503, 504)

# 翻目录的硬上限（条目数）。
# 与「页数上限」不同：页数上限会在「服务端单页很小 + 目录很大」时静默截断
# （实测每页 7 条、200 页上限 ⇒ 只读到 1400/3000 就停了），所以这里按**条目数**兜底，
# 并在触顶时明确记 warning —— 截断必须是可见的。
MAX_DIR_ITEMS = 50000

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


def upload_mark(result) -> str:
    """从 115 的接口响应里取「这条文件在云端的标识」（fid 或 pickcode，有啥取啥）。

    实测：秒传（upload_file_init）返回的是 **pickcode**（形如 abcd0demo0pick001），
    分块完成的 OSS 回调响应里带的是 `pick_code`/`pickcode`、**没有 file_id** ——
    不统一在这里取一次，分块路径落库的标识就是空的，「别删刚上传那条」的护栏会缺一道。
    """
    if not isinstance(result, dict):
        return ""
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    for cand in (result.get("file_id"), data.get("file_id"),
                 result.get("id"), data.get("id"),
                 result.get("pickcode"), data.get("pickcode"),
                 result.get("pick_code"), data.get("pick_code")):
        if cand not in (None, "", 0, "0"):
            return str(cand)
    return ""


def is_transient(exc: Exception) -> bool:
    """这个异常值不值得「原地等一会儿再试」。

    主判据走 `OSError.errno` —— `p115client` 的异常体系（`P115OSError` 及其子类）
    继承 `OSError`，errno 就是它给的语义：忙 / 操作过于频繁 / 网络抖动 → 软重试
    （`_retry_transient` 里 2/4/8s 退避）；EAUTH / EINVAL / EIO 等 → 立即上抛。

    兼容分支保留 `urllib.error.HTTPError`（万一将来换传输层），但其状态码集合里
    已剔除 401/403。
    """
    if isinstance(exc, OSError) and getattr(exc, "errno", None) in TRANSIENT_ERRNOS:
        return True
    from urllib.error import HTTPError
    return isinstance(exc, HTTPError) and exc.code in TRANSIENT_HTTP_CODES


def normalize_cloud_path(p: str) -> str:
    """云端路径规范化：合并连续 `/`、丢弃 `.` 与 `..` 段。

    必须解析 `..`（不是只折叠 `/`）。本函数是**发给 115 之前的最后一道**，
    而配置侧有三处能带进 `..`：`paths.map` 的 `to`、`paths.target_root`、
    以及手改过的 config.yaml。原实现只做 `/` 折叠，`..` 会原样打到云端 ——
    实测 `TVs => /TVs/../../etc` 会算出目标目录 `/TVs/../../etc/Show`，
    于是云端建出带 `..` 的怪目录，而 symedia 按正常路径建 strm，
    **文件传上去了却永远不出现在媒体库**，界面上毫无提示。
    退到根再遇到 `..` 就忽略（不越界），保证结果始终是绝对的、不含 `.`/`..`。
    """
    out: list[str] = []
    for seg in (p or "").replace("\\", "/").split("/"):
        if not seg or seg == ".":
            continue
        if seg == "..":
            if out:
                out.pop()
            continue
        out.append(seg)
    return "/" + "/".join(out)


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


def _part_field(part: dict, *names: str) -> int:
    for n in names:
        v = part.get(n)
        if v is not None:
            try:
                return int(v)
            except (TypeError, ValueError):
                return 0
    return 0


def resume_prefix(parts: list[dict], partsize: int, file_size: int) -> tuple[list[dict], int]:
    """从云端已传分块里挑出**可以直接沿用**的那一段，返回 (分块列表, 已传字节数)。

    只认从 1 开始、编号连续、每块恰好 partsize 字节的前缀（最后一块可以更短，但那样
    必须正好到文件末尾）。中间缺块、大小对不上的都不沿用 —— 之后的数据按原编号重传
    （OSS 同编号重传会覆盖旧分块）。返回的字节数就是本地文件要 seek 到的偏移。
    """
    ordered = sorted(parts or [], key=lambda p: _part_field(p, "PartNumber", "part_number"))
    keep: list[dict] = []
    skip = 0
    for i, p in enumerate(ordered, start=1):
        if _part_field(p, "PartNumber", "part_number") != i:
            break
        size = _part_field(p, "Size", "size")
        if size == partsize and skip + size <= file_size:
            keep.append(p)
            skip += size
            continue
        if 0 < size < partsize and skip + size == file_size:
            keep.append(p)               # 最后一块（不满 partsize），正好收尾
            skip += size
        break
    return keep, skip


class ProgressThrottle:
    """进度写库节流：距上次写入不足 interval 秒就只记在内存里，不写库。

    分块上传的回调粒度是 64KB，逐次写 SQLite（UPDATE + commit，抢同一把锁）
    在 10GB 文件上是十几万次写入，还会拖慢面板查询。面板本来就是轮询刷新，
    每秒一次足够（与 sha1_of 的进度回写同一口径）；收尾用 force=True 写最终值。
    """

    def __init__(self, write, interval: float = 1.0, cap: float = 99.9):
        self.write = write
        self.interval = interval
        self.cap = cap
        self.last_t = float("-inf")
        self.last_pct: float | None = None

    def update(self, pct: float, force: bool = False) -> None:
        pct = round(min(max(float(pct), 0.0), self.cap), 2)
        if pct == self.last_pct:
            return
        now = time.monotonic()
        if not force and now - self.last_t < self.interval:
            return
        self.write(pct)
        self.last_t = now
        self.last_pct = pct


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
        # 注意：TTL 只在读取时判断，过期条目不会自己消失 —— 每个条目是那个目录的
        # **完整文件列表**（几百个 dict）。媒体库有几百个目录时，长跑会把这些已过期的
        # 列表一直持在内存里，所以必须自己淘汰（见 list_dir 写入前）。
        self._dir_cache: dict[int, tuple[list, float]] = {}
        self._dir_cache_ttl = 30.0
        self._dir_cache_max = 64        # 最多缓存这么多个目录（dict 有序 = 近似 LRU）
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

    async def cached_pid(self, cloud_dir: str) -> int | None:
        """**只从缓存**解析目录 cid，绝不调 `fs_makedirs`。拿不到就返回 None。

        巡检（`sweep_cloud_episode_duplicates`）专用。为什么不能直接用 `ensure_pid`：
        它命中不了缓存时会 `fs_makedirs(path)` —— 对云端**早已不存在**的目录
        （比如库里留着一条早已被删掉的 `target_dir`）会**凭空建出一整条空目录树**。
        巡检是只读核对，不该有这种副作用。
        拿不到 cid 意味着这目录从没被本程序传过东西（传过就会有缓存），跳过是对的。
        """
        path = normalize_cloud_path(cloud_dir)
        if path == "/":
            return 0
        mem = getattr(self, "_cid_mem", None)
        if isinstance(mem, dict) and path in mem:
            return int(mem[path])
        try:
            cached = self.store.cid_get(path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("巡检：读 cid 缓存失败 %s：%s", path, exc)
            return None
        if cached is not None:
            if isinstance(mem, dict):
                mem[path] = int(cached)
            return int(cached)
        return None

    async def forget_pid(self, cloud_dir: str) -> None:
        """秒传报 10014（云端目录不存在）时清缓存，让下一次 ensure_pid 真正重建目录。

        内存和 SQLite **两份都要清**：ensure_pid 的顺序是「先看内存 → 再看库 →
        都没有才调 fs_makedirs」。只清内存的话，第二次进来立刻从库里读回同一个
        旧 cid，fs_makedirs 根本不会被调用 —— 那句「清缓存后重建目录」就成了空转，
        10014 会一轮一轮重复报下去，永远不会自愈。
        """
        path = normalize_cloud_path(cloud_dir)
        self._cid_mem.pop(path, None)
        try:
            self.store.cid_del(path)
        except AttributeError:          # 兼容没有 cid_del 的旧 store / 测试替身
            pass

    # ---------- 目录内容 / 删除（洗板清理用） ----------

    async def list_dir(self, pid: int, use_cache: bool = True) -> list[dict]:
        """列出目录里的**文件**条目（分页，跳过子目录）。

        实测（真 115）：文件条目带 `fid`（自身 id）+ `ico`（扩展名，如 "mkv"）+ `s`（字节数）；
        文件夹条目**没有** `fid`/`ico`，它的身份 id 在 `cid` 键上。
        所以判「是文件」只看 `fid` 在不在 —— 拿 `ico == "folder"` 当过滤条件是错的
        （那个键在文件夹条目上压根不存在，等于没过滤）。
        """
        key = int(pid)
        cache = getattr(self, "_dir_cache", None)
        if use_cache and isinstance(cache, dict):
            ttl = float(getattr(self, "_dir_cache_ttl", 30.0))
            hit = cache.get(key)
            if hit and time.monotonic() - hit[1] < ttl:
                return list(hit[0])
        out: list[dict] = []
        offset = 0
        page = 1150                                     # 115 单页上限
        seen_ids: set[str] = set()                      # 按 fid 去重（防服务端忽略 offset）
        prev_sig: tuple | None = None                   # 上一页的 fid 签名（检测「反复给同一批」）
        truncated = False
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
            if not rows:
                break                                   # 没有更多了

            # 服务端若忽略 offset、每页给同一批，翻页会永远转下去：用「本页 fid 签名
            # 与上一页相同」判定卡住，立刻停（这种情况代理/网关坏掉时会出现）。
            sig = tuple(str(r.get("fid")) for r in rows if isinstance(r, dict))
            if sig and sig == prev_sig:
                logger.warning("列目录 pid=%s 时服务端反复返回同一批（第 %d 条起），已停止翻页",
                               pid, offset)
                break
            prev_sig = sig

            for row in rows:
                if not isinstance(row, dict):
                    continue
                fid = row.get("fid")
                if fid in (None, ""):
                    continue                            # 文件夹条目（身份 id 在 cid 上）
                fid_s = str(fid)
                if fid_s in seen_ids:
                    continue                            # 去重：同一文件不重复入列
                seen_ids.add(fid_s)
                out.append(row)

            # 游标必须用**实际返回条数**推进，不能固定加 page：服务端有自己的单页上限时
            # （很常见），固定加 page 会把中间整段数据跳过。实测：2500 个文件、服务端每页
            # 只给 500 条时，请求序列 (0,1150)(1150,1150)(2300,1150) 只拿回 1200 条，**漏
            # 1300 条** —— 而 list_dir 正是洗板清理「找同集旧版」的依据，漏掉的旧版永远
            # 清不掉，且没有任何报错。
            offset += len(rows)
            if len(out) >= MAX_DIR_ITEMS:
                truncated = True
                break
            if offset >= total:
                break
        if truncated:
            # 截断必须可见：否则又是「静默少数据」。
            logger.warning("目录 pid=%s 条目超过 %d，本次只读了前 %d 条（旧版本可能因此漏清）",
                           pid, MAX_DIR_ITEMS, len(out))
        self._remember_dir(key, out)
        return out

    def _remember_dir(self, key: int, entries: list) -> None:
        """写入目录缓存，顺手淘汰：先清过期，再按插入顺序压到上限。

        TTL 只影响「能不能复用」，不会让条目消失；不主动淘汰的话，条目数只增不减
        （每个条目是该目录的完整文件列表），长跑就是一条稳定的内存增长曲线。
        属性一律 getattr 取值：测试里会手工构造 P115（走 __new__、不过 __init__），
        项目里对这类实例既有约定就是用 getattr 守卫。
        """
        cache = getattr(self, "_dir_cache", None)
        if not isinstance(cache, dict):
            return
        cap = max(1, int(getattr(self, "_dir_cache_max", 64)))
        ttl = float(getattr(self, "_dir_cache_ttl", 30.0))
        now = time.monotonic()
        if len(cache) >= cap:
            for k in [k for k, (_, ts) in cache.items() if now - ts >= ttl]:
                cache.pop(k, None)
            while len(cache) >= cap:
                cache.pop(next(iter(cache)), None)
        cache[key] = (list(entries), now)

    async def delete_file(self, file_id) -> bool:
        """把文件移进 115 回收站（`/rb/delete`，可恢复，非物理删除）。

        115 的接口文档明确要求「不要并发执行」（删除与从回收站还原互斥），
        而 upload.concurrency 允许两个文件同时走到清理，所以用实例锁串行化。
        """
        async with self._delete_lock:
            resp = await self.client.fs_delete({"fid": str(file_id)}, async_=True)
        return bool(isinstance(resp, dict) and resp.get("state"))

    async def cleanup_previous(self, pid: int, keep_name: str, keep_size: int,
                               max_delete: int, keep_fid: str = "",
                               keep_fids=None, keep_names=None) -> dict:
        """洗板清理：同一集只留最新版，其余同集**旧版本**移进回收站。

        三道「别动它」：标识命中（最可靠，见下）、文件名完全相同、大小完全相同。
        标识那道要注意：秒传落库的 file_id 实测是 **pickcode**（形如 abcd0demo0pick001），
        而目录条目里的 `fid` 是数字串 —— 所以必须同时比对 `fid` / `pc` / `file_id`。

        `keep_fids` / `keep_names` 是**额外**的保护集合（调用方传「同集其它仍在本地、
        且已上传成功」的版本）。没有它就会这样：同一集两个版本几乎同时传完，
        A 的清理把 B 当成旧版删掉、B 的清理把 A 删掉 ⇒ **云端同集一个不剩**，
        而本地两条都是「已上传」终态、strm 指向不存在的文件。本地已复现（串行与并发都会）。
        判据是「本地文件是否还在」：洗板时旧版的本地文件已被换掉/删除 ⇒ 不保护 ⇒ 照旧清理；
        同集两个都还在本地 ⇒ 都是正当新版本 ⇒ 都保护。

        返回 {"deleted": [文件名…], "note": 说明}；认不出季集 / 没有旧版本 / 多于上限都只回说明。
        """
        key = episode_key(keep_name)
        if key is None:
            return {"deleted": [], "note": "认不出季集（文件名里没有 SxxExx），跳过"}
        keep_ids: set[str] = set()
        for cand in [keep_fid, *(keep_fids or ())]:
            text = str(cand or "").strip()
            if text and text not in ("0", "None"):
                keep_ids.add(text)
        explicit_names: set[str] = {str(n) for n in (keep_names or ()) if n}
        auto_name = str(keep_name) if keep_name else ""
        rows = await self.list_dir(pid)

        def _marks(row: dict) -> set[str]:
            return {str(row.get(k)) for k in ("fid", "pc", "file_id")
                    if row.get(k) not in (None, "")}

        # 「刚上传那条」是否已被**标识**精确锁定？锁定了的话，自动加入的 keep_name
        # 必须让位给标识判定。
        #
        # 不让位的后果（实测）：把 keep_name 自动塞进保护集后
        #   `if name in keep_names_set: continue`
        # 会把**所有与 keep_name 同名的条目**一并保护住。而
        #   ① 云端同集同名重复（用户实际遇到：两条逐字同名的 S01E33）
        #   ② 115 同名自动改名（新版成 "xxx (1).mkv"，**旧的那条名字正好等于 keep_name**）
        # 这两种情形下旧条目名恰好就是 keep_name ⇒ 永远跳过 ⇒ 洗板清理静默失效：
        # 面板开关是开着的、日志里连一行都没有。
        #
        # 注意区分两类保护：
        #   - `keep_name`（自动）：只是「无标识时的兜底」，有标识时让位；
        #   - `explicit_names`（调用方显式传的同集其它正当版本）：**始终**保护，
        #     因为那确实是另一条要留下的文件，不能因为同名就连它一起删。
        hit_by_id = bool(keep_ids) and any(_marks(r) & keep_ids for r in rows)
        same_name_others = [r for r in rows
                            if auto_name and str(r.get("n") or r.get("name") or "") == auto_name
                            and not (_marks(r) & keep_ids)] if auto_name else []

        victims: list[tuple[str, str]] = []
        same_episode_all = [r for r in rows
                            if episode_key(str(r.get("n") or r.get("name") or "")) == key]
        for row in rows:
            name = str(row.get("n") or row.get("name") or "")
            fid = row.get("fid") or row.get("file_id")
            if not name or fid in (None, ""):
                continue                                 # list_dir 已滤掉目录，这里再兜一层
            if keep_ids and _marks(row) & keep_ids:
                continue                                 # 刚上传那条 / 同集正当版本（按标识，最可靠）
            if name in explicit_names:
                continue                                 # 调用方显式保护的正当版本，始终让位
            # 标识已把「刚上传那条」唯一确定 ⇒ 本条同名条目是重复项/旧版，
            # 不再走名字与大小两道闸门（否则同名旧版永远清不掉）。
            locked = bool(hit_by_id and auto_name and name == auto_name)
            if not locked and auto_name and name == auto_name:
                continue                                 # 无标识时的兜底：别动同名条目
            if episode_key(name) != key:
                continue
            try:
                size = int(row.get("s") or row.get("size") or 0)
            except (TypeError, ValueError):
                size = 0
            if not locked and keep_size and size == keep_size:
                continue                                 # 大小一模一样：更像刚传那条被改过名，别动
            victims.append((name, str(fid)))
        if not victims:
            # 云端同名多条却没动手：必须**可见**（原来只说「没有旧版本」，
            # 看日志会以为没事，其实是有重复清不掉）。
            if same_name_others:
                why = ("这次没有可用的文件标识（file_id/pickcode），分不清哪条是新的"
                       if not keep_ids else "同名条目在保护名单里")
                return {"deleted": [],
                        "note": (f"云端同集有 {len(same_name_others)} 条与本次上传同名的条目，"
                                 f"{why}，本次不动手")}
            # 同集实际有多条、但一条都没被判成旧版 —— 也要说出来。
            # 用户真机报障的场景正是这一类：云端 `HDR` 与无 `HDR` 两条同集文件都在，
            # 调用方把另一条当成「同批正当版本」保护起来 ⇒ 一条都不删。
            # 原来这里返回静默的「没有旧版本」，而调用方又刻意不打印这个 note
            # ⇒ 界面上开关开着、日志里连一行都没有，「说生效、实际没生效」。
            if len(same_episode_all) > 1:
                kept = [str(r.get("n") or r.get("name") or "") for r in same_episode_all]
                return {"deleted": [],
                        "note": (f"云端同集共 {len(same_episode_all)} 条，"
                                 f"全部判为正当版本（同批保护或本次上传那条），本次不动手："
                                 + "、".join(kept[:5]))}
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

    # ---------- 同集清理（**纯云端时间**判据） ----------
    # 用户明确的要求（原话）：「你只能在 115 云端去核对，本地肯定是没有的，因为本地的文件
    # 在秒传完已经被删掉了」。实测生产正是 `delete_after_upload=True`：每个上传成功的版本
    # 本地文件都被收尾删掉、库里 `source_deleted=1` ⇒ 任何「本地文件在不在」的判据都会把
    # 同集**所有**版本当成正当版本保护起来，清理永久空转（实测 2 小时内 `清理|洗板|旧版`
    # 零行，云端 E33 一直是 18:11 与 19:15 两条）。
    # 所以判据只用 115 条目自身的时间字段。
    _CLOUD_TS_KEYS = ("te", "tp", "tu", "pt", "uppt", "user_ptime", "ptime",
                      "user_utime", "utime", "mtime", "up_time", "t")

    @classmethod
    def cloud_ts(cls, row: dict) -> float:
        """取云端条目的时间戳（越大越新）。取不到返回 0。

        实测（真 115）文件条目带：
          `te` / `tp` / `tu` = epoch 秒（如 `1789989329` = 2026-09-21 19:15:29 +0800）
          `t`  = `"YYYY-MM-DD HH:MM"`（本地时区，只到分钟）
        所以优先 `te`（秒级最精确），字符串日期作兜底。
        """
        for k in cls._CLOUD_TS_KEYS:
            v = row.get(k)
            if v in (None, ""):
                continue
            try:
                n = float(v)
            except (TypeError, ValueError):
                s = str(v).strip()
                try:
                    fmt = "%Y-%m-%d %H:%M:%S" if s.count(":") == 2 else "%Y-%m-%d %H:%M"
                    return datetime.datetime.strptime(s, fmt).timestamp()
                except Exception:  # noqa: BLE001
                    continue
            if n > 0:
                return n
        return 0.0

    async def cleanup_episode_newest(self, pid: int, keep_name: str,
                                     max_delete: int = 0, keep_fid: str = "") -> dict:
        """同集只留**云端时间最新**的那条，其余移入 115 回收站。

        这是用户需求的直接实现，也是唯一不依赖本地的判据。判据只用 115 条目自身的时间：
          - 取同集里时间最大的那条为「要留的」（后秒传上去的那个）；
          - 时间并列 / 全部取不到时间 ⇒ **保守不动手**，并返回可见 note（不静默）；
          - `keep_fid` 命中「本次刚上传那条」时，若它**不是**时间最新（说明同集有更晚
            上传的版本，或者用户点的是老版本那条的「重新排队」）⇒ **让位不动手**，
            否则会把真正的最新版当旧版删掉，结果与用户预期相反。
        最后一点很关键：测试替身的云端条目没有时间字段 ⇒ 天然「不动手」，
        而真机（有 `te`）能正确清理 —— 同一套代码在两边都安全。
        """
        key = episode_key(keep_name)
        if key is None:
            return {"deleted": [], "note": "认不出季集（文件名里没有 SxxExx），跳过"}
        # `use_cache=False`：必须拿**新鲜**的目录内容。
        # 缓存 TTL 是 30 秒，而同一目录可能刚被另一集的清理/秒传流程列过 —— 那时
        # 本次刚上传的那条还不在缓存列表里，会出现「刚传完、旧的却没被清掉」的落空
        # （5 分钟巡检能兜底，但用户预期是传完就清）。删是破坏性动作，宁可多列一次。
        rows = await self.list_dir(pid, use_cache=False)
        same = [r for r in rows
                if episode_key(str(r.get("n") or r.get("name") or "")) == key]
        if not same:
            # 云端同集一条都没匹配上 —— 很可能是**本地文件名与云端文件名形式不同**
            # （本地 `甲剧.S01E33.HDR.mkv` vs 云端 `甲剧 (2026) [tmdbid=100001] S01E33 […].mkv`
            # ⇒ 剧名段不同 ⇒ 键不同）。这种情况必须**可见**：否则又是「开关开着、日志里
            # 一行都没有」的老问题。按云端条目**互相分组**兜一遍，把线索写进 note。
            bykey: dict[str, list[dict]] = {}
            for r in rows:
                k2 = episode_key(str(r.get("n") or r.get("name") or ""))
                if k2:
                    bykey.setdefault(k2, []).append(r)
            dup = {k2: v for k2, v in bykey.items() if len(v) > 1}
            if dup:
                hit = next((k2 for k2, v in dup.items()
                            if any(str(r.get("n") or "") == str(keep_name) for r in v)), "")
                return {"deleted": [],
                        "note": (f"本地文件名与云端对不上（本地键 {key}，云端同集多条的组："
                                 + "、".join(sorted(dup))
                                 + (f"；其中 {hit} 含本次上传那条" if hit else "")
                                 + "），本次不动手")}
            return {"deleted": [], "note": "没有旧版本"}
        if len(same) < 2:
            return {"deleted": [], "note": "没有旧版本"}
        ts = [(self.cloud_ts(r), r) for r in same]
        newest = max(t for t, _ in ts)
        if newest <= 0:
            return {"deleted": [],
                    "note": (f"云端同集 {len(same)} 条，但条目没有可用时间字段，"
                             f"分不清哪条是后传的，本次不动手")}
        top = [r for t, r in ts if t == newest]
        if len(top) != 1:
            return {"deleted": [],
                    "note": (f"云端同集 {len(same)} 条，最新那条的时间不唯一"
                             f"（{len(top)} 条并列），本次不动手")}
        # 护栏（纯云端）：本次刚上传那条若不是时间最新 ⇒ 让位（见 docstring 第三条）
        mark = str(keep_fid or "").strip()
        if mark:
            mine = [t for t, r in ts
                    if mark in (str(r.get("pc") or ""), str(r.get("fid") or ""))]
            if mine and max(mine) < newest:
                return {"deleted": [],
                        "note": ("本次上传那条不是云端最新的（同集另有更晚上传的版本），"
                                 "交给它处理，本次不动手")}
        victims = [(str(r.get("n") or r.get("name") or ""),
                    str(r.get("fid") or r.get("file_id") or ""))
                   for t, r in ts if t < newest]
        if not victims:
            return {"deleted": [], "note": "没有旧版本"}
        if max_delete > 0 and len(victims) > max_delete:
            return {"deleted": [],
                    "note": f"同集旧版本 {len(victims)} 个，超过上限 {max_delete}，本次不动手"}
        done: list[str] = []
        for name, fid in victims:
            if not fid:
                continue
            try:
                if await self.delete_file(fid):
                    done.append(name)
                    logger.info("同集清理：已移入 115 回收站 %s（保留云端时间最新那条）", name)
                else:
                    logger.warning("同集清理：删除失败 %s", name)
            except Exception as exc:  # noqa: BLE001
                logger.warning("同集清理：删除出错 %s：%s", name, exc)
        if done:
            cache = getattr(self, "_dir_cache", None)
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

        # 续传必须用**建断点时**的分块大小：两次运行之间用户改了 partsize_mb，
        # 按新值切块会让已传分块与本地偏移对不上。
        partsize = self.partsize
        if parts:
            partsize = int(record.get("upload_partsize") or 0) or self.partsize
        file_size = os.path.getsize(file_path)
        done, skip = resume_prefix(parts, partsize, file_size)
        if parts and len(done) != len(parts):
            logger.warning("断点续传：%s 云端 %d 个分块里只有前 %d 个连续可用，其余重传",
                           os.path.basename(file_path), len(parts), len(done))
        total = max(file_size, 1)
        sent = skip
        progress = ProgressThrottle(lambda pct: self.store.set(record["path"], progress=pct))
        progress.update(sent / total * 100, force=bool(skip))

        def reporthook(delta: int):
            # p115oss 每读 64KB 回调一次：10GB ≈ 16 万次。只在内存里累加，
            # 由 ProgressThrottle 按时间/百分比节流后才写库。
            nonlocal sent
            sent += int(delta or 0)
            progress.update(sent / total * 100)

        with open(file_path, "rb") as fh:
            # 【第 48 处】p115oss 的 part_iter 不会自己跳过已传数据（其文档原话：
            # 「如果需要跳过一定的数据，请提前处理好」）。原来句柄停在偏移 0、分块编号却
            # 从 len(parts)+1 开始 ⇒ 第 N+1 块传的是文件开头的数据，云端拼出来是坏文件。
            fh.seek(skip)
            async for part in oss_multipart_upload_part_iter(
                up.url, up.upload_id, fh,
                part_number_start=len(done) + 1, partsize=partsize,
                endpoint=endpoint, reporthook=reporthook, async_=True, headers=headers,
            ):
                done.append(part)
        progress.update(sent / total * 100, force=True)

        result = await up.complete(done, async_=True, headers=headers)
        if not isinstance(result, dict):
            return {"state": True}
        # OSS 完成后的 115 回调响应里带的是 pick_code/pickcode、没有 file_id ——
        # 不补这一手，分块路径落库的标识就是空的，「别删刚上传那条」那道护栏会缺一角
        if not result.get("file_id"):
            mark = upload_mark(result)
            if mark:
                result["file_id"] = mark
        return result


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

    async def list_dir(self, pid: int, use_cache: bool = True) -> list[dict]:
        """dry-run 的「云端内容」：把已上传、且目标目录是本 pid 的记录还原成条目。

        `use_cache` 只为与真实 `P115.list_dir` 签名一致（dry-run 没有缓存，
        忽略该参数）—— 调用方现在会传 `use_cache=False` 要求新鲜数据。
        """
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
    # 纯云端时间判据同样共用（stub 条目无 te/tp/tu/t ⇒ cloud_ts 返回 0 ⇒ 自然「保守不动手」，
    # 所以 dry-run 仍然安全）。不 alias 的话会走 Uploader 的 except AttributeError 回退老路径，
    # 于是 dry-run 验证的还是老判据 —— 补上这行让两边判据一致。
    cleanup_episode_newest = P115.cleanup_episode_newest

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
