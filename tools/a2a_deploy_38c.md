【补充验证 · 第 38 处真正判据的 old-vs-new 对照】逐条执行并原样回贴，前加 ##A~##C 标号：

说明：上一轮的 cell 锤的是 `p115.P115.cleanup_previous`（第 37 处的补丁位点），
两版输出相同属正常。第 38 处的判据在 `app/uploader.py::Uploader._same_episode_keeps`，
本 cell 直接锤它 —— **同一份字节在旧 tag 与新镜像上输出必然不同**。

##A 运行中容器：
docker exec upilot-mini sh -c 'cd /app && python -c "
import sys, os, tempfile, shutil, json
sys.path.insert(0,\"/app/app\")
import config as cfgmod, uploader, store as storemod

tmp = tempfile.mkdtemp()
cfg = json.loads(json.dumps(cfgmod.DEFAULTS))
cfg[\"state\"] = {\"db\": os.path.join(tmp, \"s.db\")}
cfg[\"paths\"][\"source\"] = os.path.join(tmp, \"src\")
cfg[\"paths\"][\"map\"] = [{\"from\": \"TVs\", \"to\": \"/TVs\"}]
cfg[\"upload\"][\"cleanup_previous_versions\"] = True
cfg[\"upload\"][\"delete_after_upload\"] = True
d = os.path.join(cfg[\"paths\"][\"source\"], \"TVs\", \"Drama\")
os.makedirs(d, exist_ok=True)

A = \"Show.S01E20.2160p.DV.mkv\"     # 本地刻意不存在
B = \"Show.S01E20.2160p.mkv\"        # 本地存在
pa, pb = os.path.join(d, A), os.path.join(d, B)
open(pb, \"wb\").write(b\"x\" * (5 * 1024 * 1024))

st = storemod.Store(cfg[\"state\"][\"db\"])
st.add(pa, 3 * 1024 * 1024, 1.0)
st.set(pa, status=\"已上传\", target_dir=\"/TVs/Drama\", file_id=\"fid-A\", source_deleted=1)
st.add(pb, 5 * 1024 * 1024, 2.0)
st.set(pb, status=\"已上传\", target_dir=\"/TVs/Drama\", file_id=\"fid-B\")

class Mgr:
    async def ensure_pid(self, p): return 700
    async def delete_file(self, f): return True

up = uploader.Uploader(cfg, st, Mgr())

f1, n1 = up._same_episode_keeps(\"/TVs/Drama\", pb)
print(\"CASE1 source_deleted=1 (被程序收尾删) -> protect-A:\", A in n1, \"| names=\", n1)

st.set(pa, source_deleted=0)
f2, n2 = up._same_episode_keeps(\"/TVs/Drama\", pb)
print(\"CASE2 source_deleted=0 (洗版换掉)     -> protect-A:\", A in n2, \"| names=\", n2)

print(\"EXPECT-NEW: CASE1=True CASE2=False\")
import inspect
src = inspect.getsource(uploader.Uploader._same_episode_keeps)
print(\"has-source_deleted-in-predicate:\", \"source_deleted\" in src)
shutil.rmtree(tmp, ignore_errors=True)
"'

##B 旧 tag 跑同一段（把容器换成离线镜像，脚本逐字节相同）：
docker run --rm --network none --entrypoint sh samir2013dzh/upilot-mini:pre-runbook13-20260921-130804 -c 'cd /app && python -c "<##A 里 python -c 后引号内的同一段原文>"'

##C 生产未被动过的旁证：
docker exec upilot-mini sh -c 'md5sum /config/config.yaml; cat /config/.http_port; echo; ls -l /config/state.db'
curl -s --noproxy '*' http://127.0.0.1:9844/health; echo
docker logs --since 5m upilot-mini 2>&1 | grep -ciE "traceback|no such column|异常" || echo 0

注意：不要删除、不要修改任何文件；本 cell 只在临时目录造内存/本地数据，不碰 state.db、不联网。只回原始输出。
