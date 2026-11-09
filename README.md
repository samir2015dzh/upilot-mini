# UPilot-mini

只做一件事：**监控本地目录 → 优先 SHA1 秒传到 115 → 秒传失败只重发校验请求反复重试 → 次数/时间耗尽才走分块上传（断点续传）→ 保留本地相对目录结构**。

不联网授权、无激活码，自带一块深色 Web 仪表盘（单页、零外部依赖、容器内离线可用），全部状态在 SQLite 里，重启不丢、不重复读盘。

> 与 UPilot 官方镜像无关，是按公开行为复刻的「只保留秒传这一条链路」的精简实现。

## 0. 直接拉镜像（不用自己构建）

```bash
docker pull samir2013dzh/upilot-mini:latest     # Docker Hub，amd64 + arm64
docker pull ghcr.io/samir2015dzh/upilot-mini:latest   # 备用：GHCR
```

镜像由本仓库的 GitHub Actions 在每次推送到 `main` 时自动构建（多架构）：**先跑 lint 和离线测试，
全部通过才构建、推送**。每次推送除了 `:latest` 还有 `:<运行编号>` 和 `:sha-<提交前 7 位>` 两个固定标签，
出问题时把 compose 里的 `:latest` 换成上一个能用的 `:sha-xxxxxxx` 就能回滚。源代码在
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

## 2. 拿 115 cookie（三种方式）

**方式 A：直接填（最简单）**

把浏览器里抓到的 `UID=...; CID=...; SEID=...; KID=...` 填进 `config.yaml` 的 `p115.cookie`，
或用环境变量 `P115_COOKIE`；也可以打开 Web 界面 → 设置页 →「115 账号」→「115 Cookie」粘进去保存，
**保存即生效**（自动重建 115 客户端，不用重启）。

**方式 B：跟着别的程序走（symedia 等已经在维护 cookie 的话）**

设置页 →「从别的程序同步 cookie」填上对方配置文件的容器内路径（compose 里挂进来那个），
程序每分钟读一次，发现里面的 115 cookie 变了就自动同步过来并重建客户端。
细节见 §5「115 cookie 跟着别的程序走」。

**方式 C：命令行扫码（容器里出二维码）**

```bash
docker compose run --rm upilot-mini login                      # 槽位取 config.yaml 的 p115.app
docker compose exec upilot-mini python /app/app/main.py login  # 在运行中的容器里做
```

终端里出二维码，用 115 App 扫；成功后 cookie 写入 `/config/config.yaml`，然后 `docker compose restart`。

**`p115.app` 是什么？** 它是 115 客户端身份 —— 所有 115 请求（建目录 / 秒传 / 上传）都用它。
挑一个自己平时不用的端即可，可选值（面板里是中文下拉，13 个）：

| 槽位 | 对应客户端 |
| --- | --- |
| `115ipad` / `ipad` | 115（iPad端·旧版 / iPad端）—— **默认** |
| `115android` / `115ios` | 115（Android端·旧版 / iOS端·旧版） |
| `tv` / `apple_tv` | 115网盘（Android电视端 / Apple电视端） |
| `wechatmini` / `alipaymini` | 115生活（微信小程序 / 支付宝小程序） |
| `harmony` | 115（鸿蒙端） |
| `web` | 网页版 |
| `windows` / `mac` / `linux` | Windows / macOS / Linux 电脑端 |

## 3. 工作流程（与官方行为一致）

```
发现新文件 → 等稳定（连续 N 轮 size/mtime 不变）→ 算 SHA1（1MB 分块，算完落库）
   ↓
建目标目录（fs_makedirs 整条链，保留相对结构，cid 带缓存）
   ↓
秒传 upload_file_init(filename/size/sha1/pid)  ← 只发元数据 + 服务端要求的 range 片段
   ├─ 命中（reuse & state 都为真）→ 已上传（不消耗上行流量）
   ├─ upload.mode=rapid_only（默认）→ 未命中一律进「待重试」排队等：
   │     前 max_count 轮每 interval_seconds 一次，之后每 long_interval_seconds 一次，
   │     等满 24 小时改每 1 小时、满 7 天改每 6 小时（retry.long_backoff），
   │     永远只发校验请求，不普通上传、不消耗上行
   ├─ 未命中 & 文件 < small_file_mb → 直接分块上传      ← 仅 rapid_then_upload
   └─ 未命中 & 大文件 → 待重试（默认 60s 后，最多 max_count 次 / max_hours 小时）
          每次重试只重发校验请求，不重传数据            ← 仅 rapid_then_upload
   ↓
分块上传（10MB 分块，OSS 多段上传；upload_id / callback / oss_url 落库 → 暂停或重启后从已传分块继续）
```

