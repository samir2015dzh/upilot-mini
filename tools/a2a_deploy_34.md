【部署 + 验证 · upilot-mini（第 34 处：面板数值字段挡 inf/nan）】逐条执行并原样回贴，每条前加 ##1~##6 标号：

1) 先给当前镜像留回滚名：
docker tag $(docker inspect -f '{{.Image}}' upilot-mini) samir2013dzh/upilot-mini:pre-runbook9-$(date +%Y%m%d-%H%M%S); echo "TAGGED"
docker compose -f /home/<user>/upilot-mini.yml pull

2) docker compose -f /home/<user>/upilot-mini.yml up -d --force-recreate

3) sleep 25 && docker ps --filter name=upilot-mini --format '{{.Names}} {{.Status}}' && docker inspect -f 'Image={{.Image}} RestartCount={{.RestartCount}} OOMKilled={{.State.OOMKilled}} StartedAt={{.State.StartedAt}}' upilot-mini

4) docker exec upilot-mini sh -c 'grep -c "import math" /app/app/config.py; grep -c "math.isfinite" /app/app/config.py; grep -c "请填有限数字" /app/app/config.py; grep -c "OverflowError" /app/app/config.py'

5) 运行态自检（第 34 处的核心：inf/nan 必须被拒）：
docker exec upilot-mini sh -c 'cd /app && python -c "
import sys; sys.path.insert(0,\"/app/app\")
import config as c
f = c.find_field(\"scan.min_size_mb\")
for raw in (\"inf\", \"nan\", \"Infinity\", \"1e400\", \"-inf\"):
    try:
        v = c.coerce(f, raw)
        print(\"ACCEPTED\", raw, v)
    except ValueError as e:
        print(\"rejected\", raw)
    except Exception as e:
        print(\"BAD-EXC\", raw, type(e).__name__)
g = c.find_field(\"scan.interval_seconds\")
for raw in (\"inf\", \"1e400\"):
    try:
        print(\"ACCEPTED-int\", raw, c.coerce(g, raw))
    except ValueError:
        print(\"rejected-int\", raw)
    except Exception as e:
        print(\"BAD-EXC-int\", raw, type(e).__name__)
print(\"normal 0 ->\", c.coerce(f, \"0\"), \"| normal 30 ->\", c.coerce(g, \"30\"))
"'

6) 正常接口 + 日志：
curl -s --noproxy '*' http://127.0.0.1:9844/health; echo
docker logs --tail 6 upilot-mini 2>&1
docker logs --since 10m upilot-mini 2>&1 | grep -ciE "traceback|error|异常|失败" || echo 0

注意：不要删除、不要修改任何文件（第 1 步的 docker tag 只是加本地回滚标签）。只回原始输出。
