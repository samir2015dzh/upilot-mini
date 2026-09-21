# 第 45~47 处部署+验证清单（第二十二轮）

本次要上生产的提交（**两笔一起**）：
- `25bfb29` 巡检改后台任务（实测 202 目录时扫描间隔 2s→105s：前台 `await` 会把主循环
  按住，其间不扫新文件、不派发上传任务）+ 防重入
- 最新一笔 `fix(sweep,p115)` 巡检业务拒绝可见化 + `is_transient` 死代码修正

**第 0~3 步是标准部署动作；第 4 步起全部是只读探针 ——
不要删除、不要修改任何东西，逐条原样回贴输出。**

## 0. 部署前状态
```bash
docker inspect upilot-mini --format '镜像={{.Image}} 启动={{.State.StartedAt}}'
md5sum /home/<user>/upilot-mini/config.yaml
docker ps --format '{{.Names}}' | wc -l
```
（期望：镜像 `3331d7b9f628…`；md5 `519ff7ac96e6fdc4a2e2d8f8207a03e9`；15 个容器）

## 1. 拉镜像
```bash
cd /home/<user> && docker compose -f upilot-mini.yml pull
```

## 2. 重建
```bash
docker compose -f upilot-mini.yml up -d --force-recreate
```
（不加 `--remove-orphans`）

## 3. 等 25 秒后核对容器
```bash
docker inspect upilot-mini --format '镜像={{.Image}} 启动={{.State.StartedAt}} 重启={{.RestartCount}} OOM={{.State.OOMKilled}}'
docker ps --format 'table {{.Names}}\t{{.Status}}'
```
（期望：镜像已变、不再是 `3331d7b9f628`；`Up (healthy)`；`RestartCount=0`；
兄弟容器一个都没停）

## 4. 只读符号计数（新镜像，容器内）
```bash
docker exec upilot-mini sh -c 'grep -c spawn_sweep app/main.py; grep -c spawn_sweep app/uploader.py; grep -c sweep_running app/uploader.py; grep -c 被拒绝 app/uploader.py; grep -c TRANSIENT_ERRNOS app/p115.py'
```
（期望依次：`main.py` ≥2、`uploader.py` ≥1、`sweep_running` ≥3、`被拒绝` ≥1、
`TRANSIENT_ERRNOS` ≥2）

## 5. 阳性对照（旧镜像 `3331d7b9f628`，容器外只读跑同一组 grep）
```bash
docker run --rm --entrypoint sh 3331d7b9f628 -c 'grep -c spawn_sweep app/main.py; grep -c sweep_running app/uploader.py; grep -c 被拒绝 app/uploader.py; grep -c TRANSIENT_ERRNOS app/p115.py'
```
（期望：**全 0** —— 证明这三处修复确实只存在于新镜像）

## 6. 巡检实跑观察（只读）
```bash
docker logs --since 12m upilot-mini 2>&1 | grep -a "同集巡检" | tail -8
docker logs --since 12m upilot-mini 2>&1 | grep -ac "has no attribute\|Traceback\|NameError"
```
（期望：巡检照常跑、`removed=0`、**无** attribute 报错 / Traceback / NameError；
正常时**不应**出现「被拒绝」warning —— 一旦出现就说明真被限流了，那正是本次修复
要让它可见的东西）

## 7. 部署后核对
```bash
md5sum /home/<user>/upilot-mini/config.yaml
docker ps --format '{{.Names}} {{.Status}}'
```
（期望：md5 仍为 `519ff7ac96e6fdc4a2e2d8f8207a03e9`；兄弟容器 uptime 未变）