不重复读盘的四道防线：秒传命中根本不读文件 → SHA1 落库后不再重算 → 同 path+size+mtime 的扫描去重 → 稳定判定（避免文件还在写就上传）。

文件被覆盖（size/mtime 变化）会被识别为新文件，旧 SHA1 与断点作废重来。

## 4. 配置要点（`/config/config.yaml`）

| 配置项 | 默认 | 说明 |
| --- | --- | --- |
| `p115.cookie` / `p115.app` | — / `115ipad` | 115 凭据与客户端身份（所有 115 请求都用 `app`；可选值见 §2） |
| `paths.source` | `/media/source` | 容器内监控目录 |
| `paths.target_root` | `/UPILOT-mini` | 没被 `paths.map` 命中的文件落在这里，不存在会自动创建 |
| `paths.map` | `[]` | 本机相对路径 → 115 目录的映射表（可改名、大小写照写），见 §5「目录映射」 |
| `scan.interval_seconds` | 30 | 扫描间隔 |
| `scan.stable_rounds` | 2 | 连续 N 轮 size/mtime 不变才处理 |
| `scan.ignore_suffixes` | 见示例 | 下载中的临时后缀 |
| `scan.include_suffixes` | 见示例（视频扩展名） | 只处理这些扩展名；留空 `[]` 表示不过滤。默认只传视频本体，整库挂进来也不会把 `.strm`/`.nfo`/图片传上去 |
| `scan.exclude_dirs` | `[]` | 相对监控根目录的路径片段（如 `["Sample","extras"]`），命中则整棵子树跳过 |
| `upload.delete_after_upload` | `false` | 传完就删掉监控目录里的源文件本体（破坏性，默认关）。播放不受影响——symedia 建好 strm 后是直接从云端读的；只删文件不删目录 |
| `upload.mode` | `rapid_only` | `rapid_only` = 只用秒传，未命中就排队等，**永不普通上传**；`rapid_then_upload` = 秒传优先，重试耗尽后转分块上传 |
| `upload.small_file_mb` | 10 | 仅 `rapid_then_upload`：秒传未命中且小于该值直接上传 |
| `upload.concurrency` | 2 | 同时在处理的文件数 |
| `upload.init_min_interval` | 0.5 | 两次初始化请求的最小间隔（防 115 限流 401） |
| `retry.interval_seconds` | 60 | 秒传重试间隔（每轮只发校验请求） |
| `retry.max_count` | 5 | `rapid_only`：前 N 轮用短间隔；`rapid_then_upload`：超过即算重试耗尽 |
| `retry.long_interval_seconds` | 900 | 仅 `rapid_only`：短间隔用完后改成这个节奏继续等 |
| `retry.long_backoff` | 24h→3600s，168h→21600s | 仅 `rapid_only`：按「第一次未命中至今」逐级放慢长间隔（只会更慢）。设成 `[]` 恢复旧行为（永远按 `long_interval_seconds`） |
| `http.health_stall_seconds` | 600 | 主循环心跳超过这么多秒没更新，`/health` 返回 503，容器变 unhealthy |
| `retry.max_hours` | 12 | 仅 `rapid_then_upload`：累计超过这么多小时就放弃重试，转分块上传 |
| `http.enabled` / `port` | true / 9844 | Web 仪表盘与 JSON API |

### 只走秒传（纯秒传模式，默认）

`upload.mode: rapid_only` 时，这条链路**根本没有普通上传**：秒传不命中就把文件放回「待重试」，前 `retry.max_count` 轮每 60 秒发一次校验请求，之后每 15 分钟发一次，一直到云端出现同 hash 为止 —— 全程零上行，一分钱流量不花。

