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

# 115 客户端身份（app 值取自 p115client.const.APP_TO_SSOENT）；中文标签对齐 115 官方客户端。
# 用途：所有 115 请求（目录 / 秒传 / 上传）都用它当身份，挑一个自己平时不用的端。
APP_OPTIONS: list[dict] = [
    {"value": "115ipad", "label": "115(iPad端·旧版)"},
    {"value": "ipad", "label": "115(iPad端)"},
    {"value": "115android", "label": "115(Android端·旧版)"},
    {"value": "115ios", "label": "115(iOS端·旧版)"},
    {"value": "tv", "label": "115网盘(Android电视端)"},
    {"value": "apple_tv", "label": "115网盘(Apple电视端)"},
    {"value": "wechatmini", "label": "115生活(微信小程序)"},
    {"value": "alipaymini", "label": "115生活(支付宝小程序)"},
    {"value": "harmony", "label": "115(鸿蒙端)"},
    {"value": "web", "label": "网页版"},
    {"value": "windows", "label": "Windows 电脑端"},
    {"value": "mac", "label": "macOS 电脑端"},
    {"value": "linux", "label": "Linux 电脑端"},
]
APP_VALUES = [x["value"] for x in APP_OPTIONS]

# 已经是历史值的旧槽位名：只用于界面上把老配置显示成中文（不再作为可选项）。
REMOVED_APPS = {
    "qandroid": "115管理(Android端)", "qios": "115管理(iOS端)", "qipad": "115管理(iPad端)",
    "android": "115(Android端)", "ios": "115(iOS端)", "bandroid": "115生活(Android端)",
}

