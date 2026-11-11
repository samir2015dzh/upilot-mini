【部署 + 验证 · upilot-mini（第 37 处：同集两版本不互删）】逐条执行并原样回贴，每条前加 ##1~##6 标号：

1) 先给当前镜像留回滚名：
docker tag $(docker inspect -f '{{.Image}}' upilot-mini) samir2013dzh/upilot-mini:pre-runbook12-$(date +%Y%m%d-%H%M%S); echo "TAGGED"
docker compose -f /home/<user>/upilot-mini.yml pull

2) docker compose -f /home/<user>/upilot-mini.yml up -d --force-recreate

3) sleep 25 && docker ps --filter name=upilot-mini --format '{{.Names}} {{.Status}}' && docker inspect -f 'Image={{.Image}} RestartCount={{.RestartCount}} OOMKilled={{.State.OOMKilled}} StartedAt={{.State.StartedAt}}' upilot-mini

4) docker exec upilot-mini sh -c 'grep -c "keep_fids" /app/app/p115.py; grep -c "_same_episode_keeps" /app/app/uploader.py; grep -c "keep_names_set" /app/app/p115.py; grep -c "episode_key" /app/app/uploader.py'

5) 运行态自检（第 37 处的核心：同集两版本互认、洗版旧版仍被清）：
docker exec upilot-mini sh -c 'cd /app && python -c "
import sys, asyncio; sys.path.insert(0,\"/app/app\")
import p115
A = \"Show.S01E01.2160p.DV.mkv\"; B = \"Show.S01E01.2160p.mkv\"
print(\"same episode:\", p115.episode_key(A) == p115.episode_key(B))

class Cloud:
    def __init__(self): self.f = {}
    def add(self, fid, n): self.f[fid] = {\"fid\": fid, \"n\": n, \"s\": 1000, \"ico\": \"mkv\"}
    def snap(self): return [dict(v) for v in self.f.values()]

def mgr(c):
    m = p115.P115.__new__(p115.P115)
    m._dir_cache = {}; m._dir_cache_ttl = 30.0; m._dir_cache_max = 64
    m._delete_lock = asyncio.Lock()
    async def list_dir(pid, use_cache=True):
        rows = c.snap(); m._remember_dir(int(pid), rows); return rows
    async def delete_file(fid):
        async with m._delete_lock: return bool(c.f.pop(str(fid), None))
    m.list_dir = list_dir; m.delete_file = delete_file
    return m

async def main():
    c = Cloud(); c.add(\"111\", A); c.add(\"222\", B)
    r = await mgr(c).cleanup_previous(700, A, 0, 9, keep_fid=\"111\", keep_fids=[\"222\"], keep_names=[B])
    left = sorted(x[\"n\"] for x in c.snap())
    print(\"BOTH-KEPT\" if (r[\"deleted\"] == [] and left == sorted([A, B])) else \"MUTUAL-DELETE\", r[\"deleted\"], left)

    c2 = Cloud(); c2.add(\"111\", A); c2.add(\"333\", \"Show.S01E01.1080p.mkv\")
    r2 = await mgr(c2).cleanup_previous(700, A, 0, 9, keep_fid=\"111\")
    left2 = sorted(x[\"n\"] for x in c2.snap())
    print(\"OLD-CLEANED\" if r2[\"deleted\"] == [\"Show.S01E01.1080p.mkv\"] and left2 == [A] else \"BAD\", r2[\"deleted\"], left2)
asyncio.run(main())
import inspect
print(\"sig has keep_fids:\", \"keep_fids\" in inspect.signature(p115.P115.cleanup_previous).parameters)
"'

6) 正常接口 + 日志 + 配置指纹：
curl -s --noproxy '*' http://127.0.0.1:9844/health; echo
docker logs --tail 6 upilot-mini 2>&1
docker logs --since 10m upilot-mini 2>&1 | grep -ciE "traceback|error|异常|失败" || echo 0
docker exec upilot-mini sh -c 'md5sum /config/config.yaml; ls -l /config/config.yaml; cat /config/.http_port'

注意：不要删除、不要修改任何文件（第 1 步的 docker tag 只是加本地回滚标签）。只回原始输出。
