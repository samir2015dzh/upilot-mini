"""目录扫描：稳定判定 + 扩展名/目录过滤 + 去重入队（不重复读盘、不重复上传）。"""
from __future__ import annotations

import logging
import os
import time

logger = logging.getLogger("upilot.scan")

# 下载器常用的「成品文件旁边的控制文件」后缀。成品往往已经是最终文件名，
# 有的还会预分配完整大小，只看 size/mtime 会误判为稳定。
# 不用 inotify：群晖等 NAS 上的 CIFS/NFS 挂载通知不可靠，漏事件就会提前算 SHA1。
_SIDECAR_SUFFIXES = (
    ".aria2", ".!qb", ".!qB", ".qbnewb", ".part", ".tmp",
    ".crdownload", ".downloading",
)


def still_writing(path: str) -> bool:
    """成品旁边还有下载器的控制文件时，文件还没写完，不能当稳定、不能算 SHA1。"""
    for sfx in _SIDECAR_SUFFIXES:
        if os.path.lexists(path + sfx):
            return True
    return False


def size_mtime_changed(path: str, wait: float = 0.15) -> bool:
    """短间隔再看一次 size/mtime：还在涨就先别算 SHA1（预分配写完之前也会变）。"""
    try:
        a = os.stat(path)
        time.sleep(wait)
        b = os.stat(path)
    except OSError:
        return True
    return a.st_size != b.st_size or a.st_mtime != b.st_mtime

TERMINAL = {"已上传", "错误"}
# 扫描写库每攒够这么多条提交一次（见 scan_once 末尾）
FLUSH_EVERY = 500
# 每轮「发现新文件 / 文件已稳定」逐条打 INFO 的上限，超出的降为 DEBUG，末尾打一行汇总。
# 首次扫一个 2 万集的片库原来会刷出 4 万行 INFO，把真正的问题淹掉。
LOG_EACH_MAX = 20


def file_ino(st: os.stat_result) -> str | None:
    """设备:inode，用来认出「同一个文件换了路径」（见 Store.find_sha1）。拿不到就 None。"""
    return f"{st.st_dev}:{st.st_ino}" if getattr(st, "st_ino", 0) else None


def scan_once(store, cfg: dict) -> int:
    """扫一遍监控目录，返回本轮新入队/推进的文件数。

    - 同 path+size+mtime 已存在 → 不重建记录（= 上游 check_duplicate）
    - 新文件 → 入队（stable_count=0），之后每轮 +1，达到 stable_rounds 才算稳定
    - 文件被重写（size/mtime 变）→ 视为新文件，重建记录（旧的 sha1/断点作废）
    """
    src = cfg["paths"]["source"]
    if not os.path.isdir(src):
        logger.warning("监控目录不存在：%s", src)
        return 0
    ignore = {s.lower() for s in cfg["scan"]["ignore_suffixes"]}
    include = {str(s).lower() for s in (cfg["scan"].get("include_suffixes") or [])}
    excludes = [str(d).strip("/\\").replace("\\", "/")
                for d in (cfg["scan"].get("exclude_dirs") or []) if str(d).strip()]
    stable_rounds = int(cfg["scan"]["stable_rounds"])
    min_bytes = float(cfg["scan"]["min_size_mb"]) * 1024 * 1024
    touched = 0

    def excluded(rel: str) -> bool:
        return any(rel == e or rel.startswith(e + "/") for e in excludes)

    # 按目录批量查库 + 整轮一个事务写回（见 Store.scan_lookup / apply_scan）：
    # 原来每个文件一次 SELECT、每次推进一次 commit。判断逻辑逐条不变。
    new_files: list[tuple] = []
    bumps: list[tuple[str, int]] = []
    ino_fill: list[tuple] = []       # (path, size, mtime, ino)：给升级前入库、还没有 inode 的记录补上
    n_new = n_stable = 0

    for root, dirs, names in os.walk(src):
        rel_root = os.path.relpath(root, src).replace("\\", "/")
        rel_root = "" if rel_root == "." else rel_root
        keep = []
        for d in dirs:
            if d.startswith(".") or d == "@eaDir":
                continue
            rel = f"{rel_root}/{d}".lstrip("/")
            if excluded(rel):
                logger.debug("按 exclude_dirs 跳过目录：%s", rel)
                continue
            keep.append(d)
        dirs[:] = keep
        cands: list[tuple[str, os.stat_result]] = []
        for name in names:
            if name.startswith("."):
                continue
            ext = os.path.splitext(name)[1].lower()
            if ext in ignore or (include and ext not in include):
                continue
            full = os.path.join(root, name)
            try:
                st = os.stat(full)
            except OSError:
                continue
            if st.st_size < min_bytes:
                continue
            cands.append((full, st))
        if not cands:
            continue
        known = store.scan_lookup([c[0] for c in cands])
        for full, st in cands:
            rec = known.get(full)
            if rec is None or rec[0] != st.st_size or rec[1] != st.st_mtime:
                # 新文件，或同路径内容被改写（size/mtime 变了）→ 重建记录
                new_files.append((full, st.st_size, st.st_mtime, file_ino(st)))
                n_new += 1
                logger.log(logging.INFO if n_new <= LOG_EACH_MAX else logging.DEBUG,
                           "发现新文件：%s (%.1f MB)", full, st.st_size / 1024 / 1024)
                touched += 1
                continue

            # 成品旁边还有 .aria2 / .!qB 等控制文件：先记下，但不要推进稳定计数。
            # 预分配的完整大小看起来「已经稳定」，sidecar 才是还在写的信号。
            if still_writing(full):
                logger.debug("还在下载（旁边有控制文件），暂不推进稳定计数：%s",
                             os.path.basename(full))
                continue

            status, stable_count = rec[2], rec[3]
            if rec[4] is None:
                ino = file_ino(st)
                if ino:
                    ino_fill.append((full, st.st_size, st.st_mtime, ino))
            if status in TERMINAL:
                continue
            if stable_count < stable_rounds:
                new_count = stable_count + 1
                bumps.append((full, new_count))
                if new_count >= stable_rounds:
                    n_stable += 1
                    logger.log(logging.INFO if n_stable <= LOG_EACH_MAX else logging.DEBUG,
                               "文件已稳定：%s", os.path.basename(full))
                touched += 1
        if len(new_files) + len(bumps) + len(ino_fill) >= FLUSH_EVERY:
            # 分批提交：首轮扫一个大片库时别把几万条写入攒在内存里一次性写
            # （实测 2 万条一次写完，进程常驻内存会多留十几 MB 回不去）
            store.apply_scan(new_files, bumps, ino_fill)
            new_files, bumps, ino_fill = [], [], []

    store.apply_scan(new_files, bumps, ino_fill)
    if n_new > LOG_EACH_MAX or n_stable > LOG_EACH_MAX:
        logger.info("本轮扫描：发现新文件 %d 个、文件已稳定 %d 个（逐条只列前 %d 个，其余见 DEBUG 日志）",
                    n_new, n_stable, LOG_EACH_MAX)
    return touched
