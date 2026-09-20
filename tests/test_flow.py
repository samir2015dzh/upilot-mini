"""本机离线测试：不需要 115 cookie、不联网。

运行：python tests/test_flow.py
（sys.path 指向 ../app；StubP115 替代真实 115 客户端）
"""
from __future__ import annotations

import asyncio
import base64
import builtins
import copy
import gc
import glob
import os
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.request

APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")
sys.path.insert(0, APP)

import config as cfgmod          # noqa: E402
import scanner                   # noqa: E402
import webui                     # noqa: E402
from p115 import StubP115, resolve_upload_target   # noqa: E402
from store import Store          # noqa: E402
from uploader import Uploader, sha1_of   # noqa: E402

PASS: list[str] = []


def make_env(stable_rounds: int = 1, small_file_mb: float = 10.0, retry_max_count: int = 5,
             retry_interval: int = 0, fail_first: int = 2):
    tmp = tempfile.mkdtemp(prefix="upilot-test-")
    src = os.path.join(tmp, "source")
    os.makedirs(src, exist_ok=True)
    cfg = copy.deepcopy(cfgmod.DEFAULTS)
    cfg["paths"] = {"source": src, "target_root": "/UPILOT-mini"}
    cfg["state"] = {"db": os.path.join(tmp, "state.db")}
    cfg["scan"]["stable_rounds"] = stable_rounds
    cfg["scan"]["include_suffixes"] = []   # 测试里默认不做扩展名过滤（新用例自行覆盖）
    cfg["upload"]["small_file_mb"] = small_file_mb
    cfg["retry"]["interval_seconds"] = retry_interval
    cfg["retry"]["max_count"] = retry_max_count
    cfg["http"]["enabled"] = False
    store = Store(cfg["state"]["db"])
    mgr = StubP115(cfg, store, fail_first=fail_first)
    up = Uploader(cfg, store, mgr)
    return cfg, store, mgr, up, tmp, src


