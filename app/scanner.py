"""目录扫描：稳定判定 + 去重入队（不重复读盘、不重复上传）。"""
from __future__ import annotations

import logging
import os
import time

logger = logging.getLogger("upilot.scan")

TERMINAL = {"已上传", "错误"}


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
    stable_rounds = int(cfg["scan"]["stable_rounds"])
    min_bytes = float(cfg["scan"]["min_size_mb"]) * 1024 * 1024
    touched = 0

    for root, dirs, names in os.walk(src):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d != "@eaDir"]
        for name in names:
            if name.startswith(".") or os.path.splitext(name)[1].lower() in ignore:
                continue
            full = os.path.join(root, name)
            try:
                st = os.stat(full)
            except OSError:
                continue
            if st.st_size < min_bytes:
                continue

            rec = store.seen(full, st.st_size, st.st_mtime)
            if rec is None:
                store.add(full, st.st_size, st.st_mtime)
                logger.info("发现新文件：%s (%.1f MB)", full, st.st_size / 1024 / 1024)
                touched += 1
                continue

            if rec["status"] in TERMINAL:
                continue
            if rec["stable_count"] < stable_rounds:
                new_count = rec["stable_count"] + 1
                store.set(full, stable_count=new_count)
                if new_count >= stable_rounds:
                    logger.info("文件已稳定：%s", os.path.basename(full))
                touched += 1

    return touched
