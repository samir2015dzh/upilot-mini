【部署 + 验证 · upilot-mini（第 41 处：云端同集同名重复清不掉）】逐条执行并原样回贴，每条前加 ##1~##6 标号：

1) 先给当前镜像留回滚名：
docker tag $(docker inspect -f '{{.Image}}' upilot-mini) samir2013dzh/upilot-mini:pre-runbook15-$(date +%Y%m%d-%H%M%S); echo "TAGGED"
docker compose -f /home/<user>/upilot-mini.yml pull

2) docker compose -f /home/<user>/upilot-mini.yml up -d --force-recreate

3) sleep 25 && docker ps --filter name=upilot-mini --format '{{.Names}} {{.Status}}' && docker inspect -f 'Image={{.Image}} RestartCount={{.RestartCount}} OOMKilled={{.State.OOMKilled}} StartedAt={{.State.StartedAt}}' upilot-mini

4) 符号计数（新代码是否在跑）：
docker exec upilot-mini sh -c 'grep -c "explicit_names" /app/app/p115.py; grep -c "hit_by_id" /app/app/p115.py; grep -c "same_name_others" /app/app/p115.py; grep -c "keep_names_set" /app/app/p115.py'

5) ★ 第 41 处的判据 cell（云端同集**逐字同名**的两条必须被清掉）：
docker exec -i upilot-mini python - <<'PYEOF'
import sys, asyncio
sys.path.insert(0, "/app/app")
import p115

FULL = "\u7532\u5267 (2026) [tmdbid=100001] S01E33 [2160p H265 DDP 5.1 WEB-DL HDR TX DemoWeb].mkv"
REN  = FULL[:-4] + " (1).mkv"
print("same-episode:", p115.episode_key(FULL) == p115.episode_key(REN), "| key =", p115.episode_key(FULL))
print("has-explicit_names:", "explicit_names" in p115.P115.cleanup_previous.__code__.co_varnames)
print("has-hit_by_id:", "hit_by_id" in p115.P115.cleanup_previous.__code__.co_varnames)

MB = 1024 * 1024

class Cloud:
    def __init__(self, rows): self.f = {fid: {"fid": fid, "n": n, "s": s, "ico": "mkv"} for fid, n, s in rows}
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
    # ① 用户真机场景：两条逐字同名、大小不同、标识命中新的
    c = Cloud([("111", FULL, 3*MB), ("222", FULL, 5*MB)])
    r = await mgr(c).cleanup_previous(700, FULL, 5*MB, 9, keep_fid="222", keep_fids=[], keep_names=[])
    print("CASE1 same-name-dup   :", "CLEANED" if sorted(c.f) == ["222"] else "STILL-DUP", "| deleted=", len(r["deleted"]))

    # ② 同名 + 大小完全相同（修复前会被 keep_size 挡）
    c2 = Cloud([("111", FULL, 5*MB), ("222", FULL, 5*MB)])
    r2 = await mgr(c2).cleanup_previous(700, FULL, 5*MB, 9, keep_fid="222", keep_fids=[], keep_names=[])
    print("CASE2 same-name-same-size:", "CLEANED" if sorted(c2.f) == ["222"] else "STILL-DUP", "| deleted=", len(r2["deleted"]))

    # ③ 115 自动改名：旧版名 = keep_name(完整)、新版名 = keep_name (1)
    c3 = Cloud([("111", FULL, 3*MB), ("222", REN, 5*MB)])
    r3 = await mgr(c3).cleanup_previous(700, FULL, 5*MB, 9, keep_fid="222", keep_fids=[], keep_names=[])
    print("CASE3 renamed-old    :", "CLEANED" if sorted(c3.f) == ["222"] else "STILL-DUP", "| deleted=", len(r3["deleted"]))

    # ④ 无标识：保守不动手，但 note 必须点明同名重复
    c4 = Cloud([("111", FULL, 3*MB), ("222", FULL, 5*MB)])
    r4 = await mgr(c4).cleanup_previous(700, FULL, 5*MB, 9, keep_fid="", keep_fids=[], keep_names=[])
    print("CASE4 no-id-guard    :", "KEPT-BOTH" if sorted(c4.f) == ["111", "222"] else "BAD",
          "| note-visible=", ("同名" in (r4.get("note") or "")))

    # ⑤ 洗版核心行为不变：不同名同集旧版照清
    OTHER = "\u7532\u5267 (2026) [tmdbid=100001] S01E33 [1080p WEB-DL].mkv"
    c5 = Cloud([("111", FULL, 3*MB), ("333", OTHER, 7*MB)])
    r5 = await mgr(c5).cleanup_previous(700, FULL, 3*MB, 9, keep_fid="111", keep_fids=[], keep_names=[])
    print("CASE5 old-ver-cleaned:", "CLEANED" if sorted(c5.f) == ["111"] else "BAD", "| deleted=", len(r5["deleted"]))

    # ⑥ 同集两个正当版本仍互不删（第 37/38 处的保护不能被弄坏）
    c6 = Cloud([("111", FULL, 3*MB), ("222", OTHER, 7*MB)])
    r6 = await mgr(c6).cleanup_previous(700, FULL, 3*MB, 9, keep_fid="111", keep_fids=["222"], keep_names=[OTHER])
    print("CASE6 both-kept      :", "BOTH-KEPT" if sorted(c6.f) == ["111", "222"] else "MUTUAL-DELETE")
    print("EXPECT-NEW: CASE1-CLEANED CASE2-CLEANED CASE3-CLEANED CASE4-KEPT-BOTH(note-visible=True) CASE5-CLEANED CASE6-BOTH-KEPT")
asyncio.run(main())
PYEOF

6) 正常接口 + 日志 + 文件指纹：
curl -s --noproxy '*' http://127.0.0.1:9844/health; echo
docker logs --tail 8 upilot-mini 2>&1
docker logs --since 10m upilot-mini 2>&1 | grep -ciE "traceback|异常|洗版清理出错" || echo 0
docker exec upilot-mini sh -c 'md5sum /config/config.yaml; cat /config/.http_port; echo; ls -l /config/state.db'

补充（可选、只读）：把运行容器里 `/app/app/p115.py` 的 md5 打出来，并用旧 tag（上一步打的那个 `pre-runbook15-*`）离线起一个容器，把**第 5 步这段字节**原样跑一遍做对照。预期旧 tag：`has-explicit_names: False`、CASE1/CASE2/CASE3 全部 `STILL-DUP`（这就是用户报障时生产上的行为）。此条用于证明「跑的确实是新代码」，不做也不影响。

注意：不要删除、不要修改任何文件（第 1 步的 docker tag 只是加本地回滚标签）。第 5 步全程在内存里、不联网、不触碰 state.db。只回原始输出。
