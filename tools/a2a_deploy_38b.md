【补充验证 · upilot-mini 第 38 处（只读、不碰任何生产文件）】逐条执行并原样回贴，前加 ##A~##C 标号：

背景：上一轮部署验证的 cell 只覆盖了 store 层的 source_deleted 读写与老库补列，**没有触达**
`p115.P115.cleanup_previous()` 那条删除分支。这次补上它，全程在内存里造「云端目录」，
不联网、不写任何文件、不碰 state.db。

##A 同集两版本「都不该被删」（delete_after_upload 等价态）：
docker exec upilot-mini sh -c 'cd /app && python -c "
import sys, asyncio; sys.path.insert(0,\"/app/app\")
import p115
A=\"Show.S01E09.2160p.DV.mkv\"; B=\"Show.S01E09.2160p.mkv\"
print(\"same-episode:\", p115.episode_key(A)==p115.episode_key(B))

class Cloud:
    def __init__(self): self.f={}
    def add(self, fid, n, s): self.f[fid]={\"fid\":fid,\"n\":n,\"s\":s,\"ico\":\"mkv\"}
    def snap(self): return [dict(v) for v in self.f.values()]

def mgr(c):
    m = p115.P115.__new__(p115.P115)
    m._dir_cache={}; m._dir_cache_ttl=30.0; m._dir_cache_max=64
    m._delete_lock = asyncio.Lock()
    async def list_dir(pid, use_cache=True):
        rows=c.snap(); m._remember_dir(int(pid), rows); return rows
    async def delete_file(fid):
        async with m._delete_lock: return bool(c.f.pop(str(fid), None))
    m.list_dir=list_dir; m.delete_file=delete_file
    return m

async def main():
    # A 3MB / B 5MB（大小必须不同，否则被 keep_size 那道挡住）
    c=Cloud(); c.add(\"111\", A, 3*1024*1024); c.add(\"222\", B, 5*1024*1024)
    r=await mgr(c).cleanup_previous(700, A, 3*1024*1024, 9, keep_fid=\"111\", keep_fids=[\"222\"], keep_names=[B])
    left=sorted(x[\"n\"] for x in c.snap())
    print(\"BOTH-KEPT\" if (r[\"deleted\"]==[] and left==sorted([A,B])) else \"MUTUAL-DELETE\", r[\"deleted\"], left)

    # 只按名字保护
    c2=Cloud(); c2.add(\"111\", A, 3*1024*1024); c2.add(\"222\", B, 5*1024*1024)
    r2=await mgr(c2).cleanup_previous(700, A, 3*1024*1024, 9, keep_fid=\"111\", keep_names=[B])
    print(\"NAME-ONLY-KEPT\" if r2[\"deleted\"]==[] else \"NAME-ONLY-FAIL\", r2[\"deleted\"])

    # 洗版核心行为不变：非本地的同集旧版仍必须被清
    c3=Cloud(); c3.add(\"111\", A, 3*1024*1024); c3.add(\"333\", \"Show.S01E09.1080p.mkv\", 7*1024*1024)
    r3=await mgr(c3).cleanup_previous(700, A, 3*1024*1024, 9, keep_fid=\"111\")
    left3=sorted(x[\"n\"] for x in c3.snap())
    print(\"OLD-CLEANED\" if (r3[\"deleted\"]==[\"Show.S01E09.1080p.mkv\"] and left3==[A]) else \"OLD-FAIL\", r3[\"deleted\"], left3)

    # 超上限整体跳过
    c4=Cloud(); c4.add(\"111\", A, 3*1024*1024)
    for i in range(4): c4.add(str(400+i), \"Show.S01E09.%dp.mkv\"%(480+i), 1*1024*1024+i)
    r4=await mgr(c4).cleanup_previous(700, A, 3*1024*1024, 3, keep_fid=\"111\")
    print(\"OVER-LIMIT-SKIP\" if r4[\"deleted\"]==[] else \"OVER-LIMIT-FAIL\", r4[\"deleted\"], r4.get(\"note\"))
asyncio.run(main())
import inspect
print(\"sig-params:\", list(inspect.signature(p115.P115.cleanup_previous).parameters))
"'

##B 旧 tag 跑同一段（对照）：把 `upilot-mini` 换成
`docker run --rm --network none --entrypoint sh samir2013dzh/upilot-mini:pre-runbook13-20260921-130804 -c '<上面同一段>'`
（若该 tag 名与本机不同，请用实际存在的、指向 e51765aaf55d 的那个 tag。）

##C 生产未被动过的旁证：
docker exec upilot-mini sh -c 'md5sum /config/config.yaml; cat /config/.http_port; ls -l /config/state.db'
docker ps --format '{{.Names}} {{.Status}}' | sort
curl -s --noproxy '*' http://127.0.0.1:9844/health; echo

注意：不要删除、不要修改任何文件；上面只跑内存里的 stub 云盘。只回原始输出。