为什么这招管用：115 的秒传是**全站 hash 索引**，不只看你自己的盘。新剧集刚出时云端没有，过一阵别人传上去，你这边下一次校验就直接命中（实测：`甲剧 S01E27 2160p` 1290 MB 秒传命中，零上行）。想手动催一下就在面板点「提前重试秒传」或 `POST /api/retry-now`。

代价：云端一辈子没出现同 hash 的文件，它就一直躺在「待重试」里（不占带宽、不占硬盘，只占一条记录）。想让它最终落地上传，把 `upload.mode` 换回 `rapid_then_upload`。

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
| GET | `/health` | 健康检查（Docker HEALTHCHECK 用的就是它）。主循环卡住超过 `http.health_stall_seconds` 秒返回 503 |
| POST | `/api/scan` | 立即扫描一轮 |
| POST | `/api/retry-now` | 把所有待重试的秒传提前到期 |
| POST | `/api/requeue` | 失败项重新入队 |
| POST | `/api/pause` `/api/resume` | 暂停 / 恢复派发（在途任务跑完） |
| GET | `/login` `/logout` | 站内登录页与退出（`mode: form` 时启用） |
| GET | `/api/tasks` | 任务明细（`status` / `q` / `limit` / `offset`） |
| GET | `/api/task` | 单个文件详情（`?path=`） |
| POST | `/api/task` | 单文件操作（`retry` / `retry-now` / `upload-now` / `delete`） |
| GET | `/api/config` | 设置页表单结构（密钥只回脱敏状态） |
| POST | `/api/config` | 保存设置（写回 config.yaml 并立即生效） |
| GET | `/api/logs` | 最近的运行日志（内存环形缓冲，不落盘） |
| GET | `/static/*` | 前端静态资源（白名单：app.css / app.js） |

**跨站请求防护（CSRF）**：所有 POST 都会检查是不是别的网站借你的浏览器发的——浏览器带的
`Sec-Fetch-Site` 必须是 `same-origin`；老浏览器没有这个头时看 `Origin` / `Referer`，必须是本服务自己
（请求的 Host、反代转发的 `X-Forwarded-Host`、或 `http.public_url`）。curl / 监控脚本不带这些头，照常放行。
另外 `/api/*` 带请求体时**只收 JSON**（`Content-Type: application/json`），例如：

```bash
curl -X POST http://host:9844/api/scan                                   # 无请求体，直接发
curl -X POST -H 'Content-Type: application/json' \
     -d '{"path":"/media/source/a.mkv","action":"retry"}' http://host:9844/api/task
```

### 界面能改什么（三页布局）

面板分三页：**概览 / 任务明细 / 设置**。

- **任务明细**：按状态（全部 / 进行中 / 待上传 / 待重试 / 已完成 / 错误）筛选 + 关键词搜索 + 翻页；每行给出大小、状态、进度、上传方式（秒传 or 分块）、重试次数、SHA1 摘要、更新时间；点「详情」开侧栏看全字段（云端 pid / file_id、断点续传信息、消息、入队与更新时间），并可**立即处理 / 重新排队 / 提前重试秒传 / 删除记录**。
- **设置**：目录、115 cookie、扫描、上传、秒传重试、界面与鉴权、日志级别都能在页面上直接改；保存后写回 `/config/config.yaml`（旧文件留 `config.yaml.bak`）并即时生效，改了 `p115.cookie` 会自动重建 115 客户端。
  - cookie / 登录密码这类密钥字段只显示「已设置 · N 字符」，**不返回任何明文片段**；输入框留空 = 保持原值，勾「清除」才会清空。
  - 带「需重建容器」标记的项（监听地址/端口、日志级别）要 `docker compose up -d --force-recreate` 后才生效。
  - 保存是整文件重写，config.yaml 里的注释会简化（字段说明以 `config/config.example.yaml` 为准）。

### 目录映射（本机子目录 → 115 指定目录）

设置页「目录」组里的「本机 → 115 目录映射」，或 `config.yaml` 里的 `paths.map`：

```yaml
paths:
  source: "/media/source"
  target_root: "/UPILOT-mini"        # 没命中映射的文件都落这里
  map:
    - {from: "TVs",              to: "/TVs"}
    - {from: "Movies/Others",    to: "/Movies/Others"}
    - {from: "Movies/AnimeMovies", to: "/Movies/AnimeMovies"}
    - {from: "Anime",            to: "/OtherAnime"}     # 云端改名
    - {from: "Something",        to: "/something"}      # 云端大小写照你写的来
```