def write_file(src: str, rel: str, size: int) -> str:
    path = os.path.join(src, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(os.urandom(1024) * (size // 1024) if size >= 1024 else b"x" * size)
    return os.path.normpath(path)      # 与 scanner 用 os.walk 得到的路径写法保持一致


async def drive(up: Uploader, store: Store, path: str, rounds: int = 15) -> dict:
    """模拟调度器：反复推进一个文件，直到终态。"""
    for _ in range(rounds):
        rec = store.get(path)
        if rec["status"] in ("已上传", "错误"):
            return rec
        if rec["status"] == "待重试":
            store.set(path, next_retry_at=0)      # 让重试立即到期
        await up._process(path)
    return store.get(path)


async def case_folder_and_rapid_hit():
    cfg, store, mgr, up, tmp, src = make_env(stable_rounds=1, fail_first=2, small_file_mb=1.0)
    try:
        path = write_file(src, "电影/哈利与巧克力工厂/哈利与巧克力工厂.mp4", 3 * 1024 * 1024)
        scanner.scan_once(store, cfg)             # 入队（stable_count=0）
        scanner.scan_once(store, cfg)             # stable_count=1 → 稳定
        rec = await drive(up, store, path)
        assert rec["status"] == "已上传", rec
        assert rec["target_dir"] == "/UPILOT-mini/电影/哈利与巧克力工厂", rec["target_dir"]
        assert mgr._rapid_calls[path] == 3, mgr._rapid_calls      # 2 次未命中 + 1 次命中
        assert set(mgr._mem.keys()) == {"/UPILOT-mini/电影/哈利与巧克力工厂"}, mgr._mem
        assert rec["message"].startswith("秒传命中"), rec["message"]
        PASS.append(f"[1] 目录保留 + 秒传重试命中：{rec['target_dir']}，校验请求 3 次，"
                    f"status={rec['status']}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def case_sha1_cached_no_reread():
    cfg, store, mgr, up, tmp, src = make_env(stable_rounds=1, fail_first=1)
    try:
        path = write_file(src, "剧集/S01/E01.mkv", 2 * 1024 * 1024)
        scanner.scan_once(store, cfg)
        scanner.scan_once(store, cfg)
        await drive(up, store, path)
        rec = store.get(path)
        assert rec["sha1"], rec

        real_open = builtins.open

        def deny(file, *a, **k):
            if str(file).endswith("E01.mkv"):
                raise AssertionError("不应重新读取文件")
            return real_open(file, *a, **k)

        builtins.open = deny
        try:
            again = await sha1_of(path, store.get(path), store)
        finally:
            builtins.open = real_open
        assert again == rec["sha1"]
        assert len(mgr._mem) == 1                 # cid 缓存：同目录只建一次
        PASS.append(f"[2] SHA1 落库复用（未再读盘）sha1={again[:12]}…")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def case_small_file_goes_direct_upload():
    cfg, store, mgr, up, tmp, src = make_env(stable_rounds=0, small_file_mb=10.0, fail_first=99)
    try:
        path = write_file(src, "小文件/cover.jpg", 200 * 1024)
        scanner.scan_once(store, cfg)
        rec = await drive(up, store, path)
        assert rec["status"] == "已上传", rec
        assert mgr._rapid_calls[path] == 1, mgr._rapid_calls     # 只试一次秒传
        assert "分块上传完成" in (rec["message"] or ""), rec["message"]
        PASS.append("[3] 小文件秒传未命中 → 直接分块上传（不进重试）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def case_retry_exhausted_falls_back():
    cfg, store, mgr, up, tmp, src = make_env(stable_rounds=0, retry_max_count=2, fail_first=99)
    try:
        path = write_file(src, "大文件/movie.mkv", 12 * 1024 * 1024)
        scanner.scan_once(store, cfg)
        rec = await drive(up, store, path)
        assert rec["status"] == "已上传", rec
        assert mgr._rapid_calls[path] == 3, mgr._rapid_calls     # 首次 + 2 次重试
        assert rec["retry_count"] == 3, rec["retry_count"]
        assert "分块上传完成" in (rec["message"] or "")
        PASS.append("[4] 重试耗尽（1 次 + 2 次重试）→ 转分块上传成功")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def case_scan_dedupe_and_rewrite():
    cfg, store, mgr, up, tmp, src = make_env(stable_rounds=1)
    try:
        path = write_file(src, "去重/a.bin", 4096)
        scanner.scan_once(store, cfg)
        st = os.stat(path)
        assert store.seen(path, st.st_size, st.st_mtime) is not None
        before = store.get(path)
        scanner.scan_once(store, cfg)             # 同 size/mtime：只推进稳定计数
        after = store.get(path)
        assert after["stable_count"] >= before["stable_count"]
        # 覆盖写：size 变化 → 视为新文件（旧 sha1/断点作废）
        write_file(src, "去重/a.bin", 8192)
        os.utime(path, (st.st_mtime + 5, st.st_mtime + 5))
        scanner.scan_once(store, cfg)
        rewritten = store.get(path)
        assert rewritten["sha1"] is None and rewritten["status"] == "待处理", rewritten
        PASS.append("[5] 扫描去重 + 覆盖重写识别（sha1/断点作废重来）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def case_scan_suffix_and_dir_filter():
    """扩展名白名单 + exclude_dirs：非视频扩展名不入队，排除目录整棵跳过。"""
    cfg, store, mgr, up, tmp, src = make_env(stable_rounds=1)
    try:
        cfg["scan"]["include_suffixes"] = [".mkv", ".mp4"]
        cfg["scan"]["exclude_dirs"] = ["Sample", "剧集/三体/extras"]
        keep1 = write_file(src, "电影/沙丘2.mkv", 2 * 1024 * 1024)
        keep2 = write_file(src, "剧集/三体/S01E01.mp4", 1024 * 1024)
        write_file(src, "电影/沙丘2.nfo", 2048)                   # 扩展名不在白名单
        write_file(src, "电影/poster.jpg", 4096)                  # 同上
        write_file(src, "电影/沙丘2.strm", 128)                   # strm 元数据不该上传
        write_file(src, "Sample/沙丘2.sample.mkv", 1024 * 1024)   # 顶层排除目录
        write_file(src, "剧集/三体/extras/bonus.mkv", 512 * 1024)  # 多级排除目录

        scanner.scan_once(store, cfg)
        queued = {r["path"] for r in store.by_status("待处理", limit=50)}
        assert queued == {keep1, keep2}, queued
        assert store.counts() == {"待处理": 2}, store.counts()

        # 白名单留空 = 不过滤；排除清空 = 全都扫
        cfg["scan"]["include_suffixes"] = []
        cfg["scan"]["exclude_dirs"] = []
        scanner.scan_once(store, cfg)
        total = sum(store.counts().values())
        assert total == 7, store.counts()
        PASS.append("[6] 扩展名白名单 + exclude_dirs：只收视频本体、排除目录整棵跳过；两者留空则全收")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def http_get(url: str, user: str | None = None, pwd: str = "") -> tuple[int, bytes]:
    req = urllib.request.Request(url)
    if user is not None:
        token = base64.b64encode(f"{user}:{pwd}".encode()).decode()
        req.add_header("Authorization", f"Basic {token}")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read()[:64]
    except urllib.error.HTTPError as exc:
        return exc.code, b""


async def case_http_auth():
    """HTTP 鉴权：开启后页面与 API 都要凭据，/health 始终放行，关掉即恢复开放。"""
    cfg, store, mgr, up, tmp, src = make_env(stable_rounds=1)
    srv = None
    try:
        cfg["http"] = {"enabled": True, "host": "127.0.0.1", "port": 0,
                       "auth": {"enabled": True, "username": "upilot", "password": "s3cret-token"}}
        state = {"scan_rounds": 0, "dry_run": True}
        srv = webui.serve(cfg, store, state)
        base = f"http://127.0.0.1:{srv.server_address[1]}"

        assert http_get(f"{base}/health")[0] == 200, "健康检查应始终放行"
        assert http_get(f"{base}/api/status")[0] == 401, "无凭据应 401"
        assert http_get(f"{base}/")[0] == 401, "页面无凭据应 401"
        assert http_get(f"{base}/api/status", "upilot", "wrong")[0] == 401, "错密码应 401"
        assert http_get(f"{base}/api/status", "upilot", "s3cret-token")[0] == 200, "Basic 应放行"
        assert http_get(f"{base}/api/status?token=s3cret-token")[0] == 200, "?token= 应放行"

        cfg["http"]["auth"]["enabled"] = False            # 关掉鉴权 → 恢复开放
        assert http_get(f"{base}/api/status")[0] == 200, "关闭后应开放"
        PASS.append("[7] HTTP 鉴权：Basic 与 ?token= 放行、无凭据 401、/health 始终 200、可随时关闭")
    finally:
        if srv is not None:
            srv.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)


async def case_form_auth():
    """站内登录页（mode=form）：未登录跳 /login，登录后 Cookie 放行，Basic 与 ?token= 仍是脚本通道。"""
    cfg, store, mgr, up, tmp, src = make_env(stable_rounds=1)
    srv = None

    class _NoRedirect(urllib.request.HTTPRedirectHandler):      # 保留 302，便于断言跳转目标
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    opener = urllib.request.build_opener(_NoRedirect)

    def raw(url: str, cookie: str = "") -> tuple[int, dict, bytes]:
        req = urllib.request.Request(url)
        if cookie:
            req.add_header("Cookie", cookie)
        try:
            resp = opener.open(req, timeout=5)
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, {k.lower(): v for k, v in exc.headers.items()}, exc.read()

    def login(user: str, pwd: str, nxt: str = "/") -> tuple[int, dict, bytes]:
        data = urllib.parse.urlencode({"username": user, "password": pwd, "next": nxt}).encode()
        req = urllib.request.Request(f"{base}/login", data=data,
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
        try:
            resp = opener.open(req, timeout=5)
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, {k.lower(): v for k, v in exc.headers.items()}, exc.read()

    try:
        cfg["http"] = {"enabled": True, "host": "127.0.0.1", "port": 0,
                       "auth": {"enabled": True, "mode": "form", "username": "admin",
                                "password": "s3cret-token", "session_hours": 72}}
        state = {"scan_rounds": 0, "dry_run": True}
        srv = webui.serve(cfg, store, state)
        port = srv.server_address[1]
        base = f"http://127.0.0.1:{port}"

        # 未登录：页面跳登录页，API 给 401 JSON，/health 照旧放行
        assert raw(f"{base}/health")[0] == 200, "健康检查应始终放行"
        code, hdrs, _ = raw(f"{base}/")
        assert code == 302 and hdrs.get("location", "").startswith("/login?next="), \
            f"未登录访问首页应 302 到登录页，实际 {code} {hdrs.get('location')}"
        code, _, body = raw(f"{base}/api/status")
        assert code == 401 and b'"login"' in body, "未登录 API 应 401 并给出登录地址"
        code, _, body = raw(f"{base}/login")
        assert code == 200 and b"login" in body and b'name="password"' in body, "登录页应 200 且带密码框"

        # 登录
        code, _, body = login("admin", "wrong")
        assert code == 401 and "用户名或密码不对".encode() in body, "错密码应回到登录页并提示"
        code, hdrs, _ = login("admin", "s3cret-token", "/api/status")
        jar = hdrs.get("set-cookie", "")
        assert code == 302 and hdrs.get("location") == "/api/status", f"登录成功应 302 回原地址，实际 {code}"
        assert jar.startswith("upilot_session=") and "HttpOnly" in jar and "SameSite=Lax" in jar, \
            f"应下发 HttpOnly 会话 Cookie，实际 {jar[:60]}"
        token = jar.split(";", 1)[0]

        # 带着 Cookie：页面与 API 都放行
        assert raw(f"{base}/api/status", token)[0] == 200, "带会话 Cookie 访问 API 应放行"
        code, _, body = raw(f"{base}/", token)
        assert code == 200 and b"UPilot-mini" in body, "带会话 Cookie 应看到仪表盘"

        # 伪造 Cookie 不放行；Basic 与 ?token= 仍是脚本通道
        assert raw(f"{base}/api/status", "upilot_session=ZmFrZXxleHA=.deadbeef")[0] == 401, "伪造 Cookie 应 401"
        assert http_get(f"{base}/api/status", "admin", "s3cret-token")[0] == 200, "Basic 通道应保留"
        assert http_get(f"{base}/api/status?token=s3cret-token")[0] == 200, "?token= 通道应保留"

        # 退出：清 Cookie 并回登录页
        code, hdrs, _ = raw(f"{base}/logout", token)
        assert code == 302 and hdrs.get("location") == "/login", "退出应回登录页"
        assert "Max-Age=0" in hdrs.get("set-cookie", ""), "退出应清 Cookie"

        # 关掉鉴权 → 恢复开放
        cfg["http"]["auth"]["enabled"] = False
        assert raw(f"{base}/api/status")[0] == 200, "关闭鉴权后应开放"
        PASS.append("[8] 站内登录页（form）：未登录 302 /login、错密码提示、HttpOnly Cookie 放行、"
                    "伪造 Cookie 401、Basic 与 ?token= 保留、退出清 Cookie、/health 始终 200")
    finally:
        if srv is not None:
            srv.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)


async def case_dir_mapping():
    """本机相对路径 → 115 目录：改名（Anime→/OtherAnime）、大小写、最长前缀、未命中兜底。"""
    cfg, store, mgr, up, tmp, src = make_env(stable_rounds=1, fail_first=0, small_file_mb=0.0)
    try:
        cfg["paths"]["target_root"] = "/UPILOT-mini"
        cfg["paths"]["map"] = [
            {"from": "TVs", "to": "/TVs"},
            {"from": "Movies/Others", "to": "/Movies/Others"},
            {"from": "Movies/AnimeMovies", "to": "/Movies/AnimeMovies"},
            {"from": "Anime", "to": "/OtherAnime"},
            {"from": "Something", "to": "/something"},
        ]
        cases = {
            "TVs/我的剧集/S01E01.mkv": "/TVs/我的剧集",
            "Movies/Others/某电影/movie.mkv": "/Movies/Others/某电影",
            "Movies/AnimeMovies/某动画电影/a.mkv": "/Movies/AnimeMovies/某动画电影",
            "Anime/葬送的芙莉莲/E01.mkv": "/OtherAnime/葬送的芙莉莲",
            "Something/杂物/a.bin": "/something/杂物",
            "Movies/某电影/hit.mkv": "/UPILOT-mini/Movies/某电影",        # 未命中 → 回落 target_root
            "tvs/小写不算命中/x.mkv": "/UPILOT-mini/tvs/小写不算命中",     # 大小写敏感
            "顶层的文件.mkv": "/UPILOT-mini",                            # 没有子目录
        }
        for rel, want in cases.items():
            full = os.path.join(src, rel.replace("/", os.sep))
            got = resolve_upload_target(full, src, cfg["paths"]["target_root"], cfg["paths"]["map"])
            assert got == want, f"{rel} → {got}（期望 {want}）"

        path = write_file(src, "Anime/葬送的芙莉莲/E01.mkv", 2 * 1024 * 1024)
        scanner.scan_once(store, cfg)
        scanner.scan_once(store, cfg)
        rec = await drive(up, store, path)
        assert rec["status"] == "已上传", rec
        assert rec["target_dir"] == "/OtherAnime/葬送的芙莉莲", rec["target_dir"]
        assert "/OtherAnime/葬送的芙莉莲" in mgr._mem, mgr._mem
        PASS.append("[9] 目录映射：TVs→/TVs、Anime→/OtherAnime（改名）、Movies/Others 保留、"
                    "大小写敏感、最长前缀、未命中回落 target_root")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def case_settings_map_and_slots():
    """设置页：多行「本机 => 115 目录」解析与回填、坏行拒绝、登录槽位中文下拉。"""
    field = cfgmod.find_field("paths.map")
    assert field is not None and field["type"] == "map", field
    parsed = cfgmod.coerce(field, "TVs => /TVs\nAnime -> /OtherAnime\n\n# 注释行跳过\nSomething=>something")
    assert parsed == [{"from": "TVs", "to": "/TVs"}, {"from": "Anime", "to": "/OtherAnime"},
                      {"from": "Something", "to": "/something"}], parsed
    # 「=> /x」是合法的：from 留空表示整个监控根映射过去
    assert cfgmod.coerce(field, "=> /root-mapped") == [{"from": "", "to": "/root-mapped"}]
    for bad in ("只有一边", "TVs => "):
        try:
            cfgmod.coerce(field, bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"坏行应被拒：{bad!r}")

    cfg = copy.deepcopy(cfgmod.DEFAULTS)
    cfg["paths"]["map"] = parsed
    payload = cfgmod.form_payload(cfg)
    text = [f for g in payload["groups"] for f in g["fields"] if f["key"] == "paths.map"][0]["value"]
    assert "Anime => /OtherAnime" in text, text
    assert "Something => /something" in text, text

    appf = cfgmod.find_field("p115.app")
    assert appf["type"] == "select" and appf["options"][0]["value"] == "115ipad", appf
    assert cfgmod.app_label("qandroid") == "115管理(Android端)"
    assert cfgmod.app_label("ipad") == "115(iPad端)"
    assert "qandroid" in cfgmod.QR_UNSUPPORTED and "115ipad" not in cfgmod.QR_UNSUPPORTED
    assert all(x["qr"] is True for x in cfgmod.APP_OPTIONS), "下拉里只留能扫码的槽位"
    assert not (set(cfgmod.APP_VALUES) & cfgmod.QR_UNSUPPORTED), "已停用的槽位不该出现在下拉里"
    assert cfgmod.app_label("qandroid") == "115管理(Android端)", "老配置里的槽位仍要能读出中文名"
    assert len(cfgmod.APP_OPTIONS) == 13, len(cfgmod.APP_OPTIONS)
    try:
        cfgmod.coerce(appf, "不存在的端")
    except ValueError:
        pass
    else:
        raise AssertionError("未知登录槽位应被拒")
    PASS.append("[10] 设置页：映射多行解析/回填、坏行拒绝；登录槽位中文下拉 + 未知值拒绝")



async def case_qr_prompt_confirm_flow():
    """扫码流程：轮询到「已扫码」时要先接应急 prompt.php + slogin.php，再接 status=2 取 cookie。

    115 现行流程少了「网页端接应」这一步，手机 App 就会报「扫描失败」。这条用例把它钉住：
    调用顺序必须是 token → 二维码图 → status(1) → 建客户端 → prompt → confirm → status(2) → result。
    """
    import json
    import pathlib
    import types

    cfg, store, mgr, up, tmp, src = make_env(stable_rounds=1)
    srv = None
    calls = []
    box = {"n": 0}

    def token(app="web", **kw):
        calls.append(("token", app))
        return {"state": True, "data": {"uid": "U" * 40, "time": "1789000000", "sign": "S" * 32}}

    def qrcode(payload, app="web", **kw):
        calls.append(("img", app))
        return b"\x89PNG\r\n\x1a\n" + b"0" * 40

    def scan_status(payload, **kw):
        box["n"] += 1
        calls.append(("status", box["n"], str(payload.get("uid"))[:1]))
        return {"state": True, "data": {"status": 1 if box["n"] == 1 else 2}}

    def scan_result(uid, app="web", **kw):
        calls.append(("result", app))
        return {"state": True, "data": {"cookie": {"UID": "u", "CID": "c", "SEID": "s", "KID": "k"}}}

    def fake_qr_api(path, params, timeout=10.0):
        """替掉真实 HTTP：只记录接应调用，断言 prompt 必须先于 confirm。"""
        if path.endswith("prompt.php"):
            calls.append(("prompt", str(params.get("uid"))[:1]))
            return {"state": True, "data": {"key": "KEY123"}}
        calls.append(("confirm", params.get("key"), params.get("client")))
        return {"state": True}

    fake = types.ModuleType("p115client")
    fake.P115Client = type("P115Client", (), {
        "login_qrcode_token": staticmethod(token),
        "login_qrcode": staticmethod(qrcode),
        "login_qrcode_scan_status": staticmethod(scan_status),
        "login_qrcode_scan_result": staticmethod(scan_result),
    })

    def post_json(url, payload):
        req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def get_json(url):
        try:
            with urllib.request.urlopen(url, timeout=10) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    old_mod = sys.modules.get("p115client")
    old_path = cfgmod.CONFIG_PATH
    old_api = webui._qr_api
    webui._qr_api = fake_qr_api
    sys.modules["p115client"] = fake
    try:
        cfgmod.CONFIG_PATH = pathlib.Path(tmp) / "config.yaml"
        cfgmod.CONFIG_PATH.write_text('p115:\n  cookie: ""\n  app: "115ipad"\n', encoding="utf-8")
        cfg["http"] = {"enabled": True, "host": "127.0.0.1", "port": 0, "auth": {"enabled": False}}
        srv = webui.serve(cfg, store, {"scan_rounds": 0, "dry_run": True})
        base = f"http://127.0.0.1:{srv.server_address[1]}"

        code, body = post_json(base + "/api/p115/qrcode", {})
        assert code == 200 and b"waiting" in body, (code, body[:120])
        code, body = get_json(base + "/api/p115/qrcode/status")     # 状态是 GET
        assert code == 200 and b"scanned" in body, (code, body[:160])
        code, body = get_json(base + "/api/p115/qrcode/status")
        assert code == 200 and b"done" in body, (code, body[:160])

        kinds = [c[0] for c in calls]
        assert kinds == ["token", "img", "status", "prompt", "confirm", "status", "result"], kinds
        assert ("confirm", "KEY123", 0) in calls, calls
        text = cfgmod.CONFIG_PATH.read_text(encoding="utf-8")
        assert "UID=u" in text and "KID=k" in text, text[:200]
        PASS.append("[11] 扫码流程接应：status=1 时先 prompt.php 再 slogin.php → status=2 → 取 cookie；"
                    "顺序 " + " → ".join(kinds))
    finally:
        if srv is not None:
            srv.shutdown()
        cfgmod.CONFIG_PATH = old_path
        webui._qr_api = old_api
        if old_mod is not None:
            sys.modules["p115client"] = old_mod
        else:
            sys.modules.pop("p115client", None)
        shutil.rmtree(tmp, ignore_errors=True)



def case_cookie_sync_from_file():
    """从外部程序配置同步 cookie：嵌套 yaml / 字典形式 / JSON 都能抠出来，无关文件返回空。"""
    tmp = tempfile.mkdtemp(prefix="upilot-ck-")
    try:
        p1 = os.path.join(tmp, "a.yaml")
        with open(p1, "w", encoding="utf-8") as f:
            f.write('settings_115:\n  - name: x\n    cookies: "UID=1_A; CID=2_B; SEID=3_C; KID=4_D"\n')
        ck = cfgmod.load_cookie_from(p1)
        assert ck == "UID=1_A; CID=2_B; SEID=3_C; KID=4_D", ck

        p2 = os.path.join(tmp, "b.json")
        with open(p2, "w", encoding="utf-8") as f:
            f.write('{"a": {"cookies": {"UID": "u", "CID": "c", "SEID": "s"}}}')
        ck2 = cfgmod.load_cookie_from(p2)
        assert "UID=u" in ck2 and "CID=c" in ck2 and "SEID=s" in ck2, ck2

        p3 = os.path.join(tmp, "c.yaml")
        with open(p3, "w", encoding="utf-8") as f:
            f.write("nothing: here\n")
        assert cfgmod.load_cookie_from(p3) == ""
        assert cfgmod.find_field("p115.cookie_from") is not None
        PASS.append("[12] cookie 同步：外部 yaml（嵌套 list/dict）与 json 都能抠出 115 cookie，无关文件返回空")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def main():
    await case_folder_and_rapid_hit()
    await case_sha1_cached_no_reread()
    await case_small_file_goes_direct_upload()
    await case_retry_exhausted_falls_back()
    await case_scan_dedupe_and_rewrite()
    await case_scan_suffix_and_dir_filter()
    await case_http_auth()
    await case_form_auth()
    await case_qr_prompt_confirm_flow()
    await case_dir_mapping()
    case_settings_map_and_slots()
    case_cookie_sync_from_file()
    # 兜底：清掉遗留的测试临时目录（SQLite 句柄在 GC 后才释放，所以要重试几次）
    for _ in range(3):
        left = [d for d in glob.glob(os.path.join(tempfile.gettempdir(), "upilot-test-*")) if os.path.isdir(d)]
        if not left:
            break
        gc.collect()
        for d in left:
            shutil.rmtree(d, ignore_errors=True)
        time.sleep(0.2)
    print("\n".join(PASS))
    print(f"\n全部通过：{len(PASS)} 个用例")


if __name__ == "__main__":
    asyncio.run(main())
