# UPilot-mini

只做一件事：**监控本地目录 → 优先 SHA1 秒传到 115 → 秒传失败只重发校验请求反复重试 → 次数/时间耗尽才走分块上传（断点续传）→ 保留本地相对目录结构**。

不联网授权、无激活码，自带一块深色 Web 仪表盘（单页、零外部依赖、容器内离线可用），全部状态在 SQLite 里，重启不丢、不重复读盘。

> 与 UPilot 官方镜像无关，是按公开行为复刻的「只保留秒传这一条链路」的精简实现。

## 0. 直接拉镜像（不用自己构建）

```bash
docker pull samir2013dzh/upilot-mini:latest     # Docker Hub，amd64 + arm64
docker pull ghcr.io/samir2015dzh/upilot-mini:latest   # 备用：GHCR
```

镜像由本仓库的 GitHub Actions 在每次推送到 `main` 时自动构建（多架构），源代码在
`https://github.com/samir2015dzh/upilot-mini`（私有仓库）。

用镜像跑（不本地构建）：

```bash
mkdir -p ./config && cp config/config.example.yaml ./config/config.yaml
$EDITOR ./config/config.yaml
docker compose pull && docker compose up -d --no-build
```

## 1. 从源码快速开始

```bash
# 1) 准备目录
mkdir -p ./config
cp config/config.example.yaml ./config/config.yaml
$EDITOR ./config/config.yaml        # 填 p115.cookie、paths.source 等

# 2) 改 compose 里的媒体目录挂载
$EDITOR docker-compose.yml          # - /path/to/your/media:/media/source:ro

# 3) 构建并启动
docker compose up -d --build
docker compose logs -f
```

界面：`http://<host>:9844/` ｜ JSON 状态：`http://<host>:9844/api/status` ｜ 健康检查：`http://<host>:9844/health`

## 2. 拿 115 cookie（两种方式）

**方式 A：扫码登录（推荐）**

```bash
docker compose run --rm upilot-mini login
```

终端出二维码，用 115 App 扫；成功后 cookie 自动写入 `/config/config.yaml`，然后 `docker compose up -d`。
`p115.app` 换成你平时不用的槽位（默认 `qandroid`），避免把手机/网页端挤下线。

（也可以进正在运行的容器执行：`docker compose exec upilot-mini python /app/app/main.py login`）

**方式 B：直接填**
把浏览器抓到的 `UID=...; CID=...; SEID=...; KID=...` 填进 `config.yaml` 的 `p115.cookie`，或用环境变量 `P115_COOKIE`。

## 3. 工作流程（与官方行为一致）

```
发现新文件 → 等稳定（连续 N 轮 size/mtime 不变）→ 算 SHA1（8MB 分块，算完落库）
   ↓
建目标目录（fs_makedirs 整条链，保留相对结构，cid 带缓存）
   ↓
秒传 upload_file_init(filename/size/sha1/pid)  ← 只发元数据 + 服务端要求的 range 片段
   ├─ 命中（reuse & state 都为真）→ 已上传（不消耗上行流量）
   ├─ 未命中 & 文件 < small_file_mb → 直接分块上传
   └─ 未命中 & 大文件 → 待重试（默认 60s 后，最多 max_count 次 / max_hours 小时）
          每次重试只重发校验请求，不重传数据
   ↓
分块上传（10MB 分块，OSS 多段上传；upload_id / callback / oss_url 落库 → 暂停或重启后从已传分块继续）
```

不重复读盘的四道防线：秒传命中根本不读文件 → SHA1 落库后不再重算 → 同 path+size+mtime 的扫描去重 → 稳定判定（避免文件还在写就上传）。

文件被覆盖（size/mtime 变化）会被识别为新文件，旧 SHA1 与断点作废重来。

## 4. 配置要点（`/config/config.yaml`）

| 配置项 | 默认 | 说明 |
| --- | --- | --- |
| `p115.cookie` / `p115.app` | — / `qandroid` | 115 凭据与登录槽位 |
| `paths.source` | `/media/source` | 容器内监控目录 |
| `paths.target_root` | `/UPILOT-mini` | 115 目标根目录，不存在会自动创建 |
| `scan.interval_seconds` | 30 | 扫描间隔 |
| `scan.stable_rounds` | 2 | 连续 N 轮 size/mtime 不变才处理 |
| `scan.ignore_suffixes` | 见示例 | 下载中的临时后缀 |
| `scan.include_suffixes` | 见示例（视频扩展名） | 只处理这些扩展名；留空 `[]` 表示不过滤。默认只传视频本体，整库挂进来也不会把 `.strm`/`.nfo`/图片传上去 |
| `scan.exclude_dirs` | `[]` | 相对监控根目录的路径片段（如 `["Sample","extras"]`），命中则整棵子树跳过 |
| `upload.small_file_mb` | 10 | 秒传未命中时，小于该值直接上传；大于则先重试秒传 |
| `upload.concurrency` | 2 | 同时在处理的文件数 |
| `upload.init_min_interval` | 0.5 | 两次初始化请求的最小间隔（防 115 限流 401） |
| `retry.interval_seconds` | 60 | 秒传重试间隔 |
| `retry.max_count` / `max_hours` | 5 / 12 | 重试上限，超过转分块上传 |
| `http.enabled` / `port` | true / 9844 | Web 仪表盘与 JSON API |

