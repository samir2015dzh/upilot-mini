# UPilot-mini

只做一件事：**盯住本地文件夹，把里面的文件用 SHA1 秒传到 115，文件夹结构原样保留。**

秒传没成功就隔一会儿再问，默认不会真上传；一集有新旧版本时，只留最新的那份。
自带一块网页面板（深色/浅色都有，不依赖任何外部资源，离线也能用），状态存在 SQLite 里，重启不丢。

> 与 UPilot 官方镜像无关，是按公开行为复刻的「只保留秒传这一条链路」的精简实现。

当前版本 **v0.2.0**（2026-11-10）。推荐镜像：`samir2013dzh/upilot-mini:sha-b5e5622`。这一版的变化（只秒传加固、洗版、限流冷却、特别篇/多集/`PROPER`、跳过未下完文件、启动配置校验、等秒传文案等）见 [CHANGELOG.md](CHANGELOG.md)。

## 0. 直接拉镜像（不用自己构建）

```bash
docker pull samir2013dzh/upilot-mini:sha-b5e5622      # 推荐：v0.2.0 已验证的构建，amd64 + arm64
docker pull samir2013dzh/upilot-mini:latest           # 目前与上面相同；以后 main 有新构建才会变
docker pull ghcr.io/samir2015dzh/upilot-mini:sha-b5e5622   # 备用：GHCR（私有包，需先 ghcr 登录）
```

