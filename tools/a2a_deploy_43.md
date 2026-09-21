【部署 + 真删实测 · upilot-mini（第 43 处：同集只留 115 云端时间最新那条）】
逐条执行并原样回贴，前加 ##1~##8 标号。

背景（用户明确诉求）：「上传到115以后校验115的那个文件夹里面有没有相同集数的视频……
现在115里面有两个第33集的甲剧，一个是hdr的版本（后秒传上去的），一个普通4k版本
（先秒传上去的），我需要的是后秒传上去这个hdr的版本后，把先秒传上去的普通4k版本删掉」；
「你只能在 115 云端去核对，本地肯定是没有的，因为本地的文件在秒传完已经被删掉了」。

本次改动：判据从「看本地文件」换成「只看 115 云端条目的时间字段（te/tp/tu epoch 秒）」，
并新增同集巡检 + 面板「核对同集」按钮。

1) 先给当前镜像留回滚名：
docker tag $(docker inspect -f '{{.Image}}' upilot-mini) samir2013dzh/upilot-mini:pre-runbook17-$(date +%Y%m%d-%H%M%S); echo "TAGGED"
docker compose -f /home/<user>/upilot-mini.yml pull

2) docker compose -f /home/<user>/upilot-mini.yml up -d --force-recreate

3) sleep 25 && docker ps --filter name=upilot-mini --format '{{.Names}} {{.Status}}' && docker inspect -f 'Image={{.Image}} RestartCount={{.RestartCount}} OOMKilled={{.State.OOMKilled}} StartedAt={{.State.StartedAt}}' upilot-mini

4) 符号计数（新代码是否在跑）：
docker exec upilot-mini sh -c 'grep -c "cleanup_episode_newest" /app/app/p115.py; grep -c "cloud_ts" /app/app/p115.py; grep -c "sweep_cloud_episode_duplicates" /app/app/uploader.py; grep -c "sweep_now" /app/app/webui.py; grep -c "btn-sweep" /app/app/static/index.html; grep -c "MAX_SWEEP_PAGES" /app/app/uploader.py; grep -c "import datetime" /app/app/p115.py'

5) ★ 先把这三段 cell **写成文件**（上次 heredoc 管道被工具环境提前关闭 stdin，
   报 `RuntimeError: source code string cannot contain null bytes`；写文件再跑更稳）：
cat > /root/.hermes/cache/scratch/cell43.py <<'PYEOF'
import sys, asyncio, os
sys.path.insert(0, "/app/app")
import config as cfgmod, p115
from store import Store

cfgmod.ensure_config()
cfg = cfgmod.load()
st = Store(cfg["state"]["db"])
mgr = p115.P115(cfg, st)

print("has-cloud_ts      :", hasattr(p115.P115, "cloud_ts"))
print("has-newest-cleanup:", hasattr(p115.P115, "cleanup_episode_newest"))

async def main():
    rows, _ = st.list_tasks(limit=2000)
    tdirs = []
    for r in rows or []:
        td = str(r.get("target_dir") or "").strip()
        if td and td not in tdirs:
            tdirs.append(td)
    root = str((cfg.get("paths") or {}).get("target_root") or "").strip()
    if root and root not in tdirs:
        tdirs.append(root)
    for td in tdirs:
        try:
            pid = await mgr.ensure_pid(td)
            cloud = await mgr.list_dir(pid, use_cache=False)
        except Exception as exc:
            print("列目录失败", td, type(exc).__name__, exc)
            continue
        groups = {}
        for c in cloud:
            k = p115.episode_key(str(c.get("n") or c.get("name") or ""))
            if k:
                groups.setdefault(k, []).append(c)
        dup = {k: v for k, v in groups.items() if len(v) > 1}
        print("")
        print("=== ", td, "| 文件", len(cloud), "条 | 同集多条的组:", len(dup))
        for k, v in sorted(dup.items()):
            print("   ", k, "→", len(v), "条")
            for r in sorted(v, key=lambda x: p115.P115.cloud_ts(x), reverse=True):
                print("      te=", p115.P115.cloud_ts(r), " fid=", r.get("fid"), " pc=", r.get("pc"),
                      " size=", r.get("s"), "", str(r.get("n"))[:96])
