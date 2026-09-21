【部署 + 验证 · upilot-mini（第 32~33 处：请求体上限/半开连接/断开异常）】逐条执行并原样回贴，每条前加 ##1~##7 标号：

1) 先给当前镜像留个回滚名：
docker tag $(docker inspect -f '{{.Image}}' upilot-mini) samir2013dzh/upilot-mini:pre-runbook8-$(date +%Y%m%d-%H%M%S) 2>&1 | tail -1; echo "TAGGED"
docker compose -f /home/<user>/upilot-mini.yml pull

2) docker compose -f /home/<user>/upilot-mini.yml up -d --force-recreate

3) sleep 25 && docker ps --filter name=upilot-mini --format '{{.Names}} {{.Status}}' && docker inspect -f 'Image={{.Image}} RestartCount={{.RestartCount}} OOMKilled={{.State.OOMKilled}} StartedAt={{.State.StartedAt}}' upilot-mini

4) docker exec upilot-mini sh -c 'cat /config/.http_port; echo; ls -l /config/config.yaml /config/config.yaml.bak; md5sum /config/config.yaml'

5) 确认第 32~33 处的代码真的在运行镜像里（逐字回贴）：
docker exec upilot-mini sh -c 'grep -c "MAX_BODY_BYTES" /app/app/webui.py; grep -c "HANDLER_TIMEOUT" /app/app/webui.py; grep -c "ConnectionAbortedError" /app/app/webui.py; grep -c "close_connection = True" /app/app/webui.py; grep -c "is not None" /app/app/webui.py; grep -c "_wake(" /app/app/webui.py'

6) 日志：
docker logs --tail 12 upilot-mini 2>&1
docker logs --since 10m upilot-mini 2>&1 | grep -ciE "traceback|error|异常|失败" || echo 0

7) 运行态自检（不打印任何密码）：
curl -s --noproxy '*' http://127.0.0.1:9844/health; echo
docker exec upilot-mini sh -c 'cd /app && python -c "
import sys; sys.path.insert(0,\"/app/app\")
import webui
print(\"MAX_BODY_BYTES:\", webui.MAX_BODY_BYTES)
print(\"HANDLER_TIMEOUT:\", webui.HANDLER_TIMEOUT)
print(\"has _wake:\", hasattr(webui, \"_wake\"))
"'
echo "--- 超限请求应当立刻被拒（不阻塞）---"
docker exec upilot-mini sh -c 'python - <<PY
import socket, time
s = socket.create_connection((\"127.0.0.1\", 9844), timeout=8)
s.sendall(b\"POST /login HTTP/1.1\r\nHost: x\r\nContent-Length: 104857600\r\nContent-Type: application/x-www-form-urlencoded\r\n\r\nabc\")
s.settimeout(6)
t0 = time.monotonic()
try:
    d = s.recv(200).split(b\"\r\n\")[0].decode(\"latin1\")
    print(\"resp:\", d, \"spent: %.2fs\" % (time.monotonic()-t0))
except socket.timeout:
    print(\"TIMEOUT (still blocking)\")
s.close()
PY'

注意：不要删除、不要修改任何文件（第 1 步的 docker tag 只是加一个本地标签，属必要回滚准备，可以执行）。只回原始输出。
