"""配置加载：/config/config.yaml 为主，环境变量可覆盖 cookie。"""
from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

import json

import yaml

CONFIG_PATH = Path(os.environ.get("UPILOT_CONFIG", "/config/config.yaml"))
EXAMPLE_PATH = Path("/app/config/config.example.yaml")

# 115 登录槽位（app 值取自 p115client.const.APP_TO_SSOENT）；中文标签对齐 115 官方客户端。
# 用途：扫码/上传都用它当身份，挑一个自己平时不用的端，就不会把手机/网页挤下线。
# 只列「实测能拿到二维码」的槽位（115 当前对另外 6 个槽位只回一张 HTML 跳转页，已从下拉里去掉）。
# 排序即推荐度：iPad 端排第一，多数人平时不在 iPad 上用 115，扫码不会挤掉手机/网页版。
APP_OPTIONS: list[dict] = [
    {"value": "115ipad", "label": "115(iPad端·旧版)", "qr": True},
    {"value": "ipad", "label": "115(iPad端)", "qr": True},
    {"value": "115android", "label": "115(Android端·旧版)", "qr": True},
    {"value": "115ios", "label": "115(iOS端·旧版)", "qr": True},
    {"value": "tv", "label": "115网盘(Android电视端)", "qr": True},
    {"value": "apple_tv", "label": "115网盘(Apple电视端)", "qr": True},
    {"value": "wechatmini", "label": "115生活(微信小程序)", "qr": True},
    {"value": "alipaymini", "label": "115生活(支付宝小程序)", "qr": True},
    {"value": "harmony", "label": "115(鸿蒙端)", "qr": True},
    {"value": "web", "label": "网页版", "qr": True},
    {"value": "windows", "label": "Windows 电脑端", "qr": True},
    {"value": "mac", "label": "macOS 电脑端", "qr": True},
    {"value": "linux", "label": "Linux 电脑端", "qr": True},
]
APP_VALUES = [x["value"] for x in APP_OPTIONS]
QR_SUPPORTED = list(APP_OPTIONS)

# 115 已改端点、取不到二维码的槽位：不再出现在下拉里；老 config.yaml 里若还写着它们，
# 扫码时给一句可读提示（而不是丢一个 500 栈）。
REMOVED_APPS = {
    "qandroid": "115管理(Android端)", "qios": "115管理(iOS端)", "qipad": "115管理(iPad端)",
    "android": "115(Android端)", "ios": "115(iOS端)", "bandroid": "115生活(Android端)",
}
QR_UNSUPPORTED = set(REMOVED_APPS)


def app_label(value: str) -> str:
    for item in APP_OPTIONS:
        if item["value"] == value:
            return item["label"]
    return REMOVED_APPS.get(value, value)


DEFAULTS: dict = {
    "p115": {"cookie": "", "app": "115ipad", "cookie_from": ""},
    "paths": {"source": "/media/source", "target_root": "/UPILOT-mini", "map": []},
    "scan": {
        "interval_seconds": 30,
        "stable_rounds": 2,
        "min_size_mb": 0,
        # 只处理这些扩展名；留空列表 [] 表示不过滤（全都要）
        "include_suffixes": [
            ".mkv", ".mp4", ".ts", ".m2ts", ".mts", ".avi", ".mov", ".wmv",
            ".flv", ".rmvb", ".iso", ".vob", ".mpg", ".mpeg", ".divx", ".webm",
        ],
        # 相对监控根目录的路径片段，命中则整棵子树跳过（例如 ["@eaDir", "extras"]）
        "exclude_dirs": [],
        "ignore_suffixes": [".!qb", ".part", ".tmp", ".downloading", ".crdownload", ".aria2"],
    },
    "upload": {
        "small_file_mb": 10,
        "partsize_mb": 10,
        "concurrency": 2,
        "init_min_interval": 0.5,
        "transient_retries": 2,
        "conflict_strategy": "overwrite",
    },
    "retry": {"interval_seconds": 60, "max_count": 5, "max_hours": 12},
    "state": {"db": "/config/state.db"},
    "http": {
        "enabled": True,
        "host": "0.0.0.0",
        "port": 9844,
        # 反代/暴露到内网时打开：页面与 /api/* 需要 HTTP Basic 或 ?token=…
        # /health 始终放行（容器 HEALTHCHECK 在容器内直连）
        "auth": {"enabled": False, "mode": "basic", "username": "admin",
                 "password": "", "session_hours": 72},
    },
    "log_level": "INFO",
}


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def ensure_config() -> Path:
    """首次启动：把示例配置复制到 /config/config.yaml。"""
    if not CONFIG_PATH.exists():
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        if EXAMPLE_PATH.exists():
            shutil.copyfile(EXAMPLE_PATH, CONFIG_PATH)
        else:
            CONFIG_PATH.write_text(yaml.safe_dump(DEFAULTS, allow_unicode=True), encoding="utf-8")
    return CONFIG_PATH


