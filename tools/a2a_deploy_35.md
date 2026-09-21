【部署 + 验证 · upilot-mini（第 35 处：云端路径带 ..）】逐条执行并原样回贴，每条前加 ##1~##6 标号：

1) 先给当前镜像留回滚名：
docker tag $(docker inspect -f '{{.Image}}' upilot-mini) samir2013dzh/upilot-mini:pre-runbook10-$(date +%Y%m%d-%H%M%S); echo "TAGGED"
docker compose -f /home/<user>/upilot-mini.yml pull

2) docker compose -f /home/<user>/upilot-mini.yml up -d --force-recreate

3) sleep 25 && docker ps --filter name=upilot-mini --format '{{.Names}} {{.Status}}' && docker inspect -f 'Image={{.Image}} RestartCount={{.RestartCount}} OOMKilled={{.State.OOMKilled}} StartedAt={{.State.StartedAt}}' upilot-mini

4) docker exec upilot-mini sh -c 'grep -c "路径里不能出现" /app/app/config.py; grep -c "必须解析" /app/app/p115.py; grep -c "out.pop()" /app/app/p115.py'

5) 运行态自检（第 35 处的核心）：
docker exec upilot-mini sh -c 'cd /app && python -c "
import sys; sys.path.insert(0,\"/app/app\")
import config as c, p115
n = p115.normalize_cloud_path
for src, want in ((\"/a/../b\",\"/b\"), (\"/UPILOT-mini/..\",\"/\"), (\"/TVs/../../etc/Show\",\"/etc/Show\"), (\"/a.b/c..d\",\"/a.b/c..d\"), (\"/a/.../b\",\"/a/.../b\")):
    got = n(src)
    print(\"OK\" if got == want else \"MISMATCH\", repr(src), \"->\", repr(got), \"want\", repr(want))
f = c.find_field(\"paths.map\")
for raw in (\"TVs => /TVs/../../etc\", \"TVs => ..\", \"../TVs => /Safe\"):
    try:
        print(\"ACCEPTED\", raw, c.coerce(f, raw))
    except ValueError:
        print(\"rejected\", raw)
    except Exception as e:
        print(\"BAD-EXC\", raw, type(e).__name__)
print(\"normal map ->\", c.coerce(f, \"TVs => /TVs\"))
print(\"normal normalize ->\", n(\"/UPILOT-mini/TVs/甲剧 (2026) [tmdbid=100001]/Season 01\"))
"'

6) 正常接口 + 日志 + 配置指纹：
curl -s --noproxy '*' http://127.0.0.1:9844/health; echo
docker logs --tail 6 upilot-mini 2>&1
docker logs --since 10m upilot-mini 2>&1 | grep -ciE "traceback|error|异常|失败" || echo 0
docker exec upilot-mini sh -c 'md5sum /config/config.yaml; ls -l /config/config.yaml /config/config.yaml.bak; cat /config/.http_port'

注意：不要删除、不要修改任何文件（第 1 步的 docker tag 只是加本地回滚标签）。只回原始输出。