规则很短：`from` 是**相对监控根目录**的路径（逐段比对、大小写敏感），命中时**取最长前缀**，
云端目录 = `to` + 命中点之下的剩余结构；一个都没命中才回落到 `target_root`。
所以 `from` 留空（写成 `=> /Root`）表示「整个监控根平移到 /Root」。

对应关系示例（挂载 `/data/media → /media/source`）：

| 本机 | 云端 |
| --- | --- |
| `/media/source/TVs/我的剧集/S01E01.mkv` | `/TVs/我的剧集/S01E01.mkv` |
| `/media/source/Movies/Others/某电影/a.mkv` | `/Movies/Others/某电影/a.mkv` |
| `/media/source/Anime/葬送的芙莉莲/E01.mkv` | `/OtherAnime/葬送的芙莉莲/E01.mkv` |
| `/media/source/Something/杂物/a.bin` | `/something/杂物/a.bin` |
| `/media/source/Movies/某电影/a.mkv`（未命中） | `/UPILOT-mini/Movies/某电影/a.mkv` |

### 115 cookie 跟着别的程序走（自动同步，可选）

机器上如果还有别的 115 工具在维护 cookie（比如 symedia），可以让 upilot-mini 直接跟着它，
省得两边各填一次：

```yaml
# docker-compose.yml
    volumes:
      - /home/<user>/symedia/config:/symedia-config:ro     # 把对方的配置目录只读挂进来
```

然后在设置页「115 账号」组的「从别的程序同步 cookie」里填 `/symedia-config/config.yaml`
（或直接写进 `config.yaml` 的 `p115.cookie_from`）。之后程序每 60 秒读一次那个文件，发现里面的
115 cookie 与自己的不一样就自动写进配置并重建 115 客户端，日志会打「已从 … 同步 115 cookie」。

- 支持 yaml / json：会递归找 `UID=…; CID=…; SEID=…; KID=…` 这种字符串，也认 `{UID:…, CID:…}` 这种字典写法。
- **挂目录而不是单个文件** —— 对方保存配置时往往是整文件替换，挂单文件会跟丢。
- 找不到 cookie 时什么都不做（不会把配置写坏）；留空 `p115.cookie_from` 就关掉这个功能。

### 秒传完成后通知别的程序（symedia 联动）

秒传命中（或分块上传完成）后，可以 POST 一个事件给别的程序，让对方立刻处理这个文件。典型用法是
**symedia 的「链接同步」**：它在 Webhook 模式下靠 `/api/v1/webhook/clouddrive2/file_notify` 收
CloudDrive2 的文件变更事件，收到就对该文件做目录树同步、生成 strm。我们按同一份契约发
（`action=create`、`is_dir=false`、`source_file` 是文件路径），symedia 会当成「CD2 报告多了一个文件」来处理。

面板上只有两项要填：**打开开关**、**symedia 地址**（`http://172.17.0.1:8095` 这种，只写地址，
后面的接口路径和参数程序自己补）。写成配置文件是这样：

```yaml
notify:
  enabled: true
  url: "http://172.17.0.1:8095"   # 只填地址；接口路径与参数自动补全
  on: rapid_hit                   # 秒传命中就通知（「高级设置」里还有 uploaded / both）
  # 下面两项都有默认值，一般不用写：
  # path_prefix: "/CloudNAS/CloudDrive/115open"   对方按 CD2 挂载路径找文件
  # query: "device_name=upilot-mini&user_name=upilot-mini&type=notify"
```

- `path_prefix` 负责把 115 网盘路径换算成对方视角：`/TVs/x.mkv` → `/CloudNAS/CloudDrive/115open/TVs/x.mkv`。
  这个前缀就是 symedia 某个链接同步任务的「媒体目录」去掉网盘内目录后的那一段（symedia 里能看到）。
- `on` 默认 `rapid_hit`（只在秒传命中时发）；纯秒传模式下基本都是这条。
- 通知失败只写日志，**不影响上传本身**；`timeout` / `retries` 可调。
- 对方要认证就填 `auth`（`Basic xxx` / `Bearer xxx`），会作为 `Authorization` 头带上。