def load() -> dict:
    ensure_config()
    with open(CONFIG_PATH, encoding="utf-8") as f:
        user = yaml.safe_load(f) or {}
    cfg = _merge(DEFAULTS, user)

    env_cookie = os.environ.get("P115_COOKIE", "").strip()
    if env_cookie:
        cfg["p115"]["cookie"] = env_cookie
    env_src = os.environ.get("UPILOT_SOURCE", "").strip()
    if env_src:
        cfg["paths"]["source"] = env_src
    env_target = os.environ.get("UPILOT_TARGET_ROOT", "").strip()
    if env_target:
        cfg["paths"]["target_root"] = env_target
    return cfg


def load_cookie_from(path: str) -> str:
    """从别的程序的配置文件里抠出 115 cookie（例如 symedia 的 config.yaml）。

    支持 yaml / json；递归找「看起来是 115 cookie」的内容：字符串里同时含 UID= 和 CID=
    （或含 SEID=），或者是形如 {UID:…, CID:…, SEID:…} 的字典 —— 后者会拼成 cookie 串。
    找不到返回空串（调用方据此跳过，不会把配置写坏）。
    """
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    data = None
    try:
        data = yaml.safe_load(text)
    except Exception:  # noqa: BLE001
        try:
            data = json.loads(text)
        except Exception:  # noqa: BLE001
            data = None
    hits: list[str] = []

    def walk(o):
        if isinstance(o, dict):
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
        elif isinstance(o, str) and "UID=" in o and ("CID=" in o or "SEID=" in o):
            hits.append(o.strip())

    def walk_dict(o):
        if isinstance(o, dict):
            keys = {str(k) for k in o}
            if {"UID", "CID"} <= keys or "SEID" in keys:
                parts = [f"{k}={o[k]}" for k in ("UID", "CID", "SEID", "KID") if k in o]
                if parts:
                    hits.append("; ".join(parts))
                    return
            for v in o.values():
                walk_dict(v)
        elif isinstance(o, list):
            for v in o:
                walk_dict(v)

    if data is not None:
        walk(data)
        if not hits:
            walk_dict(data)
    if not hits:      # 兜底：直接在文本里找（配置文件语法怪时也能救）
        m = re.search(r"UID=[^\s\"']{2,}?(?:;\s*(?:CID|KID|SEID)=[^\"']+)+", text)
        if m:
            hits.append(m.group(0).strip())
    return hits[0] if hits else ""


def save_cookie(cookie: str) -> None:
    """扫码登录后写回配置（仅替换 p115.cookie 一行，保留注释）。"""
    ensure_config()
    text = CONFIG_PATH.read_text(encoding="utf-8")
    lines = text.splitlines()
    out, hit = [], False
    for line in lines:
        if line.strip().startswith("cookie:"):
            out.append(f'  cookie: "{cookie}"')
            hit = True
        else:
            out.append(line)
    if not hit:
        out.insert(1, f'  cookie: "{cookie}"')
    CONFIG_PATH.write_text("\n".join(out) + "\n", encoding="utf-8")


# ---------------- 界面可编辑配置（设置页据此渲染，后端据此校验） ----------------

MASK = "••••••••"

