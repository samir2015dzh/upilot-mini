"""A2A 客户端：把请求发给远程 agent，并回报 exit code / HTTP status / 完整响应体。

凭据约定：token 只在本机内存与 HTTP 头之间流转 —— 不打印、不落盘、不进回复。
本机若设了 HTTP 代理， a2a 工具报 Errno 10061，所以走 curl + --noproxy '*'。

用法：
    python tools/a2a_send.py <请求文本文件> [context_id]

配置文件位置可用 HERMES_CONFIG 覆盖（默认取本机 当前目录的 a2a_config.yaml）。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid

import yaml

CFG = os.environ.get("HERMES_CONFIG", "a2a_config.yaml")
# 端点从配置里取（a2a_agents.server.url），环境变量 A2A_ENDPOINT 可覆盖。
# 仓库是公开的，所以这里不写死任何内网/私有地址作为默认值。
ENDPOINT_ENV = "A2A_ENDPOINT"
# a2a_agents 是**嵌套三层**的结构，不是平铺一层。
A2A_TOKEN_PATH = ("a2a_agents", "server", "auth", "token")
A2A_URL_PATH = ("a2a_agents", "server", "url")
TOKEN_KEYS = ("token", "auth_token", "bearer")


def _dig(obj, *path):
    cur = obj
    for part in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def resolve_token(cfg: dict) -> str:
    """从配置里取 A2A bearer token。

    真实结构是嵌套的 `a2a_agents.server.auth.token`，所以先按已知路径取，
    再按 token 键名递归兜底；都取不到就明确失败（绝不静默给出空 Authorization）。
    """
    for path in (A2A_TOKEN_PATH, ("a2a_agents", "server", "token")):
        tok = _dig(cfg, *path)
        if tok:
            return str(tok).strip()

    def search(obj, depth=0):
        if depth > 6 or not isinstance(obj, dict):
            return None
        for k, v in obj.items():
            if str(k) in TOKEN_KEYS and isinstance(v, str) and v.strip():
                return v.strip()
            found = search(v, depth + 1)
            if found:
                return found
        return None

    tok = search(cfg.get("a2a_agents") or {})
    if tok:
        return tok
    raise SystemExit("no a2a token found")


def build_headers(token: str) -> dict:
    """token → 预期的 HTTP 头（测试断言的就是这个形状）。"""
    return {"Content-Type": "application/json", "Authorization": "Bearer " + str(token)}


def load_token(path: str | None = None) -> str:
    with open(path or CFG, encoding="utf-8") as fh:
        return resolve_token(yaml.safe_load(fh) or {})


def resolve_endpoint(cfg: dict | None = None) -> str:
    """A2A 服务端地址：环境变量 > 配置里的 a2a_agents.server.url。

    没有硬编码默认值 —— 这类地址属于部署细节，不该随公开仓库分发。
    """
    env = str(os.environ.get(ENDPOINT_ENV) or "").strip()
    if env:
        return env
    url = _dig(cfg or {}, *A2A_URL_PATH)
    if url:
        return str(url).strip()
    raise SystemExit(f"no a2a endpoint found: set {ENDPOINT_ENV} or a2a_agents.server.url")


def load_config(path: str | None = None) -> dict:
    with open(path or CFG, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def build_payload(text: str, context_id: str | None = None) -> dict:
    payload = {
        "jsonrpc": "2.0",
        "id": str(uuid.uuid4()),
        "method": "message/send",
        "params": {
            "message": {
                "role": "user",
                "messageId": str(uuid.uuid4()),
                "parts": [{"kind": "text", "text": text}],
            }
        },
    }
    if context_id:
        payload["params"]["message"]["contextId"] = context_id
    return payload


def send(text: str, context_id: str | None = None, timeout: int = 900) -> dict:
    """真发一次。返回 {exit_code, http_status, body, stderr}。

    token 只在内存与 curl 的进程参数之间流转：不打印、不写文件、不进回复。
    curl 走 --noproxy '*'（本机代理会让内网/自建地址连不上）。
    """
    cfg = load_config()
    headers = build_headers(resolve_token(cfg))
    endpoint = resolve_endpoint(cfg)
    message = json.dumps(build_payload(text, context_id)).encode("utf-8")
    cmd = ["curl", "-sS", "--noproxy", "*", "--max-time", str(timeout),
           "-X", "POST", endpoint,
           "-w", "\n__HTTP_STATUS__%{http_code}\n",
           "-H", "Content-Type: " + headers["Content-Type"],
           "-H", "Authorization: " + headers["Authorization"],
           "--data-binary", "@-"]
    proc = subprocess.run(cmd, input=message, capture_output=True)
    out = proc.stdout.decode("utf-8", "replace")
    err = proc.stderr.decode("utf-8", "replace")
    status = ""
    if "__HTTP_STATUS__" in out:
        out, _, tail = out.rpartition("\n__HTTP_STATUS__")
        status = tail.strip()
    return {"exit_code": proc.returncode, "http_status": status,
            "body": out.strip(), "stderr": err.strip()}


def extract_text(body: str) -> str:
    """从 JSON-RPC 响应里取出可读文本（parts / message.parts 两处都认）。"""
    try:
        resp = json.loads(body)
    except ValueError:
        return body
    res = (resp or {}).get("result") or {}
    parts = [p for p in (res.get("parts") or [])
             if isinstance(p, dict) and (p.get("kind") == "text" or p.get("text"))]
    if not parts:
        msg = res.get("message") or {}
        parts = [p for p in (msg.get("parts") or [])
                 if isinstance(p, dict) and p.get("text")]
    out = [str(p.get("text") or "") for p in parts]
    return "\n".join(out) if out else json.dumps(res, ensure_ascii=False)


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: python tools/a2a_send.py <request.txt> [context_id]")
        return 2
    with open(sys.argv[1], encoding="utf-8") as fh:
        text = fh.read()
    ctx = sys.argv[2] if len(sys.argv) > 2 else None
    r = send(text, ctx)
    print(f"exit_code={r['exit_code']}")
    print(f"http_status={r['http_status'] or '(none)'}")
    if r["stderr"]:
        print(f"stderr={r['stderr'][:400]}")
    print("---- response body ----")
    print(extract_text(r["body"]))
    return 0 if (r["exit_code"] == 0 and r["http_status"].startswith("2")) else 1


if __name__ == "__main__":
    raise SystemExit(main())
