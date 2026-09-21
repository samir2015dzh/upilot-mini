【部署 + 验证 · upilot-mini（第 36 处：列目录翻页游标）】逐条执行并原样回贴，每条前加 ##1~##6 标号：

1) 先给当前镜像留回滚名：
docker tag $(docker inspect -f '{{.Image}}' upilot-mini) samir2013dzh/upilot-mini:pre-runbook11-$(date +%Y%m%d-%H%M%S); echo "TAGGED"
docker compose -f /home/<user>/upilot-mini.yml pull

2) docker compose -f /home/<user>/upilot-mini.yml up -d --force-recreate

3) sleep 25 && docker ps --filter name=upilot-mini --format '{{.Names}} {{.Status}}' && docker inspect -f 'Image={{.Image}} RestartCount={{.RestartCount}} OOMKilled={{.State.OOMKilled}} StartedAt={{.State.StartedAt}}' upilot-mini

4) docker exec upilot-mini sh -c 'grep -c "MAX_DIR_ITEMS" /app/app/p115.py; grep -c "offset += len(rows)" /app/app/p115.py; grep -c "反复返回同一批" /app/app/p115.py; grep -c "seen_ids" /app/app/p115.py'

5) 运行态自检（第 36 处的核心：模拟「服务端单页比请求小」，必须一条不漏）：
docker exec upilot-mini sh -c 'cd /app && python -c "
import sys, asyncio; sys.path.insert(0,\"/app/app\")
import p115

class Fake:
    def __init__(self, n, cap):
        self.items = [{\"fid\": str(1000000+i), \"n\": \"f%05d.mkv\" % i} for i in range(n)]
        self.cap = cap; self.calls = []
    async def fs_files(self, payload, async_=False):
        off = int(payload[\"offset\"]); lim = int(payload[\"limit\"])
        self.calls.append((off, lim))
        take = min(lim, self.cap)
        return {\"state\": True, \"count\": len(self.items),
                \"data\": {\"count\": len(self.items), \"list\": self.items[off:off+take]}}

async def probe(n, cap):
    c = Fake(n, cap)
    m = p115.P115.__new__(p115.P115)
    m.client = c; m._dir_cache = {}; m._dir_cache_ttl = 30.0; m._dir_cache_max = 64
    rows = await m.list_dir(700, use_cache=False)
    print(\"n=%d cap=%d -> got %d  calls %s  %s\" % (n, cap, len(rows), c.calls[:4], \"OK\" if len(rows)==n else \"MISSING\"))

async def main():
    for n, cap in ((300,100),(2500,100),(2500,500),(3000,7),(300,1150),(2500,1150),(1150,1150),(1151,1150)):
        await probe(n, cap)
asyncio.run(main())
print(\"MAX_DIR_ITEMS =\", p115.MAX_DIR_ITEMS)
"'

6) 正常接口 + 日志 + 配置指纹：
curl -s --noproxy '*' http://127.0.0.1:9844/health; echo
docker logs --tail 6 upilot-mini 2>&1
docker logs --since 10m upilot-mini 2>&1 | grep -ciE "traceback|error|异常|失败" || echo 0
docker exec upilot-mini sh -c 'md5sum /config/config.yaml; ls -l /config/config.yaml; cat /config/.http_port'

注意：不要删除、不要修改任何文件（第 1 步的 docker tag 只是加本地回滚标签）。只回原始输出。