FIELD_GROUPS: list[dict] = [
    {"title": "目录", "fields": [
        {"key": "paths.source", "label": "监控目录（容器内路径）", "type": "text",
         "hint": "只读挂载进容器的目录，改这里要跟 compose 的冒号左边保持一致"},
        {"key": "paths.target_root", "label": "115 目标根目录", "type": "text",
         "hint": "没被下面映射命中的文件，云端结构 = 它 + 本地相对路径"},
        {"key": "paths.map", "label": "本机 → 115 目录映射", "type": "map", "rows": 9,
         "hint": "每行一条：本机相对路径 => 115 目录。例 TVs => /TVs｜Movies/Others => /Movies/Others｜Anime => /OtherAnime（可改名、大小写照写）。命中时取最长前缀，其下结构原样保留"},
    ]},
    {"title": "115 账号", "fields": [
        {"key": "p115.cookie", "label": "115 Cookie", "type": "secret",
         "hint": "留空 = 不改；粘贴完整 cookie 保存即生效（会自动重建 115 客户端）"},
        {"key": "p115.cookie_from", "label": "从别的程序同步 cookie（可选）", "type": "text",
         "hint": "填容器内可见的配置文件路径，例如挂进来的 /symedia-config/config.yaml；"
                 "程序每分钟读一次，发现里面的 115 cookie 变了就自动同步过来并重建客户端"},
        {"key": "p115.app", "label": "115 登录槽位（扫码与上传都用它）", "type": "select",
         "options": APP_OPTIONS,
         "hint": "选一个平时不用的端，扫码时就不会把手机/网页版挤下线；改完保存，扫码卡片会同步"},
    ]},
    {"title": "扫描", "fields": [
        {"key": "scan.interval_seconds", "label": "扫描间隔（秒）", "type": "int", "min": 2, "max": 86400},
        {"key": "scan.stable_rounds", "label": "稳定判定轮数", "type": "int", "min": 1, "max": 20,
         "hint": "连续几轮大小与修改时间不变才算“传完了”"},
        {"key": "scan.min_size_mb", "label": "最小文件体积（MB）", "type": "float", "min": 0},
        {"key": "scan.include_suffixes", "label": "只收这些扩展名", "type": "list",
         "hint": "逗号分隔；留空 = 全都要"},
        {"key": "scan.exclude_dirs", "label": "跳过这些目录", "type": "list",
         "hint": "相对监控根目录的路径片段，命中整棵子树跳过"},
        {"key": "scan.ignore_suffixes", "label": "忽略这些扩展名", "type": "list",
         "hint": "下载中的临时后缀，逗号分隔"},
    ]},
    {"title": "上传", "fields": [
        {"key": "upload.concurrency", "label": "并发文件数", "type": "int", "min": 1, "max": 8},
        {"key": "upload.small_file_mb", "label": "小文件阈值（MB）", "type": "float", "min": 0,
         "hint": "小于它：秒传未命中就直接分块上传，不排队重试"},
        {"key": "upload.partsize_mb", "label": "分块大小（MB）", "type": "int", "min": 1, "max": 512},
        {"key": "upload.init_min_interval", "label": "秒传请求最小间隔（秒）", "type": "float", "min": 0},
        {"key": "upload.conflict_strategy", "label": "同名冲突处理", "type": "select",
         "options": ["overwrite", "rename", "skip"]},
        {"key": "upload.transient_retries", "label": "分块瞬时重试次数", "type": "int", "min": 0, "max": 10},
    ]},
    {"title": "秒传重试", "fields": [
        {"key": "retry.interval_seconds", "label": "重试间隔（秒）", "type": "int", "min": 1, "max": 86400},
        {"key": "retry.max_count", "label": "最多重试次数", "type": "int", "min": 0, "max": 100},
        {"key": "retry.max_hours", "label": "重试最长等待（小时）", "type": "float", "min": 0},
    ]},
    {"title": "界面与鉴权", "fields": [
        {"key": "http.enabled", "label": "启用 Web 界面", "type": "bool"},
        {"key": "http.host", "label": "监听地址", "type": "text", "restart": True},
        {"key": "http.port", "label": "监听端口", "type": "int", "min": 1, "max": 65535, "restart": True},
        {"key": "http.public_url", "label": "对外地址（可选）", "type": "text",
         "hint": "登录页底部显示用，例如 https://up.example.com"},
        {"key": "http.auth.enabled", "label": "开启登录鉴权", "type": "bool"},
        {"key": "http.auth.mode", "label": "鉴权方式", "type": "select", "options": ["form", "basic"],
         "hint": "form = 站内登录页；basic = 浏览器原生弹框"},
        {"key": "http.auth.username", "label": "登录用户名", "type": "text"},
        {"key": "http.auth.password", "label": "登录密码", "type": "secret",
         "hint": "留空 = 不改；改了等于换签名密钥，已登录的浏览器会全部失效"},
        {"key": "http.auth.session_hours", "label": "登录状态保持（小时）", "type": "int", "min": 1, "max": 8760},
    ]},
    {"title": "日志", "fields": [
        {"key": "log_level", "label": "日志级别", "type": "select",
         "options": ["DEBUG", "INFO", "WARNING", "ERROR"], "restart": True},
    ]},
]

RELOAD_KEYS = {"p115.cookie", "p115.app"}          # 改了要重建 115 客户端
RESTART_KEYS = {"http.host", "http.port", "log_level"}   # 改了要重建容器


def _dig(data: dict, key: str, default=None):
    cur = data
    for part in key.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def _plant(data: dict, key: str, value) -> None:
    parts = key.split(".")
    cur = data
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[part] = nxt
        cur = nxt
    cur[parts[-1]] = value


def find_field(key: str) -> dict | None:
    for group in FIELD_GROUPS:
        for field in group["fields"]:
            if field["key"] == key:
                return field
    return None


