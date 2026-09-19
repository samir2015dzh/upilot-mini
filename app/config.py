"""配置加载：/config/config.yaml 为主，环境变量可覆盖 cookie。"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

import yaml

CONFIG_PATH = Path(os.environ.get("UPILOT_CONFIG", "/config/config.yaml"))
EXAMPLE_PATH = Path("/app/config/config.example.yaml")

DEFAULTS: dict = {
    "p115": {"cookie": "", "app": "qandroid"},
    "paths": {"source": "/media/source", "target_root": "/UPILOT-mini"},
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