镜像由本仓库的 GitHub Actions 在每次推送到 `main` 时自动构建（多架构）：**先跑 lint 和离线测试，
全部通过才构建、推送**。每次推送除了 `:latest` 还有 `:<运行编号>` 和 `:sha-<提交前 7 位>` 两个固定标签。
生产环境建议把 compose 里的镜像钉成 `:sha-b5e5622`（见下面的示例）；出问题再换成另一个 `sha-` 标签回滚。
只改文档不会构建新镜像。源代码在 `https://github.com/samir2015dzh/upilot-mini`（公开仓库）。

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
发现新文件 → 等稳定（连续 N 轮 size/mtime 不变，旁边还有 .aria2 / .!qB 等控制文件时先等）→ 算 SHA1（1MB 分块，算完落库；同一文件改名/移动/硬链接时直接复用，不再读盘）
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
分块上传（默认 10MB 分块，超过 97.6 GiB 的文件自动放大分块、保证不超过 OSS 的 10000 片；OSS 多段上传；upload_id / callback / oss_url 落库 → 暂停或重启后从已传分块继续）
```

不重复读盘的四道防线：秒传命中根本不读文件 → SHA1 落库后不再重算 → 同 path+size+mtime 的扫描去重 → 稳定判定（避免文件还在写就上传）。下载器预分配了完整大小时，只看 size/mtime 不够，旁边还有 `.aria2` / `.!qB` / `.part` 等控制文件就先不算 SHA1。扫描时用目录里已经列出的文件名判断控制文件，已经传完的记录不再探。不用 inotify（NAS 上的 CIFS/NFS 挂载通知不可靠）。

文件被覆盖（size/mtime 变化）会被识别为新文件，旧 SHA1 与断点作废重来。

## 4. 配置要点（`/config/config.yaml`）

启动时会先检查配置。YAML 写坏了、该是数字的写成了文字、监控目录没填，会直接退出并打出中文原因；监控目录还不存在、还没填 115 凭证，只警告，不阻止启动。

| 配置项 | 默认 | 说明 |
| --- | --- | --- |
| `p115.cookie` / `p115.app` | — / `115ipad` | 115 登录凭证与客户端身份（可选值见 §2） |
| `paths.source` | `/media/source` | 容器内监控目录 |
| `paths.target_root` | `/UPILOT-mini` | 没写对应关系的文件放这里，目录没有会自动建 |
| `paths.map` | `[]` | 本地文件夹 → 115 文件夹的对应表，见 §5「目录映射」 |
| `scan.interval_seconds` | 30 | 扫描间隔 |
| `scan.stable_rounds` | 2 | 文件连续 N 轮没变化才处理 |
| `scan.ignore_suffixes` | 见示例 | 没下完的临时文件后缀（这些文件本身不入队；成品旁边的同名控制文件也会拦住哈希） |
| `scan.include_suffixes` | 见示例（视频扩展名） | 只处理这些扩展名，留空 = 全部。默认只传视频，不会带上 `.strm` / `.nfo` / 图片 |
| `scan.exclude_dirs` | `[]` | 要跳过的文件夹名（如 `["Sample","extras"]`），整个文件夹都不处理 |
| `upload.delete_after_upload` | `false` | 传完就删掉本地源文件（默认关）。只删文件、不删文件夹，不影响播放 |
| `upload.mode` | `rapid_only` | `rapid_only` = 只秒传，没命中就一直等，**不会真上传**；`rapid_then_upload` = 等不到再真上传。只有明确写 `rapid_then_upload` 才会真传，空着或写错都按 `rapid_only` |
| `upload.small_file_mb` | 10 | 仅 `rapid_then_upload`：没命中且小于这个值就直接上传 |
| `upload.concurrency` | 2 | 同时在处理的文件数 |
| `upload.max_file_gb` | 115 | 真上传时的单文件上限（GiB），超过直接标「错误」。115 VIP 上限 115GB（原石 5GB、尝鲜 15GB），`0` = 不限。秒传不受影响 |
| `upload.init_min_interval` | 0.5 | 两次请求至少隔几秒（防 115 限流） |
| `retry.interval_seconds` | 60 | 秒传重试间隔（只发校验请求） |
| `retry.max_count` | 5 | 前 N 轮用短间隔；`rapid_then_upload` 下超过就算重试耗尽 |
| `retry.long_interval_seconds` | 900 | 仅 `rapid_only`：短间隔用完后改成这个节奏 |
| `retry.long_backoff` | 24h→3600s，168h→21600s | 仅 `rapid_only`：等得越久问得越少。设成 `[]` 就一直按 `long_interval_seconds` |
| `http.health_stall_seconds` | 600 | 程序卡住超过这么多秒，容器会显示 unhealthy |
| `retry.max_hours` | 12 | 仅 `rapid_then_upload`：等超过这么多小时就改成真上传 |
| `http.enabled` / `port` | true / 9844 | Web 仪表盘与 JSON API |

### 只走秒传（默认）

`upload.mode: rapid_only` 下根本没有普通上传：秒传没命中，文件就回到「待重试」，隔一会儿再问一次，一直问到云端有为止。全程不占上行流量。

为什么能等到：115 的秒传是全站索引，不只看你自己的盘。新剧刚出时云端没有，过一阵有人传了，下一次就命中。想马上再试，点面板的「立即重试秒传」。

- 从 `rapid_then_upload` 切回来时，没传完的分块记录不会续传，统一回到秒传队列。
- 云端一直没有的文件会一直留在「待重试」，只占一条记录，面板会写「在等秒传，不会真上传」，不会说成队列空了。想让它最终真传，把 `upload.mode` 换成 `rapid_then_upload`。
- 如果 115 返回限流或风控（429、访问上限、操作太频繁），不会当成「没秒传到」去加次数。程序会暂停向 115 发请求，先等大约 1 分钟，不行再加倍，最长 1 小时，到点只放 1 个文件去试。面板上会有黄色提示。上一个删除还没做完这种「正忙」，只推迟这一个文件。

## 5. 界面（Web 仪表盘）

打开 `http://<host>:9844/` 就是一块深色仪表盘，纯单页 HTML + 原生 JS，不依赖 CDN，NAS 上离线也能用：

- **顶部四张卡**：文件总数 / 已上传（其中秒传几个）/ 秒传命中（省下多少上行）/ 处理中与待重试（重试累计次数）。
- **左栏**：处理中·待重试（黄色倒计时条，剩余时间越少条越短）→ 最近活动（时间 / 文件 / 状态 / 115 目标目录 / 说明，长文本悬停看全）→ 运行配置。
- **各季度情况**：按 115 上的季度目录，列出待重试数量（有几个已经到点）和最近的洗版记录。洗版记录写在状态库里，容器重启后还在。
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

