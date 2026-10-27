# TG 频道 → B 站动态

当前默认只使用 TG 和 B 站，X 已从默认同步规则中停用。

| TG 消息或图片说明 | 发布结果 |
| --- | --- |
| `今天做了个小工具` | 只留在 TG |
| `今天做了个小工具 #b` | 同时发布到 B 站动态 |

在文字里任何位置带上 ` #b`（空格加 #b）就会发布到 B 站，也支持大写 `#B`。同步到 B 站时会去掉标记，TG 保留原文。
标记必须是完整的标签：`#battle`、`#b站` 这类后面紧连文字的不算。图片相册会合并，等最后收到的图片稳定 2 秒再处理。
只写标记且没有文字或图片时，不发布空动态。
原项目的 `/x` 结尾标记仍表示只留在来源平台，与 X 的接入无关。

## 管理页面

页面与 Bot 在同一进程运行，不需要单独构建前端。

- 账号状态：查看 TG 和 B 站最近状态，单独检测或检测全部账号。
- Cookie 检测：通过登录接口判断 B 站 Cookie 当前是否有效，区分已失效和网络异常。
- 同步记录：按成功、失败、待处理筛选，搜索正文，查看完整消息、失败原因、尝试次数和动态链接。
- 手动重试：只对失败的目标排队，立即唤醒发送进程，沿用已成功片段的发送断点。
- 密码登录：账号 Token 和 Cookie 不会返回给页面。
- 账号与配置：B 站 APP 扫码登录、手动更新 Cookie、连接 TG Bot 与频道，以及修改同步标记和轮询间隔。

页面每 10 秒刷新本地记录，不会因此请求 B 站。主动点击“检测账号”才会进行登录检测。
B 站网页接口没有可靠的 Cookie 失效日期，页面展示的是最近一次检测结果。
Cookie 失效后，进入“账号与配置”重新扫码登录即可，也支持手动填写 Cookie。
账号与规则保存后立即生效，不需要重启服务。凭证只保存在服务器，页面不回填已保存的 Token 和 Cookie。
扫码登录流程参考 bili-sync，见 [第三方说明](THIRD_PARTY_NOTICES.md)。

## Docker 部署

```bash
git clone https://github.com/nothingness0db/be-everywhere-bot.git
cd be-everywhere-bot
cp .env.example .env
```

编辑 `.env`，设置至少 12 个字符的 `DASHBOARD_PASSWORD`，然后启动：

```bash
sh ./manage.sh up -d --build
```

默认管理页地址为 `http://127.0.0.1:8080`。登录后进入“账号与配置”，连接 B 站账号和 TG 频道。
页面与同步进程在同一个容器运行，账号配置保存在 `data/be_everywhere.db`。
`DASHBOARD_BIND`、`DASHBOARD_PORT` 可调整监听地址与端口。
远程服务器可以通过 SSH 转发访问，例如：

```bash
ssh -L 8080:127.0.0.1:8080 user@your-server
```

通过 HTTPS 反代访问时，保留原始 Host 并传递 `X-Forwarded-Proto`，
将 `FORWARDED_ALLOW_IPS` 设置为容器实际看到的反代来源 IP。

常用管理命令：

```bash
sh ./manage.sh ps
sh ./manage.sh logs -f --tail=100 bot
sh ./manage.sh restart bot
sh ./manage.sh exec bot uv run python main.py --list-accounts
```

也可以在终端连接账号：

```bash
sh ./manage.sh run --rm bot uv run python main.py --auth=telegram --label=main
sh ./manage.sh run --rm bot uv run python main.py --auth=bilibili --label=main
```

同一个 TG Bot 不要同时由多个程序实例读取。

### 部署到挂载盘

可以将源码放在挂载盘目录中运行。若挂载盘使用 NTFS 等无法设置私有文件权限的文件系统，
建议把 `data` 和 `.env` 放到支持私有权限的用户目录，再通过符号链接放回项目目录。
备份时请备份链接指向的实际数据文件。

`sh ./manage.sh` 会自动加载项目内的 `docker-compose.override.yml`（如果存在）。
该文件属于服务器本地配置，已从 Git 和 Docker 构建上下文中排除，更新源码时请保留。

## TG 账号配置

