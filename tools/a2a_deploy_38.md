【部署 + 验证 · upilot-mini（第 38 处：source_deleted 落库）】逐条执行并原样回贴，每条前加 ##1~##7 标号：

1) 先给当前镜像留回滚名：
docker tag $(docker inspect -f '{{.Image}}' upilot-mini) samir2013dzh/upilot-mini:pre-runbook13-$(date +%Y%m%d-%H%M%S); echo "TAGGED"
docker compose -f /home/<user>/upilot-mini.yml pull

2) docker compose -f /home/<user>/upilot-mini.yml up -d --force-recreate

3) sleep 25 && docker ps --filter name=upilot-mini --format '{{.Names}} {{.Status}}' && docker inspect -f 'Image={{.Image}} RestartCount={{.RestartCount}} OOMKilled={{.State.OOMKilled}} StartedAt={{.State.StartedAt}}' upilot-mini

4) 符号计数（新代码是否在跑）：
docker exec upilot-mini sh -c 'grep -c "source_deleted" /app/app/store.py; grep -c "source_deleted" /app/app/uploader.py; grep -c "keep_fids" /app/app/p115.py'

5) ★ 最关键：老库补列是否成功（真机 state.db 是旧结构，必须自动补上 source_deleted 列）：
docker exec upilot-mini sh -c 'cd /app && python -c "
import sqlite3, os, sys
sys.path.insert(0, \"/app/app\")
db = os.environ.get(\"STATE_DB\") or \"/config/state.db\"
import config as cfgmod
try:
    cfgmod.ensure_config()
    db = cfgmod.load()[\"state\"][\"db\"]
except Exception as e:
    print(\"config 读取失败，用默认路径:\", e)
print(\"db =\", db)
c = sqlite3.connect(db)
cols = [r[1] for r in c.execute(\"PRAGMA table_info(files)\").fetchall()]
print(\"HAS-COL\" if \"source_deleted\" in cols else \"MISSING-COL\", \"source_deleted\")
print(\"rows =\", c.execute(\"SELECT COUNT(*) FROM files\").fetchone()[0])
print(\"nonzero =\", c.execute(\"SELECT COUNT(*) FROM files WHERE source_deleted<>0\").fetchone()[0])
print(\"uploaded =\", c.execute(\"SELECT COUNT(*) FROM files WHERE status=?\", (\"已上传\",)).fetchone()[0])
c.close()
"'

6) 运行态自检（第 38 处的判据：同集两版本在 delete_after_upload 开着时也不互删）：
docker exec upilot-mini sh -c 'cd /app && python -c "
import sys, asyncio, os, tempfile, sqlite3
sys.path.insert(0, \"/app/app\")
import store as storemod
print(\"DDL-has-col:\", \"source_deleted\" in storemod.DDL)

tmp = tempfile.mkdtemp()
db = os.path.join(tmp, \"t.db\")
st = storemod.Store(db)
st.add(\"/x/A.mkv\", 100, 1.0); st.set(\"/x/A.mkv\", status=\"已上传\", source_deleted=1)
st.add(\"/x/B.mkv\", 100, 1.0); st.set(\"/x/B.mkv\", status=\"已上传\")
print(\"A.source_deleted =\", st.get(\"/x/A.mkv\").get(\"source_deleted\"))
print(\"B.source_deleted =\", st.get(\"/x/B.mkv\").get(\"source_deleted\"))

# 老库补列（旧 DDL 无该列 → _migrate 必须自动补）
db2 = os.path.join(tmp, \"old.db\")
c = sqlite3.connect(db2)
c.executescript(\"CREATE TABLE files(path TEXT PRIMARY KEY, size INTEGER NOT NULL, mtime REAL NOT NULL, sha1 TEXT, status TEXT NOT NULL DEFAULT 0, stable_count INTEGER NOT NULL DEFAULT 0, retry_count INTEGER NOT NULL DEFAULT 0, first_failed_at REAL, next_retry_at REAL, upload_id TEXT, upload_callback TEXT, upload_oss_url TEXT, upload_partsize INTEGER, target_dir TEXT, pid INTEGER, file_id TEXT, progress REAL DEFAULT 0, speed REAL DEFAULT 0, message TEXT, upload_mode TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL); CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT);\")
c.execute(\"INSERT INTO files(path,size,mtime,status,created_at,updated_at) VALUES(?,?,?,?,?,?)\", (\"/old/a.mkv\",1,1.0,\"已上传\",1,1))
c.commit(); c.close()
st2 = storemod.Store(db2)
cols = [r[1] for r in st2.db.execute(\"PRAGMA table_info(files)\").fetchall()]
print(\"OldDB-has-col:\", \"source_deleted\" in cols, \"| default =\", st2.get(\"/old/a.mkv\").get(\"source_deleted\"))
import shutil; shutil.rmtree(tmp, ignore_errors=True)
"'

7) 正常接口 + 日志 + 配置指纹：
curl -s --noproxy '*' http://127.0.0.1:9844/health; echo
docker logs --tail 8 upilot-mini 2>&1
docker logs --since 10m upilot-mini 2>&1 | grep -ciE "no such column|traceback|error|异常" || echo 0
docker exec upilot-mini sh -c 'md5sum /config/config.yaml; ls -l /config/config.yaml; cat /config/.http_port'
docker exec upilot-mini sh -c 'ls -l /config/state.db* 2>/dev/null'

另外，请给一条**只看不改**的旁证：把当前运行容器的 /app/app/store.py 与 /app/app/uploader.py 的 md5 打出来（用 md5sum），并可只读地离线起一个旧 tag 容器比对同一处符号计数（旧应 0/0，新应非 0）—— 用于证明「跑的确实是新代码」。此条为可选加分项，不做也不影响。

注意：不要删除、不要修改任何文件（第 1 步的 docker tag 只是加本地回滚标签；数据库由应用自身启动时迁移，属于正常启动行为）。只回原始输出。
