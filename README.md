# 采购公告监控后端

Python 3.11+，SQLite 持久化，BeautifulSoup 解析 HTML，Flask 提供带密码登录的管理界面。已实现中国政府采购网搜索列表采集，其他三个站点通过独立采集器扩展。提供 Web 管理、CLI、后台工作进程和 QQ 机器人通知适配层。

## Web 管理界面

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m procurement_watch web
```

打开 http://127.0.0.1:8000 。首次在本机访问时设置至少 10 个字符的管理密码，以后输入密码登录。默认只监听本机；不提供默认账号或默认密码。页面左侧文字 Logo 为“消毒供应采集系统”，提供“采集任务”和“推送配置”菜单，默认进入“采集任务”。

界面支持：采集概览、采集状态和失败原因、关键词增删、分钟/小时/天频率设置、暂停/恢复自动采集、后台手动采集、公告搜索/匹配筛选/分页、详情和原文链接、运行日志、按当前筛选导出 CSV。页面每 5 秒刷新任务状态；修改规则立即保存到原来的 SQLite 数据库。公告列表中的分页仅用于查看本地历史记录，爬虫仍只请求网站第一页。

`web` 自带单个采集工作线程，无须同时运行 `run`。两个工作进程共用文件锁，会阻止重复启动。暂停仅影响后续自动采集，不会取消正在执行的一轮；暂停时仍允许手动采集和已有通知队列重试。CLI `run` 也会遵循 Web 保存的暂停设置。

## Docker Compose 部署

有源码时直接运行：

```sh
docker compose up -d --build
docker compose logs app
```

访问 `http://服务器IP:8000`。首次启动日志会显示“首次初始化验证码”，在网页中填写验证码并创建管理密码，即可进入系统。验证码不在网页中返回，设置密码后不能再次初始化；尚未初始化就重启容器会生成新的验证码。

无需 `.env`、机器人环境变量、配置文件挂载或其他业务启动参数。默认每 **1 分钟**采集第一页；关键词、采集频率和推送配置在 Web 中管理。现有本地项目的采集频率也已调整为 1 分钟；后续升级不会覆盖在 Web 保存的新频率。

容器使用 Waitress 生产 WSGI 服务，以普通用户运行，监听 8000。`monitor-data` 命名卷挂载到 `/app/data`，保留 SQLite、管理密码、会话密钥和机器人令牌解密密钥。重建/更新容器保留设置；不要在保留数据的部署中删除该卷。未设置管理员密码之前，不自动采集或发送通知。

镜像仅打包源码和 `config.example.toml`；不会携带本机 `config.toml`、数据库、登录密码或生产机器人令牌。新部署需要在 Web“推送配置”中填写自己的机器人信息。`/healthz` 提供无业务数据的健康检查。

停止与更新：

```sh
docker compose down
docker compose up -d --build
```

重置容器中的管理密码（在终端交互输入）：

```sh
docker compose exec app python -m procurement_watch web-password
```

## GitHub Actions 发布到 Docker Hub

工作流：`.github/workflows/docker-publish.yml`。先在 Docker Hub 创建 `supply-monitor` 仓库，再在 GitHub 仓库的 **Settings → Secrets and variables → Actions** 添加两个 Repository secrets：

| 名称 | 内容 |
| --- | --- |
| `DOCKERHUB_USERNAME` | Docker Hub 用户名 |
| `DOCKERHUB_TOKEN` | 具有对应仓库写入权限的 Docker Hub Access Token |

镜像名固定为 `<Docker Hub 用户名>/supply-monitor`。工作流仅在推送 **`Vx.x.x`** 版本标签时触发（大写 `V`，三段数字，例如 `V1.2.3`），先测试再构建并上传 `linux/amd64`、`linux/arm64` 镜像。普通分支推送、Pull Request、其他格式的标签和手动操作不触发此工作流。

发布示例：

```sh
git tag V1.2.3
git push origin V1.2.3
```