### 传完就删掉本地源文件（可选）

`upload.delete_after_upload: true` 时，秒传命中（或分块上传完成）之后会把监控目录里那个源文件删掉，省地方。

删了照样能播：播放链路是 symedia 建好的 strm，指向云端那套路径，从 115 直接读，跟本地这份源文件没关系。

前置条件（缺一个都会删不动，实测踩过）：

1. compose 里监控目录必须是 `:rw` 挂载，写成 `:ro` 时容器只能读到 `Read-only file system`
2. 容器以 uid 1000 运行，而源目录通常是 `root:root 775` —— 光改 `:rw` 还不够，会报 `Permission denied`。给容器用户单独授权（不动属主，可逆）：

```bash
setfacl -R -m u:1000:rwx /data/media
setfacl -R -d -m u:1000:rwx /data/media     # 默认 ACL，新下载的文件也继承
```

撤销用 `setfacl -b -R /data/media`。这两步是宿主侧的，重新部署时会丢，要重做。

护栏都写在代码里，不依赖配置：

- 只删 `paths.source` 监控目录**内部**的文件，路径跑到外面一律跳过并记警告
- 大小和入库记录对不上（说明文件被换过）就跳过
- 只删文件本体，**不删文件夹**
- 删除失败只记一条警告，上传结果不受影响；成功则在记录里留一句「已删除本地源文件」，任务明细里能看到

面板上也有同一个开关（设置 → 上传 → 传完就删掉本地源文件），带红色提醒。

### 洗板时云端只留最新那一版（可选）

同一集重下（洗板）会出现这种情况：先传了带 DV 的 `S01E31`，洗板后重下不带 DV 的 `S01E31`，
两份都秒传上去了 —— 云端就有了两个第 31 集。

`upload.cleanup_previous_versions: true` 打开后，新版传成功时会自动把**同一集的旧版本**
移进 115 回收站（可恢复，不是彻底删除），一个目录里只留最新那份。

判「同一集」的规则（`app/p115.py::episode_key`）只看三样东西：

- 剧名（季集号之前的部分）
- 季集号（`S01E31`；`第31集` 这种中文写法也认）
- 扩展名

中括号里的内容整段忽略，所以下面两个文件判成同一集：

```
甲剧 (2026) [tmdbid=100001] S01E31 [2160p H265 DDP 5.1 WEB-DL DV TX DemoWeb].mkv
甲剧 (2026) [tmdbid=100001] S01E31 [2160p H265 DDP 5.1 WEB-DL TX DemoWeb].mkv
```

点分命名（`Show.S01E31.2160p.DV.mkv`）同理 —— 集号之后的内容一律不参与比较。

护栏都写在代码里，不依赖配置：

- 只在**同一个云端目录**里找，跨目录不碰
- 只处理视频本体（`.mkv` / `.mp4` / `.ts` …），`.nfo` / `.strm` / `.jpg` 不参与
- 文件名里认不出季集的（比如 `花絮合集.mkv`）一律不动，不去猜
- 大小与刚传上去那条**完全一样**的也不动（更像 115 给它改过名的那条）
- 同集旧版本多于 `upload.cleanup_max_delete`（默认 3）就整体跳过并记警告
- 任何失败只记一条警告，上传结果不受影响；真清掉了会在记录里留一句
  「云端同集旧版本已清 N 个：<文件名>」，任务明细里能看到

面板上也有同一个开关（设置 → 上传 → 洗板时云端只留最新那一版），带红色提醒。

两点边界要知道：删的是云端文件条目，**symedia 那边先前为旧版建好的 strm 不会自动消失**，
等它自己扫到差异再处理；本地已经下好的旧文件（如果 MoviePilot 还留着）也不受影响，
那是下载侧的事。

### 鉴权与反代

面板默认**没有鉴权**（只绑 `127.0.0.1`，外面碰不到）。要反代/暴露到内网，先在 `config.yaml` 打开：

```yaml
http:
  auth:
    enabled: true
    mode: form                  # form = 站内登录页（推荐）；basic = 浏览器原生弹框
    username: "admin"
    password: "换成一串长随机密码"
    session_hours: 72           # 仅 mode=form：登录状态保持多少小时
```