`upload.delete_after_upload: true` 时，传完（秒传命中或上传完成）会把监控目录里的源文件删掉，省空间。不影响播放：播放用的是 symedia 建好的 strm，直接读 115。

要先满足两个条件，否则删不动：

1. compose 里监控目录要用 `:rw` 挂载（`:ro` 只读，删不了）
2. 容器以 uid 1000 运行，源目录通常是 `root:root 775`，要单独授权：

```bash
setfacl -R -m u:1000:rwx /data/media
setfacl -R -d -m u:1000:rwx /data/media     # 默认 ACL，新下载的文件也继承
```

撤销：`setfacl -b -R /data/media`。这是宿主机上的设置，重新部署会丢，要重做。

只会删监控目录里的文件本体，不删文件夹；文件被换过（大小对不上）会跳过；失败只记一条警告，不影响上传。面板里也有这个开关，带红色提醒。

### 洗版：同一集只留最新那一份（默认开）

同一集重下了新版本（比如先传了 `S01E01.mkv`，后来又有 `S01E01.mp4`），115 里就会有两份。
`upload.cleanup_previous_versions`（默认开）会让同一个文件夹里同一集只留**最新下载**的那份，旧的移进 115 回收站（可以还原）。本地文件不会动。

- **怎么算同一集**：只看季集号（`S01E01`、`1x01`、`第12集`、`第一百零五集`、`EP05`、`S00E01` / `SP01` 特别篇等），文件格式、画质标签、字幕组都不管。`PROPER` / `REPACK` / `v2` 也不改变「是不是同一集」。
- **怎么算最新**：看本地文件的修改时间，后下载的算新；对不上本地记录时才看 115 上的上传时间；时间还并列的话，带 `PROPER` / `REPACK` / `v2` 的算新；再分不清就不动手。
- **多部剧放同一文件夹**：默认要求剧名也有共同词，`Alpha S01E01` 不会顶掉 `Beta S01E01`。要改的话用 `upload.cleanup_title_match`（`auto` / `ignore` / `same`）。
- **想先看看会删什么**：打开 `upload.cleanup_dry_run`，只写日志和面板提示，不真删。
- **安全带**：一次要删的旧版本超过 `upload.cleanup_max_delete`（默认 3）就整体跳过；新版还没传成功时，旧版不会被删光；认不出季集的文件一律不碰。
- 每次替换都会在日志里留一行，面板概览也会显示最近 5 次。

删的只是 115 上的文件，symedia 之前给旧版建好的 strm 不会自动消失，等它自己扫到差异再处理。

<details>
<summary>细节：认得哪些写法、新旧怎么判（点开看）</summary>

| 文件名 | 认成 |
|---|---|
| `Show.S01E01.1080p.mkv` / `Show S01E01 [2160p].mp4` / `show.s01e01.ts` | `s01e001`（同一集） |
| `Show 1x01.mkv`、`Show Season 1 Episode 1.mkv` | `s01e001` |
| `某剧 第二季 第十二集.mkv`、`某剧 第12集.mkv` | `s02e012`、`e012` |
| `Show S02 EP05.mp4`、`Show EP05.mp4` | `s02e005`、`e005`（没写季号的单独算，不跟 `S01E05` 混） |
| `[字幕组] Show - 05 [1080p].mp4` | `e005` |
| `Show.S01E01-E02.mkv`、`Show.E01-E02.mkv` | `s01e001-e002`、`e001-e002`（多集合并文件，单独算） |
| `Show.S00E01.mkv`、`Show.SP01.mkv` | `s00e001`（特别篇） |
| `某剧 第一百零五集.mkv`、`某剧 第一千零一集.mkv` | `e105`、`e1001` |
| `Show.S01E01.PROPER.mkv` / `REPACK` / `v2` | 仍是 `s01e001`；时间并列时修订标记算新 |
| `Movie (2012).mkv`、`Show 1920x1080.mkv` | 认不出，不动（年份、分辨率不会当集号） |

判「最新」的顺序：