asyncio.run(main())
PYEOF
docker exec -i upilot-mini python - < /root/.hermes/cache/scratch/cell43.py

（此步只列目录、只算，不删任何东西。）

6) ★★ 真删实测（**破坏性，但正是用户明确要的效果**：同集旧版移入 115 回收站，可恢复）。
   只有第 5 步确实列出了「同集多条」时才执行：
cat > /root/.hermes/cache/scratch/cell43_del.py <<'PYEOF'
import sys, asyncio, os
sys.path.insert(0, "/app/app")
import config as cfgmod, p115
from store import Store

cfgmod.ensure_config()
cfg = cfgmod.load()
st = Store(cfg["state"]["db"])
mgr = p115.P115(cfg, st)

async def main():
    rows, _ = st.list_tasks(limit=2000)
    tdirs = []
    for r in rows or []:
        td = str(r.get("target_dir") or "").strip()
        if td and td not in tdirs:
            tdirs.append(td)
    root = str((cfg.get("paths") or {}).get("target_root") or "").strip()
    if root and root not in tdirs:
        tdirs.append(root)
    total = 0
    for td in tdirs:
        try:
            pid = await mgr.ensure_pid(td)
            cloud = await mgr.list_dir(pid, use_cache=False)
        except Exception as exc:
            print("列出失败", td, exc)
            continue
        groups = {}
        for c in cloud:
            k = p115.episode_key(str(c.get("n") or c.get("name") or ""))
            if k:
                groups.setdefault(k, []).append(c)
        for k, v in sorted(groups.items()):
            if len(v) < 2:
                continue
            keep = max(v, key=lambda x: p115.P115.cloud_ts(x))
            res = await mgr.cleanup_episode_newest(pid, str(keep.get("n")), 3,
                                                  keep_fid=str(keep.get("pc") or keep.get("fid") or ""))
            print("同集", k, "| 保留 te=", p115.P115.cloud_ts(keep), str(keep.get("n"))[:70])
            print("   deleted =", res.get("deleted"))
            print("   note    =", res.get("note"))
            total += len(res.get("deleted") or [])
            after = await mgr.list_dir(pid, use_cache=False)
            left = [str(r.get("n")) for r in after
                    if p115.episode_key(str(r.get("n"))) == k]
            print("   清理后该集剩余", len(left), "条：")
            for n in left:
                print("      ✔", n[:96])
    print("")
    print("TOTAL-REMOVED =", total)
asyncio.run(main())
PYEOF
docker exec -i upilot-mini python - < /root/.hermes/cache/scratch/cell43_del.py

7) 巡检 + 面板入口（不删东西也要能跑通）。同样先写文件再跑：
cat > /root/.hermes/cache/scratch/cell43_sweep.py <<'PYEOF'
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
    print("（幂等：刚清理过，这里应该是 removed=0）")
asyncio.run(main())
PYEOF
docker exec -i upilot-mini python - < /root/.hermes/cache/scratch/cell43_sweep.py
curl -s --noproxy '*' -o /dev/null -w 'sweep-http=%{http_code}\n' -X POST http://127.0.0.1:9844/api/sweep
docker exec upilot-mini sh -c 'grep -c "核对同集" /app/app/static/index.html'
docker exec upilot-mini sh -c 'cat /config/.http_port; echo; md5sum /config/config.yaml'

8) 日志 + 回收站确认：
docker logs --since 30m upilot-mini 2>&1 | grep -iE "同集|回收站|清理|洗板" | tail -25
docker logs --since 30m upilot-mini 2>&1 | grep -ciE "traceback|异常|NameError" || echo 0
curl -s --noproxy '*' http://127.0.0.1:9844/health; echo

执行完请**去 115 回收站**看一下那几条被删的文件名（确认可恢复），把名称回贴。

注意：不要删除、不要修改其它任何文件（第 1 步只是加本地回滚标签）。
第 5 步只读；第 6 步是用户明确要求的效果（同集旧版移入 115 回收站，可恢复）。
只回原始输出。