`mode: form` 是反代场景该用的那种：未登录访问页面会跳到 `/login`——站内登录页，与仪表盘同一套设计语言（同一色板变量、16px 圆角卡片、渐变主按钮、pill 与 hint 组件），不再出现浏览器那个灰白原生弹框。登录成功后下发 `HttpOnly; SameSite=Lax` 的签名 Cookie（HMAC-SHA256，密钥取自 `password`），到期自动失效；仪表盘右上角会多一个「退出登录」。连续输错 5 次锁 60 秒。

`mode: basic` 保持原样：浏览器直接弹原生框，适合临时排查。

两种模式下脚本通道都在：

```bash
curl -u admin:密码 http://host:9844/api/status      # HTTP Basic
curl "http://host:9844/api/status?token=密码"               # 令牌，适合监控脚本
```

`/health` 始终放行（容器 HEALTHCHECK 用）。密码留空等同于关闭。改掉 `password` 等于换掉签名密钥——所有已登录的浏览器立刻失效，这就是强制全员下线的做法。

反代把 `Cookie` 与 `Authorization` 头透传即可（nginx 默认透传 Cookie）：

```nginx
location / {
    proxy_pass http://<server-ip>:9844;
    proxy_set_header Host $host;
    proxy_set_header Authorization $http_authorization;
    proxy_set_header X-Real-IP $remote_addr;   # 登录失败限速按它区分来源
}
```

注意：反代域名自己若叠了 `auth_basic`，看到的仍是浏览器原生弹框——要站内登录页就别在反代层开 Basic，交给应用这层。

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
- **暂停/重启后大文件要重传吗**：不用。分块的 `upload_id` 和当时的分块大小都在库里，重启后 `list_parts` 取回已传分块，本地文件从已传字节处接着读、按原分块大小继续传（中途改了 `partsize_mb` 也不影响续传）。分块上传中途失败（比如断网）会按 30 秒、60 秒退避自动续传，连续 3 次失败才标「错误」。
- **`docker stop` / 重启时会丢进度吗**：不会。收到退出信号后不再派发新任务，正在做的（秒传请求、SHA1、分块）最多再等 8 秒自然结束（docker 默认 10 秒后强杀），没做完的状态都在库里，下次启动接着做。
- **日志太多**：compose 示例已带日志轮转（最多 3×10 MB）；自己写 `docker run` 的加 `--log-opt max-size=10m --log-opt max-file=3`。逐个文件的「秒传未命中」只有第一次是 INFO，之后是 DEBUG，每轮另打一行「秒传校验汇总」；要看明细把 `log_level` 改成 `DEBUG`。
- **容器显示 unhealthy**：`docker inspect --format '{{json .State.Health}}' upilot-mini` 看原因；`stalled: main loop heartbeat …` 表示主循环卡住了，`docker restart upilot-mini` 即可（状态都在库里）。
- **115 换了 OSS 区域，分块上传连不上**：在 `config.yaml` 的 `upload` 下加 `oss_endpoint: "https://oss-cn-xxx.aliyuncs.com"`，默认是深圳。
- **状态怎么看**：浏览器打开 `http://<host>:9844/`，或 `curl -s localhost:9844/api/status`，或直接查 `/config/state.db` 的 `files` 表。
- **想临时停一下上传**：界面上点「暂停调度」，在途任务跑完就停；点「恢复调度」继续。
- **想重新传某个文件**：删库里那行（`sqlite3 /config/state.db "delete from files where path like '%xxx%'"`），下一轮扫描会重新入队。
- **权限**：容器以 uid 1000 运行；挂载目录只读即可，`/config` 需要可写。

## 9. 本地（不进容器）跑测试

```bash
pip install PyYAML ruff                # 离线测试只需要 PyYAML（115 客户端全部用替身）；Python >= 3.12
ruff check app                         # 与 CI 同一套规则（ruff.toml：F + B）
python tests/test_flow.py              # 47 个离线用例，约 30 秒；CI 在构建镜像之前跑同一套
```

要装完整运行依赖（真跑服务）：

```bash
pip install -r requirements.txt -c constraints.txt   # constraints.txt 锁死全部子依赖
```
