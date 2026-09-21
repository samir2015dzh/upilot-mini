【部署 + 验证 · upilot-mini（第 39~40 处：鉴权三通道统一常数时间比较）】逐条执行并原样回贴，每条前加 ##1~##6 标号：

1) 先给当前镜像留回滚名：
docker tag $(docker inspect -f '{{.Image}}' upilot-mini) samir2013dzh/upilot-mini:pre-runbook14-$(date +%Y%m%d-%H%M%S); echo "TAGGED"
docker compose -f /home/<user>/upilot-mini.yml pull

2) docker compose -f /home/<user>/upilot-mini.yml up -d --force-recreate

3) sleep 25 && docker ps --filter name=upilot-mini --format '{{.Names}} {{.Status}}' && docker inspect -f 'Image={{.Image}} RestartCount={{.RestartCount}} OOMKilled={{.State.OOMKilled}} StartedAt={{.State.StartedAt}}' upilot-mini

4) 符号计数（新代码是否在跑）：
docker exec upilot-mini sh -c 'grep -c "_secure_eq" /app/app/webui.py; grep -c "compare_digest" /app/app/webui.py; grep -c "== pwd" /app/app/webui.py; grep -c "secure_eq" /app/tests/test_flow.py'

5) ★ 第 39~40 处的判据 cell（直接锤三通道 × 非 ASCII 凭据）：
docker exec -i upilot-mini python - <<'PYEOF'
import sys, base64, urllib.parse
sys.path.insert(0, "/app/app")
import config as cfgmod, webui

print("has-_secure_eq:", hasattr(webui, "_secure_eq"))
print("sec-eq-ascii:", webui._secure_eq("abc", "abc"), webui._secure_eq("abc", "abd"))
print("sec-eq-cjk:", webui._secure_eq("\u5bc6\u7801", "\u5bc6\u7801"), webui._secure_eq("\u5bc6\u7801", "\u5bc6\u78bc"))
print("sec-eq-emoji:", webui._secure_eq("p@ss\U0001F510", "p@ss\U0001F510"))

USR, PWD = "\u7ba1\u7406\u5458", "\u5bc6\u7801\u4e2d\u6587\u6d4b\u8bd5\U0001F510"
a = {"enabled": True, "mode": "basic", "username": USR, "password": PWD}
print("creds-cjk-ok:", webui.check_credentials(a, USR, PWD))
print("creds-cjk-bad:", webui.check_credentials(a, USR, "wrong"))

c = {"http": {"auth": dict(a, session_hours=72)}}
print("token-ok:", webui.check_auth(c, {}, "token=" + urllib.parse.quote(PWD)))
print("token-bad:", webui.check_auth(c, {}, "token=nope"))
tok = webui.make_session(USR, PWD, 1)
print("cookie-ok:", webui.check_auth(c, {"Cookie": webui.COOKIE_NAME + "=" + tok}, ""))
ok = base64.b64encode((USR + ":" + PWD).encode()).decode()
bad = base64.b64encode((USR + ":wrong").encode()).decode()
print("basic-ok:", webui.check_auth(c, {"Authorization": "Basic " + ok}, ""))
print("basic-bad:", webui.check_auth(c, {"Authorization": "Basic " + bad}, ""))

try:
    import hmac
    hmac.compare_digest("\u5bc6\u7801", "\u5bc6\u7801")
    print("legacy-raises: False")
except TypeError:
    print("legacy-raises: True")
print("EXPECT-NEW: has-_secure_eq=True legacy-raises=True basic-ok=True basic-bad=False")
PYEOF

6) 正常接口 + 日志 + 文件指纹：
curl -s --noproxy '*' http://127.0.0.1:9844/health; echo
docker logs --tail 8 upilot-mini 2>&1
docker logs --since 10m upilot-mini 2>&1 | grep -ciE "non-ASCII|TypeError|traceback|异常" || echo 0
docker exec upilot-mini sh -c 'md5sum /config/config.yaml; cat /config/.http_port; echo; ls -l /config/state.db'

补充（可选、只读）：把运行容器里 `/app/app/webui.py` 与 `/app/tests/test_flow.py` 的 md5 打出来，并用旧 tag 离线起一个容器，把**第 5 步这段字节**在旧 tag 上原样跑一遍做对照（预期旧 tag：`has-_secure_eq: False`，且 `creds-cjk-ok` 处抛
`TypeError: comparing strings with non-ASCII characters is not supported`）。此条用于证明「跑的确实是新代码」，不做也不影响。

注意：不要删除、不要修改任何文件（第 1 步的 docker tag 只是加本地回滚标签）。第 5 步全程在内存里、不联网、不触碰 state.db。只回原始输出。