1. 向 [@BotFather](https://t.me/BotFather) 创建 Bot，获取 Bot Token。
2. 将 Bot 加到你的频道，并设为管理员。
3. 打开管理页 →“账号与配置”→“Telegram 频道”，填写 Bot Token 和频道标识，例如 `@yourchannel` 或 `-100…`。账号名称默认填 `main`，这是本地配置名称，不是 Bot 的 `@用户名`。
4. 点击“验证并保存 TG 配置”。程序会检查频道类型、管理员权限和 webhook 冲突，成功后立即生效。
5. 在频道发布一条新消息进行验证。

更新已有账号时，Token 留空表示继续使用已保存的值；接收进度会保留。
连接不同频道时请使用新的账号名称，避免把旧频道的同步记录关联到新频道。
终端 `--auth=telegram` 仍可使用。

程序使用 [Telegram Bot API 的 getUpdates](https://core.telegram.org/bots/api#getupdates)，无需个人 TG 账号登录。
如果已有 webhook，需要先停用原 webhook 接收方式。Bot API 不提供完整频道历史，
未被程序收取的更新最多保留 24 小时。

## B 站账号配置

使用自己的登录 Cookie 和 B 站网页端动态接口，目前支持纯文字和图片动态。

推荐在管理页 →“账号与配置”→“B 站账号”点击“扫码登录”，用 B 站 APP 扫码并在手机确认。
二维码有效期约 3 分钟，过期后可刷新；关闭弹窗会取消此次登录。确认后由服务器读取并验证登录信息，
成功后直接保存凭证，前端只收到账号名称与状态。重新登录时保留原账号名称。

也可以展开“手动填写 Cookie”：

1. 在浏览器登录自己的 B 站账号。
2. 从浏览器开发者工具的 Cookie 面板读取 `SESSDATA`、`bili_jct`，以及可选的 `buvid3`。
3. 在页面填写 Cookie 并点击“验证并保存 Cookie”；或运行 `--auth=bilibili` 在终端输入。
4. 程序读取登录信息确认账号，然后将凭证保存在本地数据库。

Cookie 失效或需要验证时，可以在页面重新扫码登录，或更新手动填写的 Cookie。
网页端接口可能变化，参考实现：[动态发布接口调用源码](https://docs.rs/bpi-rs/latest/src/bpi_rs/dynamic/publish.rs.html)。

单条动态最多 2000 字符、9 张图片，超出会拆成独立动态。视频和 GIF 暂不支持发布到 B 站，
含这些媒体的消息会保留失败记录，不会丢掉媒体后只发文字。

## 自定义标记

在“账号与配置”→“同步规则”里修改 B 站同步标记、消息检查间隔与相册等待时间，点击保存立即生效。
前端配置保存在数据库里，容器重建或服务器重启后仍会恢复，优先于 `routing.toml`。
默认规则仍可通过 `routing.toml` 配置：

```toml
mode = "telegram_prefix"
poll_interval_seconds = 5
album_settle_seconds = 2

[prefixes]
b = ["bilibili"]
```

表里的词就是标记词，发消息时写 `#b`（前面留个空格，位置不限）即可触发。
文件中也可以用 `bilibili/main` 指向某个 `--label` 对应的账号。
前端保存规则使用 TG 按标记同步到所有已连接 B 站账号的模式；复杂的文件规则会被此次保存替换。
没有标记的消息始终不会触发跨平台发布。只有显式改为 `mode = "mesh"`，
才会启用原项目的多平台互相同步模式。

## 来源标记

从 TG 发布到 B 站的动态，第一行会写上“转自tg频道”，空一行再接原文，读者一眼能看出这条是从 TG 搬过来的。
文案在 `config.py` 的 `ATTRIBUTION_TEMPLATES` 里配置，`{network}` 是来源平台名，`{source}` 是来源账号名；
把值改成空字符串就关闭。只有第一条消息带标记，长文拆成多条时不会每条都重复。

```python
ATTRIBUTION_TEMPLATES = {"bilibili": "转自 {network}"}   # 发布后：转自tg频道
ATTRIBUTION_TEMPLATES = {"bilibili": "转自 {source}"}    # 发布后：转自 频道标题
ATTRIBUTION_TEMPLATES = {"bilibili": ""}                 # 不写标记
```

## 直接运行 Python


```bash
uv sync
export DASHBOARD_PASSWORD='请替换为你自己的至少12字符密码'
uv run python main.py --dashboard --host 127.0.0.1 --port 8080
```

## 状态和重试

程序在确认 TG 更新前保存消息，失败记录会自动重试，也可以在页面手动重试。
重启后会恢复中断任务和待执行的手动重试。手动重试使用最近一次失败时保存的正文。
修改 TG 消息只影响尚未发送的内容，不会编辑已经发布的动态。

外部平台已经发布成功、但连接在返回 ID 前断开时，仍无法完全排除重复发布。
数据库包含账号凭证、消息缓存、同步记录和发送断点，目前没有自动清理。
请备份实际的数据目录，删除数据库也会删除去重状态。

代码验证：

```bash
uv sync --dev
uv run pytest -q
```
