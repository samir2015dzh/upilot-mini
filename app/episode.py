"""从文件名里认「第几季第几集」，给洗版（同集只留最新一份）用。

体量刻意很小、不加任何依赖（guessit / PTN 动辄几千行、还带一堆我们用不上的字段）。
正则从两个现成项目里挑出和「季集号」有关的几条，按本项目的需求改写：

  - PTN（parse-torrent-name）的 season / episode 规则：
    https://github.com/platelminto/parse-torrent-name/blob/4e793868512074e49c78e5b714d072f748ab9dc1/PTN/patterns.py
    （`S01E02`、`S01E02-03` 多集、`1x02`、`Season 1 Episode 2`、`Ep02`、`E02`）
  - MoviePilot 的 MetaVideo（中文 `第N集/话`、`第N季`、动漫 ` - 05 `、`EP05`）：
    https://github.com/jxxghp/MoviePilot/blob/af963b47b350f07b7ee407ab54a5eb9b4091ab35/app/domain/meta/metavideo.py

识别结果只有三样：剧名（归一化）、季（可能没有，S00 是特别篇）、集（可能是多集区间）。
**扩展名不参与**：同一集的 mkv / mp4 / ts 就是同一集。
PROPER / REPACK / RERIP / v2 这类修订标记不进「同一集」键，只在时间并列时用来判断谁更新。
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass

VIDEO_SUFFIXES = {".mkv", ".mp4", ".ts", ".m2ts", ".mts", ".avi", ".mov", ".wmv",
                  ".flv", ".rmvb", ".iso", ".vob", ".mpg", ".mpeg", ".divx", ".webm"}

# 剧名比较方式（Uploader 启动时按 upload.cleanup_title_match 设置）：
#   auto   —— 默认。季集号相同 + 剧名「兼容」（有共同词，或一方没有剧名）才算同一集。
#             同一个文件夹里放着好几部剧时，不会把 A 剧的 S01E01 当成 B 剧的 S01E01 删掉。
#   ignore —— 只看季集号（一个文件夹只放一部剧、且新旧版本片名差异很大时用）。
#   same   —— 剧名归一化后必须完全一样。
TITLE_MATCH_MODES = ("auto", "ignore", "same")
_title_match = "auto"


def set_title_match(mode: str) -> str:
    global _title_match
    m = str(mode or "").strip().lower()
    _title_match = m if m in TITLE_MATCH_MODES else "auto"
    return _title_match


def get_title_match() -> str:
    return _title_match


_CN_NUM = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9}


def cn_to_int(s: str) -> int | None:
    """中文数字转整数：十二→12、二十→20、一百零五→105、一千零一→1001。纯阿拉伯数字原样返回。"""
    s = (s or "").strip()
    if s.isdigit():
        return int(s)
    if not s or any(c not in _CN_NUM and c not in "十百千" for c in s):
        return None
    total, cur = 0, 0
    for c in s:
        if c == "千":
            total += (cur or 1) * 1000
            cur = 0
        elif c == "百":
            total += (cur or 1) * 100
            cur = 0
        elif c == "十":
            total += (cur or 1) * 10
            cur = 0
        else:
            cur = _CN_NUM[c]
    return total + cur


_NUM = r"(\d{1,4}|[零〇一二两三四五六七八九十百千]{1,7})"
# 1) S01E02 / S01E02-E03 / S01E02E03 / S01E02-03（PTN 的 season+episode；多集区间）
_SXXEYY = re.compile(
    r"(?<![a-z0-9])s(\d{1,2})[\s.\-_]*e(\d{1,4})"
    r"(?:\s*(?:-|~|–)\s*e?(\d{1,4})(?![\dpik])|e(\d{1,4})(?![\dpik]))?")
# 2) Season 1 Episode 2
_SEASON_EPISODE = re.compile(r"(?<![a-z0-9])season\s*(\d{1,2})\W{0,3}episode\s*(\d{1,4})")
# 3) 1x02（排除 1920x1080 / x264）
_NXNN = re.compile(r"(?<![a-z0-9])(\d{1,2})x(\d{2,3})(?![a-z0-9])")
# 4) 第12集 / 第十二话 / 第3期（可前置 第2季）
_CN_SEASON = re.compile(r"第\s*" + _NUM + r"\s*季")
_CN_EP = re.compile(r"第\s*" + _NUM + r"\s*[集话話期]")
# 5) EP05 / E05 / E01-E02 / EP01-EP02（单独出现；季号从别处找）
_EP_ONLY = re.compile(
    r"(?<![a-z0-9])e(?:p|pisode)?\s*(\d{1,4})"
    r"(?:\s*(?:-|~|–)\s*e(?:p|pisode)?\s*(\d{1,4})(?![\dpik]))?")
# 5b) Show SP01 / Show.Special.01 → 特别篇，记成第 0 季
_SPECIAL = re.compile(r"(?<![a-z0-9])(?:sp|special)\s*(\d{1,3})(?![a-z0-9])")
_SEASON_ONLY = re.compile(r"(?<![a-z0-9])(?:s|season\s*)(\d{1,2})(?![a-z0-9])")
# 6) 动漫字幕组命名：「Show - 05」「Show - 05v2」（只认 2~3 位，避开 4 位年份）
_ANIME = re.compile(r"\s-\s(\d{2,3})(?:v\d)?(?=\s|$)")

_YEAR = re.compile(r"(?<!\d)(?:19|20)\d{2}(?!\d)")
_STOP = {"the", "a", "an", "of", "and", "season", "complete"}
_CJK = re.compile(r"[\u3040-\u30ff\u3400-\u9fff]+")
_LATIN = re.compile(r"[a-z0-9]+")
# 修订标记（不进同一集键）。借鉴 PTN 的 proper/repack 规则：
# https://github.com/platelminto/parse-torrent-name/blob/4e793868512074e49c78e5b714d072f748ab9dc1/PTN/patterns.py
# v2 / 05v2 也算修订；PROPER/REPACK/RERIP/REAL 至少当作 v2。
_VER_TAG = re.compile(r"(?<![a-z0-9])(proper|repack|rerip|real)(?![a-z0-9])")
_VER_NUM = re.compile(r"(?<![a-z])v(\d{1,2})(?![a-z0-9])")


def version_score(name: str) -> int:
    """文件名里的修订标记：没有=0，v2=2，v3=3；PROPER/REPACK/RERIP/REAL 至少为 2。

    洗版判新旧时，本地下载时间仍然优先；只有时间并列（或退回云端时间也并列）才用它。
    """
    stem, _ext = os.path.splitext(name or "")
    text = _normalize(stem)
    n = 0
    m = _VER_NUM.search(text)
    if m:
        try:
            n = int(m.group(1))
        except (TypeError, ValueError):
            n = 0
    if _VER_TAG.search(text):
        n = max(n, 2)
    return n


@dataclass(frozen=True)
class Episode:
    title: str                 # 归一化剧名（可能为空）
    season: int | None
    episodes: tuple[int, ...]  # 一般只有 1 个；S01E01-E02 是 (1, 2)

    @property
    def key(self) -> str:
        """季集键（**不含剧名、不含扩展名**）：s01e001 / s01e001-e002 / e005（没有季号）。"""
        eps = "-".join(f"e{e:03d}" for e in self.episodes)
        return f"s{self.season:02d}{eps}" if self.season is not None else eps

    @property
    def tokens(self) -> frozenset[str]:
        t = _YEAR.sub(" ", self.title)
        out = {w for w in _LATIN.findall(t) if w not in _STOP}
        out |= set(_CJK.findall(t))
        return frozenset(out)


def _normalize(stem: str) -> str:
    text = re.sub(r"\[[^\]]*\]|【[^】]*】", " ", stem)   # 剥掉 [tmdbid=…] / [2160p …] / [字幕组]
    text = re.sub(r"[._]+", " ", text)
    return re.sub(r"\s+", " ", text).strip().lower()


def _clean_title(text: str) -> str:
    t = re.sub(r"\([^)]*\)|（[^）]*）", " ", text)             # (2026) 之类
    t = _CN_SEASON.sub(" ", t)
    t = re.sub(r"(?<![a-z0-9])(?:s\d{1,2}|season\s*\d{1,2})(?![a-z0-9])", " ", t)
    t = _VER_TAG.sub(" ", t)
    t = _VER_NUM.sub(" ", t)
    t = re.sub(r"[\s\-–:：,，]+$", "", t)
    return re.sub(r"\s+", " ", t).strip(" -–:：")


def parse_episode(name: str) -> Episode | None:
    """认不出季集信息、或不是视频文件就返回 None（不参与洗版清理）。

    >>> parse_episode("Show.Name.S01E02.1080p.WEB-DL.mkv").key
    's01e002'
    >>> parse_episode("Show Name 1x02.mp4").key
    's01e002'
    >>> parse_episode("[字幕组] 某剧 - 05 [1080p].mp4").key
    'e005'
    >>> parse_episode("某剧 第二季 第十二集.mkv").key
    's02e012'
    """
    stem, ext = os.path.splitext(name or "")
    if ext.lower() not in VIDEO_SUFFIXES:
        return None
    text = _normalize(stem)
    season: int | None = None
    eps: tuple[int, ...] = ()
    start = -1

    m = _SXXEYY.search(text)
    if m:
        season = int(m.group(1))
        a = int(m.group(2))
        b = m.group(3) or m.group(4)
        eps = (a,) if not b or not (a < int(b) < a + 100) else (a, int(b))
        start = m.start()
    if not eps:
        m = _SEASON_EPISODE.search(text)
        if m:
            season, eps, start = int(m.group(1)), (int(m.group(2)),), m.start()
    if not eps:
        m = _NXNN.search(text)
        if m:
            season, eps, start = int(m.group(1)), (int(m.group(2)),), m.start()
    if not eps:
        m = _CN_EP.search(text)
        n = cn_to_int(m.group(1)) if m else None
        if m and n is not None:
            eps, start = (n,), m.start()
            ms = _CN_SEASON.search(text)
            if ms and cn_to_int(ms.group(1)) is not None:
                season = cn_to_int(ms.group(1))
                start = min(start, ms.start())
    if not eps:
        m = _EP_ONLY.search(text)
        if m:
            a = int(m.group(1))
            b = m.group(2)
            eps = (a,) if not b or not (a < int(b) < a + 100) else (a, int(b))
            start = m.start()
            ms = _SEASON_ONLY.search(text)
            if ms:
                season = int(ms.group(1))
                start = min(start, ms.start())
    if not eps:
        m = _ANIME.search(text)
        if m:
            eps, start = (int(m.group(1)),), m.start()
    if not eps:
        m = _SPECIAL.search(text)
        if m and int(m.group(1)) > 0:
            season, eps, start = 0, (int(m.group(1)),), m.start()
    if not eps or any(e <= 0 for e in eps):
        return None
    return Episode(_clean_title(text[:start]), season, eps)


def titles_compatible(a: Episode, b: Episode, mode: str | None = None) -> bool:
    mode = mode or _title_match
    if mode == "ignore":
        return True
    ta, tb = a.tokens, b.tokens
    if mode == "same":
        return ta == tb
    if not ta or not tb:
        return True                       # 一方没带剧名（如 S01E01.mkv）：无法证伪，按同剧
    if ta & tb:
        return True
    cja = [t for t in ta if _CJK.fullmatch(t)]
    cjb = [t for t in tb if _CJK.fullmatch(t)]
    return any(x in y or y in x for x in cja for y in cjb)


def episode_key(name: str) -> str | None:
    """文件名 → 「同一集」键（季集号，不含扩展名与剧名）。认不出返回 None。"""
    ep = parse_episode(name)
    return ep.key if ep else None


def _find(parent: list[int], i: int) -> int:
    while parent[i] != i:
        parent[i] = parent[parent[i]]
        i = parent[i]
    return i


def group_same_episode(items: list[tuple[object, str]]) -> list[list[object]]:
    """把 [(任意对象, 文件名)] 按「同一集」分组，只返回成员数 ≥ 2 的组。

    季集键相同的先聚在一起，再按剧名兼容性用并查集拆开（`auto` 下 A 剧与 B 剧即使
    季集号相同也不会混成一组）。
    """
    buckets: dict[str, list[tuple[object, Episode]]] = {}
    for obj, nm in items:
        ep = parse_episode(nm)
        if ep:
            buckets.setdefault(ep.key, []).append((obj, ep))
    out: list[list[object]] = []
    for members in buckets.values():
        if len(members) < 2:
            continue
        parent = list(range(len(members)))
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                if titles_compatible(members[i][1], members[j][1]):
                    parent[_find(parent, i)] = _find(parent, j)
        clusters: dict[int, list[object]] = {}
        for i, (obj, _) in enumerate(members):
            clusters.setdefault(_find(parent, i), []).append(obj)
        out.extend(c for c in clusters.values() if len(c) >= 2)
    return out