def coerce(field: dict, raw) -> object:
    """按字段类型把界面传来的值转成配置里的真实类型，顺带做范围校验。"""
    kind = field["type"]
    label = field.get("label") or field["key"]
    if kind == "bool":
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() in ("1", "true", "on", "yes", "是")
    if kind in ("int", "float"):
        text = str(raw).strip()
        if not text:
            raise ValueError(f"{label}：请填数字")
        try:
            value = float(text) if kind == "float" else int(float(text))
        except ValueError as exc:
            raise ValueError(f"{label}：请填数字") from exc
        lo, hi = field.get("min"), field.get("max")
        if lo is not None and value < lo:
            raise ValueError(f"{label}：最小 {lo}")
        if hi is not None and value > hi:
            raise ValueError(f"{label}：最大 {hi}")
        return value
    if kind == "list":
        if isinstance(raw, list):
            items = [str(x).strip() for x in raw]
        else:
            items = [x.strip() for x in re.split(r"[,，;；\s]+", str(raw))]
        return [x for x in items if x]
    if kind == "map":
        lines = raw if isinstance(raw, list) else re.split(r"[\r\n]+", str(raw or ""))
        out: list[dict] = []
        for line in lines:
            text = str(line).strip()
            if not text or text.startswith("#"):
                continue
            sep = "=>" if "=>" in text else ("->" if "->" in text else None)
            if sep is None:
                raise ValueError(f"{label}：每行要写成「本机相对路径 => 115 目录」，收到：{text}")
            src, _, dst = text.partition(sep)
            src = src.strip().strip("/").strip("\\")
            dst = dst.strip()
            if not dst:
                raise ValueError(f"{label}：缺少 115 目录 —— {text}")
            if not dst.startswith("/"):
                dst = "/" + dst
            out.append({"from": src, "to": dst})
        return out
    if kind == "select":
        value = str(raw).strip()
        options = field.get("options") or []
        allowed = [o["value"] if isinstance(o, dict) else str(o) for o in options]
        if allowed and value not in allowed:
            raise ValueError(f"{label}：只能是 {'/'.join(allowed)}")
        return value
    return str(raw).strip()


def apply_values(cfg: dict, values: dict, clear_secrets: list | None = None) -> dict:
    """把界面提交的值写进内存 cfg。密钥类字段留空表示保持原值。

    返回 {"changed": [...], "reload": bool, "restart": bool}
    """
    clear = {str(k) for k in (clear_secrets or [])}
    changed: list[str] = []
    for key, raw in (values or {}).items():
        field = find_field(key)
        if field is None:
            continue                                    # 白名单之外一律忽略
        old = _dig(cfg, key)
        if field["type"] == "secret":
            text = str(raw or "").strip()
            if key in clear:
                new: object = ""
            elif text:
                new = text
            else:
                continue                                # 留空 = 不改
        else:
            new = coerce(field, raw)
        if new != old:
            _plant(cfg, key, new)
            changed.append(key)
    return {
        "changed": changed,
        "reload": any(k in RELOAD_KEYS for k in changed),
        "restart": any(k in RESTART_KEYS for k in changed),
    }


def form_payload(cfg: dict) -> dict:
    """给设置页用的表单结构：密钥类字段只回脱敏状态，不回落明文。"""
    groups = []
    for group in FIELD_GROUPS:
        fields = []
        for field in group["fields"]:
            item = dict(field)
            value = _dig(cfg, field["key"], "")
            if field["type"] == "secret":
                text = str(value or "")
                # 只回答“设没设、多长”，不回任何明文片段
                item.update({"value": "", "set": bool(text), "length": len(text), "mask": MASK if text else ""})
            elif field["type"] == "bool":
                item["value"] = bool(value)
            elif field["type"] == "list":
                item["value"] = ", ".join(str(x) for x in (value or []))
            elif field["type"] == "map":
                item["value"] = "\n".join(
                    f'{str(m.get("from") or "")} => {str(m.get("to") or "")}'
                    for m in (value or []) if isinstance(m, dict))
            else:
                item["value"] = value
            fields.append(item)
        groups.append({"title": group["title"], "fields": fields})
    return {"groups": groups, "path": str(CONFIG_PATH)}


def save(cfg: dict) -> Path:
    """把当前内存配置落盘（原子写 + 保留一份 .bak）。"""
    ensure_config()
    path = CONFIG_PATH
    body = ("# 本文件由界面保存生成（字段说明见 config.example.yaml）\n"
            + yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False, default_flow_style=False))
    tmp = path.parent / (path.name + ".tmp")
    tmp.write_text(body, encoding="utf-8")
    if path.exists():
        shutil.copyfile(path, path.parent / (path.name + ".bak"))
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path