每次版本发布更新 `latest` 标签；`V1.2.3` 标签生成 `1.2.3`、`1.2` 镜像标签，同时保留提交 SHA 标签。工作流先移除大写 `V` 再生成镜像版本，部署文件指向该版本镜像。工作流参考 [Docker 官方 GitHub Actions 流程](https://docs.docker.com/guides/gha/)，提供构建缓存、SBOM 和 provenance。容器验收会验证首次密码初始化、登录、静态资源、1 分钟默认间隔、镜像不含生产凭据，以及重启后设置持久化；测试期间暂停采集，不连接生产采集站点或 QQ 机器人。

发布成功后，在 Actions 该次运行的 **Artifacts → docker-compose-deployment** 下载 `compose.hub.yaml`。其中已填好实际 Docker Hub 镜像名和本次发布标签，不需要源码或环境变量：

```sh
docker compose -f compose.hub.yaml up -d
docker compose -f compose.hub.yaml logs app
```

使用同一个文件更新镜像时运行 `docker compose -f compose.hub.yaml pull`，然后重新 `up -d`。版本标签固定的文件会保持该版本；升级到新版本可下载新一次发布生成的文件，命名卷保持不变。

## 本地密码管理与代理部署

重置密码：

```powershell
.\.venv\Scripts\python.exe -m procurement_watch web-password
```

密码用 PBKDF2-SHA256 加盐哈希存储，不保存明文；会话 Cookie 使用 HttpOnly / SameSite=Strict，12 小时过期；写接口验证 CSRF；连续 5 次错误登录按来源 IP 暂停登录 15 分钟。重置密码会使已有会话失效。直接运行本地 `web` 命令时，首次设置只允许绑定本机；对外监听前必须先在终端设置密码。容器入口则使用前述一次性初始化验证码，无须提前配置密码。

```powershell
.\.venv\Scripts\python.exe -m procurement_watch web --host 0.0.0.0 --port 8000
```

此命令使用 Flask 本地开发服务器。正式对外部署应采用 WSGI 服务和 HTTPS 反向代理；HTTPS 环境启用 `secure_cookie=True`，单层可信代理时启用 `trust_proxy=True`。本地调试对应参数为 `--secure-cookie --trust-proxy`，此时必须先在终端设置密码。代理须保留原始 Host，覆盖并传递正确的 `X-Forwarded-Proto`；只在上游访问被限制到可信代理时启用代理信任。应用不直接信任转发的 IP 或 Host 请求头。

## 快速运行

在项目根目录执行（Windows PowerShell）：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item config.example.toml config.toml
.\.venv\Scripts\python.exe -m procurement_watch init
.\.venv\Scripts\python.exe -m procurement_watch once
.\.venv\Scripts\python.exe -m procurement_watch list --limit 20
.\.venv\Scripts\python.exe -m procurement_watch run
```

`once` 立即执行一次；`run` 持续运行，每 5 秒检查是否到达下次采集时间，Ctrl+C 退出。配置路径可通过全局参数 `--config 路径` 指定，放在子命令前。数据库路径相对于配置文件目录解析，默认 `data/watch.db`。所有时间字段带时区：公告时间为北京时间，采集和调度状态为 UTC。

当前环境已准备 `.venv` 并导入本地 HTML 中的 20 条公告，可直接执行 `list` 查看。

## 关键词和频率

```powershell
.\.venv\Scripts\python.exe -m procurement_watch keyword add "电子内窥镜"
.\.venv\Scripts\python.exe -m procurement_watch keyword add "医院"
.\.venv\Scripts\python.exe -m procurement_watch keyword list
.\.venv\Scripts\python.exe -m procurement_watch keyword remove "医院"
.\.venv\Scripts\python.exe -m procurement_watch schedule 1 minutes
.\.venv\Scripts\python.exe -m procurement_watch schedule 2 hours
.\.venv\Scripts\python.exe -m procurement_watch schedule 1 days
.\.venv\Scripts\python.exe -m procurement_watch status
.\.venv\Scripts\python.exe -m procurement_watch list --keyword "医院" --limit 10
```

关键词按标题的子串匹配，多个关键词为“任意一个命中”，忽略大小写，不使用正则表达式；空关键词禁止保存。首次初始化从 TOML 导入关键词和频率，此后以 SQLite 中的管理配置为准，修改 TOML 不覆盖已保存的关键词/频率。可以在 `run` 运行期间从另一个终端调整，下一轮自动生效。修改频率会让所有来源下次立即采集。

采集频率目前对所有来源统一设置，每个来源各自记录执行时间。添加关键词只影响后续新公告，不自动补发历史公告。已入队通知保留入队时命中的关键词，即使删除关键词也不会撤回待发送记录。

## 采集与增量规则

- 保存标题、完整链接、发布时间、采购人、代理机构、公告类型、地区、品目、摘要，以及首次/最近发现时间。以 `(source_id, url)` 唯一去重。
- 首次成功采集建立基线，默认不发送历史公告。需要首次就通知时，在 TOML 设置 `notify_on_first_run = true`，且须在该来源首次成功采集前设置。
- 后续采集的新 URL 如果标题匹配，才入通知队列；已知 URL 更新标题或其他字段只更新数据库，不重新通知。页面中暂时消失的记录保留，重新出现也不重复推送。
- 每轮固定采集第一页，成功后公告、通知队列和采集状态在同一事务提交。采集失败写入运行记录，不清空旧数据、不更新成功时间、不建立首次基线。
- HTTP 200 也可能是拦截页。频繁访问/验证页、结果结构变化、异常空列表、无效公告项均作为采集失败。只在明确显示“共找到 0 条”时接受空结果。限流响应不会立即重试；网络错误和 HTTP 5xx 按指数间隔有限重试。整轮失败后至少等待 5 分钟或一个完整采集周期（取较长值）。
- 请求中的 `page_index` 固定为 `1`，即使配置 URL 指向其他页也会自动改为第一页；不跟随翻页链接，旧配置中的 `max_pages` 不再生效。每轮比较第一页全部结果，只推送首次发现且标题匹配的公告。如果两次采集间新公告已被挤到第二页，可能漏报，可根据更新量提高采集频率。
- `date_mode = "rolling"` 每轮以北京时间当天为结束日期，向前查 `lookback_days` 天；`fixed` 原样保留提供 URL 的日期。示例配置默认 rolling，避免固定在 2026 年的查询范围而停止发现新公告。地区条件 `zoneId=33 not 3302`、公告筛选等保留。

2026-10-27 实际请求目标 URL 时，站点返回 HTTP 200 的“您的访问过于频繁”页面，未取得实时搜索结果。仓库已有的 `website-1.html` 包含 20 条完整结果，解析验证基于该文件。程序不绕过站点验证。

离线导入（不联网、不直接发消息，会更新来源基线）：

```powershell
.\.venv\Scripts\python.exe -m procurement_watch import-html website-1.html --source ccgp-zhejiang
```

## QQ 机器人通知

进入左侧“推送配置”，填写 NapCat 完整接口 URL（如 `http://127.0.0.1:3000/send_group_msg`）、QQ群组 ID 和 Access Token。令牌只填写值，不带 `Bearer ` 前缀。保存不会额外发送测试消息；自动推送关闭时，新增匹配记录仍会入队。打开“自动推送”并保存后，工作进程会处理包括已积累记录在内的待发送队列，页面会显示队列数量。

Web 保存的 URL、群号、推送开关和令牌优先于 TOML 默认值，`web`、`run`、`once` 和 `notify` 共用并动态读取，无需重启工作进程。访问令牌使用 Fernet 加密保存，读取配置接口不返回明文或密文；表单留空保留已有令牌，填写新值替换，勾选“清除已保存的令牌”后不再发送鉴权头。未在 Web 设置令牌时仍兼容 `token_env` 指定的环境变量。

数据库旁的 `<数据库文件名>.push.key` 是令牌解密密钥。备份和迁移时需要同时保留数据库及密钥文件，并保护两者的访问权限；缺失密钥时可在界面重新填写令牌恢复。

“发送测试消息”只使用已保存配置，独立于自动推送开关，不消费公告队列。点击后会向目标群发送一条明确标记的测试消息，展示并保存成功/失败结果；有未保存的修改时需要先保存。正式环境的 URL、群号和令牌已在本机保存，当前自动推送关闭，尚未调用生产接口。

查看纯文字消息，不调用 API、不修改发送状态：

```powershell
.\.venv\Scripts\python.exe -m procurement_watch notify --preview
```

默认预留 **OneBot HTTP `send_group_msg`** 契约，配置举例：

```toml
[notification]
enabled = true
url = "http://127.0.0.1:3000/send_group_msg"
token_env = "QQ_BOT_TOKEN"
group_id = "123456789"
max_items = 5
max_chars = 3500
timeout_seconds = 15
retry_seconds = 60
success_mode = "onebot"
```

```powershell
$env:QQ_BOT_TOKEN = "你的访问令牌"
.\.venv\Scripts\python.exe -m procurement_watch notify
```

请求：`POST application/json`，正文 `{"message_type":"group","group_id":"123456789","message":"纯文字内容","auto_escape":true}`；有令牌时添加 `Authorization: Bearer ...`。兼容 NapCat 的 OneBot HTTP 接口，默认同时检查 HTTP 2xx、JSON `status = "ok"`、`retcode = 0`。拒绝跟随接口重定向，避免向其他目标转发令牌。若自建接口只通过 HTTP 状态表达成功，可以在 TOML 使用 `success_mode = "http"`。其他请求格式/鉴权/成功条件在 `notifications.QQWebhook.send()` 适配。

消息按公告发布时间从新到旧排列，包含标题、时间、采购人、代理机构、类别、地区、命中词和独立一行的完整 URL。每条消息最多 `max_items` 条，并按 `max_chars` 控制长度；剩余信息分批发送，不截断丢弃。过长的单条记录会保留队列并记录错误，需提高字符上限。QQ群客户端是否把 URL 识别为可点击、是否交由默认浏览器打开，由 QQ 客户端设置决定，后端不会在服务器上打开浏览器。

发送成功才标记已发送，发送失败记录错误和尝试次数，至少间隔 `retry_seconds` 重试。SQLite 队列跨重启保留。文件锁阻止多个工作进程同时采集/发送。通知采用“至少一次”交付：若机器人已收到但响应丢失，或程序在成功调用后、保存发送状态前退出，重试可能重复发消息。要做到接收端去重，需要真实机器人支持幂等键后再对接。

## 目录与扩展

```text
procurement_watch/
  collectors/
    base.py          # 采集器抽象接口
    ccgp.py          # 政府采购网第一页查询与 HTML 解析策略
    __init__.py      # 策略注册表与工厂
  config.py          # TOML 读取、参数校验
  models.py          # 公告模型、频率单位
  http.py            # HTML 请求、超时、重试
  storage.py         # SQLite、事务、持久化状态、工作进程锁
  service.py         # 采集任务编排
  notifications.py   # 纯文字排版、队列发送、QQ 适配器
  push_config.py     # Web/CLI 共用推送设置、校验、令牌加密与脱敏
  cli.py             # 管理命令、持久化调度循环
  web.py             # 登录、CSRF、管理 API、后台采集线程
  container.py       # 固定配置的容器入口
  templates/         # 采集任务、推送配置、登录页及共用侧栏
  static/            # 本地 CSS / JavaScript，无前端构建依赖
tests/test_backend.py
tests/test_web.py
tests/test_push.py
tests/test_deployment.py
scripts/smoke_container.py
scripts/render_hub_compose.py
Dockerfile
compose.yaml
.github/workflows/docker-publish.yml
config.example.toml
website-1.html       # 用户提供的真实 HTML 样本
```

增加站点：继承 `Collector` 实现 `collect()` 和 `parse()`；返回统一 `Article`，在 `COLLECTORS` 注册新类型，再添加 `[[sources]]` 配置。不同站点的请求、分页、选择器互不影响，存储/匹配/通知逻辑复用。来源 ID 是数据命名空间，应保持稳定；不要让相同 ID 指向完全不同的站点/查询。

Web 管理界面调用原有业务层和存储层，后台线程每次通过独立的 SQLite 连接执行采集。Flask 请求按请求创建和关闭连接。当前面向单管理员、单工作进程部署；后续采用多进程 WSGI 时，应把调度分离为独立 `run` 进程并单独挂载不带 worker 的 `create_app()`。

SQLite 表：`articles` 公告、`keywords` 关键词、`settings` 管理设置、`sources` 来源状态、`crawl_runs` 运行记录、`outbox` 通知队列。数据库使用 WAL 和外键。备份运行中的数据库时应使用 SQLite backup API；或停止程序后复制数据库。

## 验证

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

测试使用临时 SQLite 和本机临时 HTTP 服务，不向真实 QQ 群发消息，覆盖真实 HTML 字段、只请求第一页、URL 参数保留、限流/结构异常、事务回滚、重启去重、关键词/频率持久化、失败状态、消息拆分、发送重试、工作进程锁、NapCat/OneBot 请求与 Bearer 鉴权、拒绝重定向、令牌加密/替换/清除、推送配置实时生效和显式测试消息，以及 Web 登录/退出、CSRF、密码重置、错误登录限流、筛选/导出和后台采集触发。