1. 状态库里能对上每条云端文件（按 115 的 pickcode，或「文件名 + 大小」）时，按本地文件修改时间判，并列再看修订标记（`PROPER` / `REPACK` / `v2`），再看首次入库时间。后下载的仍然优先；这样老版本的秒传晚命中也不会误删新版本；`delete_after_upload` 删掉本地文件后记录还在，不受影响。
2. 有一条对不上（别的工具传的）或时间并列，退回 115 条目的上传时间；时间仍并列就用修订标记；再拿不到就不动手，日志里写明原因。

清理有两个时机：新版秒传成功后马上清，以及每 5 分钟一次的云端巡检（兜底）。

</details>

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
标题旁会显示橙色小圆点（悬停或长按可看到「dry-run 离线自检 · 数字为模拟」），适合先看一遍界面和目录映射再填 cookie。

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

- **秒传总不命中**：正常。只有 115 里已经有一模一样的文件才能秒传。
- **大量小文件报 401**：被 115 限流了。把 `upload.init_min_interval` 调大（1.0~2.0），或把 `upload.concurrency` 降到 1。
- **报 10014 / 云端目录不存在**：程序会自己重建目录再试，不用管。
- **暂停/重启后大文件要重传吗**：不用，会从已传的分块接着传。中途断网也会自动续传，连续失败 3 次才标「错误」。
- **`docker stop` / 重启会丢进度吗**：不会。收到退出信号后不再接新任务，在做的最多再等 8 秒，没做完的下次启动接着做。
- **日志太多**：compose 示例已带日志轮转（最多 3×10 MB）；自己写 `docker run` 的加 `--log-opt max-size=10m --log-opt max-file=3`。要看每个文件的明细，把 `log_level` 改成 `DEBUG`。
- **容器显示 unhealthy**：用 `docker inspect --format '{{json .State.Health}}' upilot-mini` 看原因。如果是 `stalled: main loop heartbeat …`，说明程序卡住了，`docker restart upilot-mini` 即可，状态都在库里。
- **分块上传断网抖一下就等半分钟**：不会。单个分块失败会原地重试 3 次（2/4/8 秒），只重传失败那一块。
- **cookie 过期了会怎样**：程序会暂停向 115 发请求，每 5 分钟只放 1 个文件去试探，面板上有红色提示，文件不会被标成「错误」。在「115 账号」换上新 cookie，下一轮自动恢复。
- **面板提示「115 暂时限制访问」**：被限流或风控了，不是文件的问题，也不会加重视试次数。过几分钟到一小时会自动再试，一般不用改设置。如果经常出现，可以把 `upload.init_min_interval` 调大一点，或把 `upload.concurrency` 降到 1。
- **状态库备份**：每 24 小时自动备份一份 `state.db.bak`。库丢了，把它改名回 `state.db` 就行，省得重算全部 SHA1。
- **115 换了 OSS 区域，分块上传连不上**：在 `config.yaml` 的 `upload` 下加 `oss_endpoint: "https://oss-cn-xxx.aliyuncs.com"`，默认是深圳。
- **状态怎么看**：浏览器打开 `http://<host>:9844/`，或 `curl -s localhost:9844/api/status`，或直接查 `/config/state.db` 的 `files` 表。
- **想临时停一下上传**：界面上点「暂停调度」，在途任务跑完就停；点「恢复调度」继续。
- **想重新传某个文件**：删库里那行（`sqlite3 /config/state.db "delete from files where path like '%xxx%'"`），下一轮扫描会重新入队。
- **权限**：容器以 uid 1000 运行；挂载目录只读即可，`/config` 需要可写。

## 9. 本地（不进容器）跑测试

```bash
pip install PyYAML ruff                # 离线测试只需要 PyYAML（115 客户端全部用替身）；Python >= 3.12
ruff check app                         # 与 CI 同一套规则（ruff.toml：F + B）
python tests/test_flow.py              # 80 个离线用例，约 50 秒；CI 在构建镜像之前跑同一套
```

要装完整运行依赖（真跑服务）：

```bash
pip install -r requirements.txt -c constraints.txt   # constraints.txt 锁死全部子依赖
```
