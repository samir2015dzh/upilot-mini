【部署 + 验证 · upilot-mini（修巡检链路：面板「核对同集」按钮）】逐条执行并原样回贴，前加 ##1~##5 标号。

背景：上一轮部署后，**上传后自动清理已真删成功**（普通4K版已进 115 回收站，HDR 留下）。
但同一次回贴抓到巡检链路的一个 bug：`'Uploader' object has no attribute 'client'`（3 条 warning），
导致 **5 分钟自动巡检 + 面板「核对同集」按钮** 恒为 `removed=0`（列表失败被 except 降级成 warning）。
本次修：巡检列目录改走 `self.mgr.client`；列目录失败返回 `None`、不再把失败目录计进「已扫目录数」。

1) 留回滚名 + 拉新镜像 + 重建：
docker tag $(docker inspect -f '{{.Image}}' upilot-mini) samir2013dzh/upilot-mini:pre-runbook18-$(date +%Y%m%d-%H%M%S); echo TAGGED
docker compose -f /home/<user>/upilot-mini.yml pull
docker compose -f /home/<user>/upilot-mini.yml up -d --force-recreate
sleep 25
docker ps --filter name=upilot-mini --format '{{.Names}} {{.Status}}'
docker inspect -f 'Image={{.Image}} RestartCount={{.RestartCount}} OOMKilled={{.State.OOMKilled}} StartedAt={{.State.StartedAt}}' upilot-mini

2) 符号计数（确认新代码在跑）：
docker exec upilot-mini sh -c 'grep -c "self.mgr.client" /app/app/uploader.py; grep -c "await self.client." /app/app/uploader.py; grep -c "失败 ≠ 空目录" /app/app/uploader.py'

（期望：第一项 ≥1；第二项 **0**；第三项 1）

3) ★ 巡检实跑（关键是**不再有** attribute 报错，且 `dirs` 计数真实）：
cat > /root/.hermes/cache/scratch/cell44.py <<'PYEOF'
import sys, asyncio
sys.path.insert(0, "/app/app")
import config as cfgmod, p115
from store import Store
from uploader import Uploader

cfgmod.ensure_config()
cfg = cfgmod.load()
st = Store(cfg["state"]["db"])
mgr = p115.P115(cfg, st)
up = Uploader(cfg, st, mgr)

async def main():
    r = await up.sweep_cloud_episode_duplicates()
    print("sweep 结果 =", r)
    print("（E33 已在上一轮清理完毕，故 removed 应为 0；dirs 应为真实扫成功数）")
asyncio.run(main())
PYEOF
docker exec -i upilot-mini python - < /root/.hermes/cache/scratch/cell44.py

4) 面板入口 + 云端复核（只读）：
curl -s --noproxy '*' -o /dev/null -w 'sweep-http(no-auth)=%{http_code}\n' -X POST http://127.0.0.1:9844/api/sweep
docker exec upilot-mini sh -c 'grep -c "核对同集" /app/app/static/index.html'
cat > /root/.hermes/cache/scratch/cell44_check.py <<'PYEOF'
import sys, asyncio
sys.path.insert(0, "/app/app")
import config as cfgmod, p115
from store import Store
cfgmod.ensure_config()
cfg = cfgmod.load()
st = Store(cfg["state"]["db"])
mgr = p115.P115(cfg, st)
async def main():
    pid = await mgr.ensure_pid("/TVs/甲剧 (2026) [tmdbid=100001]/Season 01")
    rows = await mgr.list_dir(pid, use_cache=False)
    same = [r for r in rows if p115.episode_key(str(r.get("n"))) == "甲剧 (2026)|s01e033|.mkv"]
    print("E33 云端剩余 =", len(same), "条")
    for r in same:
        print("   te=", p115.P115.cloud_ts(r), "size=", r.get("s"), str(r.get("n"))[:90])
    print("（期望只剩 HDR 那条 te=1789989329 size=5187057457）")
asyncio.run(main())
PYEOF
docker exec -i upilot-mini python - < /root/.hermes/cache/scratch/cell44_check.py

5) 日志 + 指纹：
docker logs --since 10m upilot-mini 2>&1 | grep -iE "同集巡检|has no attribute|同集清理" | tail -15
docker logs --since 10m upilot-mini 2>&1 | grep -ciE "traceback|NameError|has no attribute" || echo 0
curl -s --noproxy '*' http://127.0.0.1:9844/health; echo
docker exec upilot-mini sh -c 'md5sum /config/config.yaml; cat /config/.http_port; echo'

注意：不要删除、不要修改任何其它文件；第 1 步只加本地回滚标签。只回原始输出。