## 5. 界面（Web 仪表盘）

打开 `http://<host>:9844/` 就是一块深色仪表盘，纯单页 HTML + 原生 JS，不依赖 CDN，NAS 上离线也能用：

- **顶部四张卡**：文件总数 / 已上传（其中秒传几个）/ 秒传命中（省下多少上行）/ 处理中与待重试（重试累计次数）。
- **左栏**：处理中·待重试（黄色倒计时条，剩余时间越少条越短）→ 最近活动（时间 / 文件 / 状态 / 115 目标目录 / 说明，长文本悬停看全）→ 运行配置。
- **右栏**：秒传命中率环图（命中 / 已上传）、状态分布（含占比）、连接（刷新间隔、上次扫描、扫描轮次）、队列与提示。
- **操作**：暂停/恢复调度、自动刷新间隔切换（3 / 5 / 10 / 30 秒，点一下切一档）、刷新、立即扫描、立即重试秒传、失败项重新入队。

JSON 接口（脚本、监控、HomeAssistant 都能用）：

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| GET | `/api/status` | 统计、队列、处理中、最近活动、配置摘要 |
| GET | `/health` | 健康检查（Docker HEALTHCHECK 用的就是它） |
| POST | `/api/scan` | 立即扫描一轮 |
| POST | `/api/retry-now` | 把所有待重试的秒传提前到期 |
| POST | `/api/requeue` | 失败项重新入队 |
| POST | `/api/pause` `/api/resume` | 暂停 / 恢复派发（在途任务跑完） |

## 6. 离线自检（不需要 cookie）

```bash
docker compose run --rm upilot-mini python /app/app/main.py --dry-run
```

`--dry-run` 用内置的假客户端模拟 115（目录创建返回假 cid、秒传前 N 次未命中然后命中），
真实验证：扫描、稳定判定、SHA1 计算、目录映射、秒传重试、分块上传、状态落库，全程不联网。
界面会显示「dry-run 离线自检 · 数字为模拟」徽章，适合先看一遍界面和目录映射再填 cookie。

只想扫一遍目录看会传什么：

```bash
docker compose run --rm upilot-mini python /app/app/main.py scan-once
```

## 7. 目录结构

`resolve_upload_target()` 按监控根目录算相对路径，秒传与分块上传用同一个 pid，所以结构会保留：

| 本地 | 115 |
| --- | --- |
| `<source>/电影/哈利与巧克力工厂/哈利与巧克力工厂.mp4` | `<target_root>/电影/哈利与巧克力工厂/哈利与巧克力工厂.mp4` |
| `<source>/哈利与巧克力工厂-哈利与巧克力工厂.mp4` | `<target_root>/哈利与巧克力工厂-哈利与巧克力工厂.mp4` |

目录是**按需创建**的（只有有文件要传的路径才建），空文件夹不会同步过去。

## 8. 常见问题

- **秒传总不命中**：正常。115 只对「网盘里已有完全相同文件（SHA1 一致）」的才秒传；第一次上传必然走流量。
- **大量小文件报 401**：115 限流。把 `upload.init_min_interval` 调大（如 1.0~2.0），或把 `upload.concurrency` 降到 1。
- **报 10014 / 云端目录不存在**：程序会自动清 cid 缓存并重建目录后重试。
- **暂停/重启后大文件要重传吗**：不用。分块的 `upload_id` 等一直在库里，重启后 `list_parts` 校验后从已传分块继续。
- **状态怎么看**：浏览器打开 `http://<host>:9844/`，或 `curl -s localhost:9844/api/status`，或直接查 `/config/state.db` 的 `files` 表。
- **想临时停一下上传**：界面上点「暂停调度」，在途任务跑完就停；点「恢复调度」继续。
- **想重新传某个文件**：删库里那行（`sqlite3 /config/state.db "delete from files where path like '%xxx%'"`），下一轮扫描会重新入队。
- **权限**：容器以 uid 1000 运行；挂载目录只读即可，`/config` 需要可写。

## 9. 本地（不进容器）跑测试

```bash
pip install -r requirements.txt        # 需要 Python >= 3.12
python tests/test_flow.py              # 5 个离线用例：目录保留 / 秒传重试 / SHA1 复用 / 小文件直传 / 去重
```
