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

## 2. 拿 115 cookie（三种方式）

**方式 A：面板里扫码（推荐，有界面就用它）**

打开 Web 界面 → 设置页 →「115 扫码登录」卡片 → 选「登录槽位」→ 点「获取二维码」→
手机 115 官方 App 扫一扫 → 手机上确认。状态变「已登录」后 cookie 自动写进
`/config/config.yaml` 并重建 115 客户端 —— 不用手抄、不用重启。

登录槽位就是 `p115.app`：二维码按该端申请，登录出来的 cookie 也归该端，**所以挑一个自己
平时不用的端，扫码不会把手机上正在用的 115 / 网页版挤下线**。下拉里是中文标签，并按「能不能出二维码」
排序（下面是 115 当前的真实返回，面板里不可扫码的会标注「115 已不支持扫码」）：

**能扫码**（115 返回真 PNG）

| 槽位 | 对应客户端 |
| --- | --- |
| `115ipad` / `ipad` | 115（iPad端·旧版 / iPad端）—— **默认**，多数人平时不在 iPad 上用 115 |
| `115android` / `115ios` | 115（Android端·旧版 / iOS端·旧版） |
| `tv` / `apple_tv` | 115网盘（Android电视端 / Apple电视端） |
| `wechatmini` / `alipaymini` | 115生活（微信小程序 / 支付宝小程序） |
| `harmony` | 115（鸿蒙端） |
| `web` | 网页版 |
| `windows` / `mac` / `linux` | Windows / macOS / Linux 电脑端 |

115 目前对 `qandroid`、`qios`、`qipad`、`android`、`ios`、`bandroid` 这 6 个槽位不再返回二维码图片，
**所以下拉里不列它们**（只留上面 13 个能扫的）。老 `config.yaml` 里如果还写着这些值，扫码时面板会
直接告诉你去换一个，不会让你白点。

**方式 B：命令行扫码**

```bash
docker compose run --rm upilot-mini login                      # 槽位取 config.yaml 的 p115.app
docker compose exec upilot-mini python /app/app/main.py login  # 在运行中的容器里做
```

终端里出二维码，用 115 App 扫；成功后 cookie 写入 `/config/config.yaml`，然后 `docker compose restart`。

**方式 C：直接填**
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
| `p115.cookie` / `p115.app` | — / `115ipad` | 115 凭据与登录槽位（扫码与上传都用它，挑平时不用的端；可选值见 §2） |
| `paths.source` | `/media/source` | 容器内监控目录 |
| `paths.target_root` | `/UPILOT-mini` | 没被 `paths.map` 命中的文件落在这里，不存在会自动创建 |
| `paths.map` | `[]` | 本机相对路径 → 115 目录的映射表（可改名、大小写照写），见 §5「目录映射」 |
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
| GET | `/login` `/logout` | 站内登录页与退出（`mode: form` 时启用） |
| GET | `/api/tasks` | 任务明细（`status` / `q` / `limit` / `offset`） |
| GET | `/api/task` | 单个文件详情（`?path=`） |
| POST | `/api/task` | 单文件操作（`retry` / `retry-now` / `upload-now` / `delete`） |
| GET | `/api/config` | 设置页表单结构（密钥只回脱敏状态） |
| POST | `/api/config` | 保存设置（写回 config.yaml 并立即生效） |
| GET | `/api/logs` | 最近的运行日志（内存环形缓冲，不落盘） |
| GET | `/static/*` | 前端静态资源（白名单：app.css / app.js） |

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

### 115 扫码登录（面板里直接扫）

设置页顶部有「115 扫码登录」卡片：**先选「登录槽位」**（就是 `p115.app`）→ 点「获取二维码」→
用手机 115 官方 App 扫一扫 → 手机上确认，状态会从「等待扫码」变「已登录」，cookie 自动写进配置
并重建 115 客户端。二维码约 5 分钟有效，过期点「刷新二维码」重来。

- **槽位可选**（下拉 19 个中文选项，见 §2 的实测表）就是为了这件事：扫码身份按槽位申请，登录出来的
  cookie 也归该端。例如选 `115(iPad端·旧版)` 或 `115网盘(Android电视端)`，就不会顶掉你手机上正在用的
  115 App / 网页版。槽位下拉改完立刻存进配置，下一次「获取二维码」即生效。
- 下拉里只放**实测能出二维码**的 13 个槽位；`qandroid` 等 6 个已被 115 停用，不出现在列表里。
- 115 的状态接口是长轮询：有状态变化立刻返回，空闲时会挂住；面板给 5 秒读超时
  （底层还会重试，一次轮询约十几秒），所以扫码后最多十几秒页面上就会显示「已登录」。
- 命令行方式仍然可用：`docker compose run --rm upilot-mini login`（终端里显示二维码）。

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