# 上传模式
# rapid_only：只用秒传 —— 未命中就排队等，隔一段时间再发一次校验请求，永不普通上传。
#             115 的秒传是全站 hash 索引，热门片源通常过一阵就有人传上云，届时直接命中、零上行。
# rapid_then_upload：秒传优先，重试耗尽后转普通上传（分块 + 断点续传）。
UPLOAD_MODES: list[dict] = [
    {"value": "rapid_only", "label": "只秒传：云端有就秒完成，没有就一直等（不费流量）"},
    {"value": "rapid_then_upload", "label": "先试秒传，等不到就真上传（会费流量）"},
]
UPLOAD_MODE_LABELS = {x["value"]: x["label"] for x in UPLOAD_MODES}


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
        # rapid_only = 只用秒传（云端有同 hash 才收，永不普通上传）
        # rapid_then_upload = 秒传优先，重试耗尽后转普通上传（断点续传）
        "mode": "rapid_only",
        "small_file_mb": 10,
        "partsize_mb": 10,
        "concurrency": 2,
        "init_min_interval": 0.5,
        "transient_retries": 2,
        "conflict_strategy": "overwrite",
        # 传完就删掉监控目录里的本地源文件（破坏性，默认关）
        "delete_after_upload": False,
    },
    "retry": {"interval_seconds": 60, "max_count": 5, "max_hours": 12,
              "long_interval_seconds": 900},
    "notify": {
        # 秒传命中 / 分块上传完成后，POST 一个事件给别的程序。默认按 CloudDrive2 的
        # file_notify 契约发，symedia 的「链接同步（Webhook 模式）」就认这个格式：
        #   POST {url}?{query}
        #   {"device_name":…,"event_category":"file","event_name":"notify",
        #    "event_time":…,"send_time":…,"data":[{"action":"create","is_dir":"false",
        #    "source_file":"<path_prefix + 115 路径>","destination_file":""}]}
        "enabled": False,
        "url": "",
        "query": "",
        # 115 网盘路径 → 对方视角：symedia 认 CD2 挂载路径（它的标准挂载就是这个），
        # 一般不用改。填好后 /TVs/x.mkv → /CloudNAS/CloudDrive/115open/TVs/x.mkv
        "path_prefix": "/CloudNAS/CloudDrive/115open",
        "auth": "",                  # 可选：Basic xxx / Bearer xxx
        "on": "rapid_hit",           # rapid_hit | uploaded | both
        "timeout": 20,               # symedia 收到后会先建 strm 再回执，给宽一点
        "retries": 2,
    },
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
    """把 cookie 写回配置（仅替换 p115.cookie 一行，保留注释）。"""
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
    {"title": "文件夹", "fields": [
        {"key": "paths.source", "label": "盯住哪个文件夹", "type": "text",
         "hint": "就是装的时候挂进来的那个目录。要改的话，得同时改 compose 文件里冒号左边那一段"},
        {"key": "paths.target_root", "label": "放到 115 的哪个目录下", "type": "text",
         "hint": "下面「文件夹对应关系」没提到的文件，就按原来的文件夹结构放进这里；目录不存在会自动建"},
        {"key": "paths.map", "label": "文件夹对应关系", "type": "map", "rows": 9,
         "hint": "一行一条，写法：本地文件夹 => 115 上的文件夹。左边写你硬盘上的相对位置，右边写 115 里的目录。"
                 "例：TVs => /TVs　｜　Anime => /OtherAnime（相当于改名）"},
    ]},
    {"title": "115 账号", "fields": [
        {"key": "p115.cookie", "label": "115 登录凭证", "type": "secret",
         "hint": "从浏览器或别的程序里复制过来，粘上保存就行；留空表示不修改。保存后立刻生效，不用重启"},
        {"key": "p115.cookie_from", "label": "自动跟着别的程序换凭证（可选）", "type": "text",
         "hint": "填一个配置文件路径，比如 /symedia-config/config.yaml。程序每分钟看一眼，"
                 "那边换了登录凭证，这边自动跟着换；不用就留空"},
        {"key": "p115.app", "label": "用哪个 115 客户端身份", "type": "select",
         "options": APP_OPTIONS,
         "hint": "随便挑一个自己平时不用的（比如 iPad 端）就行，只是换个身份，不影响功能"},
    ]},
    {"title": "扫描", "fields": [
        {"key": "scan.interval_seconds", "label": "多久看一眼文件夹（秒）", "type": "int",
         "min": 2, "max": 86400,
         "hint": "默认 30 秒。调小反应更快，稍微费一点性能"},
        {"key": "scan.stable_rounds", "label": "连续几次没变化才算传完", "type": "int", "min": 1, "max": 20,
         "hint": "正在下载的文件大小一直在变，等它不动了再动手，免得传半个文件。默认 2"},
        {"key": "scan.min_size_mb", "label": "小到这个程度就不管（MB）", "type": "float", "min": 0,
         "hint": "0 = 多小都处理"},
        {"key": "scan.include_suffixes", "label": "只处理这些类型", "type": "list",
         "hint": "用逗号分开，比如 .mkv, .mp4。留空 = 什么文件都要"},
        {"key": "scan.exclude_dirs", "label": "跳过这些文件夹", "type": "list",
         "hint": "写文件夹名，命中的整个跳过。比如 Sample, @eaDir"},
        {"key": "scan.ignore_suffixes", "label": "这些是没下完的文件", "type": "list",
         "hint": "用逗号分开，一般不用改"},
    ]},
    {"title": "上传", "fields": [
        {"key": "upload.mode", "label": "上传方式", "type": "select", "options": UPLOAD_MODES,
         "hint": "「只秒传」最省流量：云端已经有同样的文件就瞬间完成，一个字节都不用传；"
                 "没有就排队等，隔一会儿再问一次，绝不真上传"},
        {"key": "upload.concurrency", "label": "同时处理几个文件", "type": "int", "min": 1, "max": 8,
         "hint": "默认 2。网络吃紧就改成 1"},
        {"key": "upload.small_file_mb", "label": "小文件就别等秒传了（MB）", "type": "float", "min": 0,
         "hint": "只有「先试秒传，等不到就真上传」这种模式用得上；「只秒传」模式下无效"},
        {"key": "upload.conflict_strategy", "label": "115 上已经有同名文件怎么办", "type": "select",
         "options": [{"value": "overwrite", "label": "用新的盖掉旧的"},
                     {"value": "rename", "label": "两个都留，新的改个名"},
                     {"value": "skip", "label": "旧的留着，新的不要"}]},
        {"key": "upload.delete_after_upload", "label": "传完就删掉本地源文件", "type": "bool",
         "danger_note": "删掉就找不回来了，确认 MoviePilot 那边不需要再留着它",
         "hint": "秒传命中（或分块传完）之后，把监控目录里那个源文件删掉，省地方。"
                 "播放不受影响——symedia 建好 strm 后是直接从云端读的，不碰这个文件。"
                 "只删文件本身，不删文件夹；只删监控目录里面的东西。"},
        {"key": "upload.partsize_mb", "label": "上传切多大一块（MB）", "type": "int", "min": 1, "max": 512,
         "advanced": True, "hint": "一般不用改"},
        {"key": "upload.init_min_interval", "label": "两次请求至少隔几秒", "type": "float", "min": 0,
         "advanced": True, "hint": "防止被 115 限流，一般不用改"},
        {"key": "upload.transient_retries", "label": "网络抽风时多试几次", "type": "int", "min": 0, "max": 10,
         "advanced": True},
    ]},
    {"title": "秒传没成功时要等多久", "fields": [
        {"key": "retry.interval_seconds", "label": "先每隔几秒问一次", "type": "int", "min": 1, "max": 86400,
         "hint": "只是问一句「这个文件现在有了吗」，不传数据、不费流量"},
        {"key": "retry.max_count", "label": "这样问几次", "type": "int", "min": 0, "max": 100,
         "hint": "问完这么多次还没等到，就换成下面的慢节奏继续等"},
        {"key": "retry.long_interval_seconds", "label": "之后每隔几秒问一次", "type": "int",
         "min": 1, "max": 86400,
         "hint": "默认 900 秒（15 分钟）。「只秒传」模式会一直问下去，不会放弃"},
        {"key": "retry.max_hours", "label": "最多等多久就放弃（小时）", "type": "float", "min": 0,
         "hint": "只有「先试秒传，等不到就真上传」这种模式用得上"},
    ]},
    {"title": "传完了告诉别的程序（可选）", "fields": [
        {"key": "notify.enabled", "label": "开启", "type": "bool",
         "hint": "文件传好之后通知别人一声。比如让 symedia 马上把播放用的文件建好，不用等它自己慢慢扫"},
        {"key": "notify.on", "label": "什么时候告诉它", "type": "select",
         "options": [{"value": "rapid_hit", "label": "秒传一成功就告诉（推荐）"},
                     {"value": "uploaded", "label": "真上传完了才告诉"},
                     {"value": "both", "label": "两种情况都告诉"}],
         "hint": "「只秒传」模式下基本都是秒传成功，选第一个就行"},
        {"key": "notify.url", "label": "告诉谁 · symedia 地址", "type": "text",
         "hint": "只填地址就行，例：http://172.17.0.1:8095（172.17.0.1 表示「这台机器」）。"
                 "后面的接口路径和固定参数会自动补上"},
        {"key": "notify.path_prefix", "label": "对方看到的路径开头", "type": "text", "advanced": True,
         "hint": "对方按 CD2 挂载路径找文件。默认值就是 symedia 的标准挂载，不用改"},
        {"key": "notify.query", "label": "地址后面固定带的参数", "type": "text", "advanced": True,
         "hint": "留空即可，程序会带默认值（device_name=upilot-mini&user_name=upilot-mini&type=notify）"},
        {"key": "notify.auth", "label": "对方要求密码时填这里", "type": "secret", "advanced": True,
         "hint": "比如 Basic xxxxx；对方没要求就留空"},
        {"key": "notify.timeout", "label": "最多等对方几秒", "type": "float", "min": 1, "max": 120,
         "advanced": True, "hint": "对方忙着处理、来不及回话也算成功，不会重复发"},
        {"key": "notify.retries", "label": "没送到就再试几次", "type": "int", "min": 0, "max": 5,
         "advanced": True},
    ]},
    {"title": "网页面板", "fields": [
        {"key": "http.enabled", "label": "打开网页面板", "type": "bool"},
        {"key": "http.auth.enabled", "label": "打开时要输密码", "type": "bool"},
        {"key": "http.auth.mode", "label": "怎么登录", "type": "select",
         "options": [{"value": "form", "label": "站内登录页（和面板同一个风格）"},
                     {"value": "basic", "label": "浏览器自带的账号密码小弹窗"}],
         "hint": "选第一个就行；弹窗那个是浏览器原生的白底框，比较丑"},
        {"key": "http.auth.username", "label": "用户名", "type": "text"},
        {"key": "http.auth.password", "label": "密码", "type": "secret",
         "hint": "留空 = 不改。改了之后，已登录的浏览器都要重新登一次"},
        {"key": "http.auth.session_hours", "label": "登录一次管多久（小时）", "type": "int",
         "min": 1, "max": 8760},
        {"key": "http.port", "label": "面板端口", "type": "int", "min": 1, "max": 65535, "restart": True,
         "hint": "默认 9844，一般不用改"},
        {"key": "http.public_url", "label": "对外网址（可选）", "type": "text", "advanced": True,
         "hint": "填了会显示在登录页底部，比如 https://up.example.com"},
        {"key": "http.host", "label": "监听地址", "type": "text", "restart": True, "advanced": True,
         "hint": "0.0.0.0 = 所有网卡都能访问，一般不用改"},
    ]},
    {"title": "运行日志", "fields": [
        {"key": "log_level", "label": "记多详细", "type": "select", "restart": True,
         "options": [{"value": "DEBUG", "label": "最详细（排查问题时才用）"},
                     {"value": "INFO", "label": "正常（推荐）"},
                     {"value": "WARNING", "label": "只看警告和错误"},
                     {"value": "ERROR", "label": "只看错误"}]},
    ]},
]

RELOAD_KEYS = {"p115.cookie", "p115.app"}          # 改了要重建 115 客户端
# 这几段是 Uploader / Notifier 构造时读的：改了就顺手重建，免得非要重启容器才生效
RELOAD_PREFIXES = ("notify.", "upload.", "retry.")
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
        "reload": any(k in RELOAD_KEYS or k.startswith(RELOAD_PREFIXES) for k in changed),
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
            # 按当前配置补一句提示，免得在「只秒传」模式下白折腾用不到的项
            if cfg["upload"].get("mode") == "rapid_only":
                if field["key"] in ("upload.small_file_mb", "retry.max_hours"):
                    item["note"] = "当前是「只秒传」模式，这一项用不到"
            nt = cfg.get("notify") or {}
            if (field["key"] == "notify.on" and nt.get("on") == "uploaded"
                    and cfg["upload"].get("mode") == "rapid_only"):
                item["note"] = "「只秒传」模式下永远不会真上传，选这项就收不到通知"
            if field["key"] == "notify.url" and nt.get("enabled") and not nt.get("url"):
                item["note"] = "已经开启但还没填地址，现在是不会发通知的"
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
