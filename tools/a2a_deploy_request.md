# A2A 部署 + 验证请求模板（upilot-mini）

用法：把本文件内容作为请求正文发给远程 agent。

```bash
cd <仓库目录>
python tools/a2a_send.py <本文件> 
```

约定：远程只做**只读探针 + 重建容器**，不删除、不修改任何文件；逐条执行并原样回贴输出。

---

【部署 + 验证 · upilot-mini】逐条执行并原样回贴输出，每条前加 ##1~##8 标号：

1) docker compose -f /home/<user>/upilot-mini.yml pull

2) docker compose -f /home/<user>/upilot-mini.yml up -d --force-recreate

3) sleep 25 && docker ps --filter name=upilot-mini --format '{{.Names}} {{.Status}} {{.Image}}' && docker inspect -f 'Image={{.Image}} RestartCount={{.RestartCount}} OOMKilled={{.State.OOMKilled}}' upilot-mini

4) docker exec upilot-mini sh -c 'cat /config/.http_port; echo; ls -l /config/config.yaml /config/config.yaml.bak; md5sum /config/config.yaml'

5) 确认新代码真的在镜像里（逐字回贴数字）：
docker exec upilot-mini sh -c 'grep -c "_atomic_write_text" /app/app/config.py; grep -c "_SAVE_LOCK" /app/app/config.py; grep -c "def _wake" /app/app/webui.py; grep -c "_wake(state)" /app/app/webui.py; grep -c "_wait_next_round" /app/app/main.py; grep -c "_SHARED" /app/app/uploader.py; grep -c "PORT_FILE" /app/app/healthcheck.py; grep -c "is not None" /app/app/webui.py'

6) 最近 25 行日志 + 近 10 分钟错误计数：
docker logs --tail 25 upilot-mini 2>&1
docker logs --since 10m upilot-mini 2>&1 | grep -ciE "traceback|exception|error" || echo 0

7) 健康与并发闸门（只读，不改任何文件）：
curl -s --noproxy '*' http://127.0.0.1:9844/health; echo
docker inspect -f 'Health={{.State.Health.Status}} FailingStreak={{index .State.Health "FailingStreak"}}' upilot-mini

8) 运行态自检（只读；不要删除、不要修改任何文件）：
docker exec upilot-mini sh -c 'cd /app && python -c "
import sys; sys.path.insert(0, \"/app/app\")
import config, webui, main as m
print(\"has _wake:\", hasattr(webui, \"_wake\"))
print(\"has _wait_next_round:\", hasattr(m, \"_wait_next_round\"))
print(\"PORT_FILE:\", config.PORT_FILE)
print(\"save lock:\", type(config._SAVE_LOCK).__name__)
import copy
c = copy.deepcopy(config.DEFAULTS)
c[\"http\"][\"auth\"] = {\"enabled\": True, \"password\": \"probe-pw\", \"username\": \"\", \"mode\": \"form\"}
tok = webui.make_session(\"user\", \"probe-pw\", 1)
print(\"empty-user session check_auth:\", webui.check_auth(c, {\"Cookie\": \"upilot_session=\" + tok}, \"\"))
bad = webui.make_session(\"user\", \"wrong-pw\", 1)
print(\"wrong-secret session check_auth:\", webui.check_auth(c, {\"Cookie\": \"upilot_session=\" + bad}, \"\"))
print(\"trust_forwarded_ip default:\", config.DEFAULTS[\"http\"][\"auth\"][\"trust_forwarded_ip\"])
"'
（说明：`check_auth` 返回 True=放行、False=拒绝。故正确会话应为 True，错密钥会话应为 False。）

注意：不要删除、不要修改任何文件；只回贴原始输出。

---

## 读结果时的三个坑（踩过）

- `grep -ci` 命中 0 时自身退出码非 0，`|| echo 0` 会再打一行，看起来像"两个 0"，属正常。
- 探针里 `print` 的标签要写清语义（`check_auth: True/False` 谁是放行），否则 False 会被误读成"拒绝失败"。
- pull 前先给当前 `:latest` 打一个回归 tag：旧镜像若只剩 `:latest` 一个本地名，pull 会让它变 `<none>`，回滚引用就消失了。
  例：`docker tag <旧镜像ID> samir2013dzh/upilot-mini:pre-runbook-$(date +%Y%m%d-%H%M%S)`
