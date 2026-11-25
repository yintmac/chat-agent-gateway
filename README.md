# Chat Agent Gateway

Chat Agent Gateway 连接 Lark 和 Codex CLI，支持私聊交办任务、续聊和实时进度。它也能查询 OpenProject、转发工单更新和 Mattermost 提醒，并提供 macOS 任务监视窗口。

## 日常使用

- 在 Lark 私聊机器人，直接发文字需求。后续消息沿用当前对话，重启服务后也能继续。发 `新对话` 可以另开话题，也可以在同一条消息里写 `新对话 帮我看一下……`。
- 在私聊问「查一下 OP 上我有哪些未关闭工单」，机器人会实时查询分配给你的工单，并列出编号、状态和链接。
- 在同一个私聊点击顶部置顶的「任务进度」，查看当前步骤和耗时、排队顺序，以及最近 5 个完成或受阻的任务。看板会原地更新，并显示最后更新时间。也可以发送 `进度` 查找入口。
- OpenProject 工单更新和 Mattermost 的 @ 会发来提醒。需要处理时，使用 Lark 的「回复」引用那条提醒，写清要求，例如「先解释这次状态变更」。机器人在你的原消息下回结果。回复以前的对话消息可以切回那个话题。
- 双击项目里的 **Chat Agent Gateway.app** 打开 Python 小窗口。窗口显示后台进程是否在运行、排队数、当前任务耗时和后端实际报告的步骤。默认跟随当前任务，也可以选最近的任务查看记录或勾选「置顶」。关闭窗口后后台继续运行。
- 升级前已经发出的审批卡可以直接回复，也可以继续使用原来的按钮。旧卡仍支持 `确认 审批1 同事邮箱`，收件人须在应用可用范围内。

## 安装与运行

需要 macOS、Python 3.11 或更高版本、uv 和已登录的 Codex CLI。在仓库目录安装锁定的依赖并创建本机配置。

```sh
uv sync --locked
install -m 600 config.example.json config.local.json
```

在 `config.local.json` 填写自己的设置。此文件已被 Git 忽略。可用 `CHAT_AGENT_GATEWAY_CONFIG` 指定其他配置文件。文件须由当前用户持有，权限可设为 `600`。

| 字段 | 用途 |
| --- | --- |
| `openproject.origin`、`user_id`、`api_key` | OpenProject 的 HTTPS 站点、数字用户 ID 和个人 API Key。启动时会核对登录身份。 |
| `mattermost.origin`、`email` | 可选。配置 Mattermost 的 HTTPS 站点和登录邮箱后，再绑定会话。 |
| `workspace_root` | 可选。填入供 Codex 查阅的工作区目录，例如 `~/projects`。填了相对路径时，以配置文件所在目录为基准。 |
| `workspace_branches` | 可选。用「仓库目录名 → 分支名」指定工作树，例如 `{"demo-project": "main"}`。 |

按下一节绑定 Lark 机器人后，安装并启动当前用户的 LaunchAgent。

```sh
.venv/bin/python manage.py install
.venv/bin/python manage.py status
```

首次开启看板置顶时，执行 `.venv/bin/python -m chat_agent_gateway lark pin-progress`，再按下段重启服务。Lark 只允许更新最近 14 天发出的卡片，网关会在到期前置顶新卡片，再移除旧卡片的置顶。发现看板发布结果未确认时，先在私聊核对是否已经收到新卡片。

改完代码并通过测试后，执行 `.venv/bin/python manage.py restart` 加载新版。`stop` 停止当前服务，`start` 再次启动。停服前会检查是否有任务正在发提醒、调用执行后端或回传结果。遇到这三种情况会显示任务号，等任务完成后再操作。

会话、任务和步骤保存在 `~/Library/Application Support/Chat Agent Gateway/lark/approvals.sqlite3`。同目录还有 Lark 绑定、OpenProject 检查进度和日志。启动与连接记录在 `service.stdout.log`，错误在 `service.stderr.log`。进程意外中断后，如启动时报告未完成的发送或执行，请先核对 Lark 投递或本地改动。窗口只读访问本机状态。也可以使用 `.venv/bin/python -m chat_agent_gateway.monitor` 启动窗口，Python 需支持 Tk。

## 创建和绑定机器人

1. 在 [Lark 开发者后台](https://open.larksuite.com/app) 创建企业自建应用，添加机器人。开通「以应用的身份发消息」(`im:message:send_as_bot`) 和「读取用户发给机器人的单聊消息」(`im:message.p2p_msg:readonly`)。
2. 在服务目录打开 Mac 终端，执行下面的绑定命令。App Secret 在终端内输入，输入时不会显示。

   ```sh
   .venv/bin/python -m chat_agent_gateway lark pair
   ```

3. 保持绑定程序运行。在 Lark 后台的「事件与回调」下，分别给「事件配置」和「回调配置」选择长连接。添加「接收消息」(`im.message.receive_v1`) 和新版「卡片回传交互」(`card.action.trigger`)。应用可用范围先选你自己，发布版本。
4. 在 Lark 里找到机器人，私聊发送终端给出的「绑定 …」。绑定后，应用凭据保存在 Mac 的 `~/Library/Application Support/Chat Agent Gateway/lark/app.json`，文件权限为 `600`。

[配置事件长连接](https://open.larksuite.com/document/ukTMukTMukTM/uYDNxYjL2QTM24iN0EjN/event-subscription-configure-/use-websocket) · [配置按钮回调长连接](https://open.larksuite.com/document/uAjLw4CM/ukTMukTMukTM/event-subscription-guide/callback-subscription/configure-callback-request-address)

## 测试和切换

如要手动验证飞书卡片，先停止本机 Lark 后台服务，再运行 `check`。收到测试卡后点「确认」或「取消」，然后启动后台服务。测试卡只验证收发。

```sh
.venv/bin/python manage.py stop
.venv/bin/python -m chat_agent_gateway lark check
.venv/bin/python manage.py start
```

只有从旧版 Mattermost 审批切换到 Lark 的机器才需要执行迁移。在两个服务都停下后，可以把待审批的 OpenProject 工单迁移到 Lark。每条待办会在 Lark 重新发一张提醒，原编号和 OpenProject 检查进度会保留。已批准、发送中或正在处理的任务需要先核对。

```sh
.venv/bin/python -m chat_agent_gateway lark migrate
```

日常用 `.venv/bin/python manage.py start` 启动已安装的服务。OpenProject 每 60 秒检查一次。只有绑定账号的私聊文字能启动新任务，同一条消息重复到达只入队一次。要切回 Mattermost，需先核对两边的待办和 OpenProject 检查进度。

## 接收 Mattermost 的 @

在 Mac 终端用个人账号登录一次，按提示输入 Mattermost 密码和验证码。

```sh
.venv/bin/python -m chat_agent_gateway lark bind-mm
```

会话凭据存在 Mac 钥匙串。后台服务会自动加载，Mattermost 会话失效后重新运行 `bind-mm`。别人直接 @你的新消息会发成 Lark 提醒，点「看原消息」可打开 Mattermost。提醒中只有一个 OpenProject 工单号时，你回复提醒后会读取关联工单。监听断开后会重连，离线期间的消息需要在 Mattermost 核对。

## 渠道和后端

启动命令分别指定后端和渠道。本机的后台服务使用下面这组参数。需要在前台调试时，先停止后台服务，再在终端运行它。

```sh
.venv/bin/python -m chat_agent_gateway --backend codex lark run
```

`lark` 提供私聊对话与提醒。`mattermost` 子命令保留原有的自聊审批入口，两个入口共用单实例锁。

后端提供异步函数 `run_job(request, *, progress=None)`，返回结果文本和是否受阻。对话任务提供按时间排序的历史、当前用户指令和引用的提醒。后端可以通过 `progress(步骤名称)` 上报实际进度。后台按接收顺序逐条执行。要增加后端，在 `backends/` 实现这个函数，并在启动入口的 `BACKENDS` 注册。要增加聊天工具，在 `channels/` 实现收发逻辑并注册子命令。

## 开发和验证

| 要改的部分 | 文件 |
| --- | --- |
| Lark 提醒、私聊收发、旧卡回调 | [channels/lark.py](chat_agent_gateway/channels/lark.py) |
| 对话归属、消息去重、历史和进度事件 | [conversations.py](chat_agent_gateway/conversations.py) |
| 本机监视窗口 | [monitor.py](chat_agent_gateway/monitor.py) |
| Lark 进度卡片和共用进度查询 | [channels/lark_progress.py](chat_agent_gateway/channels/lark_progress.py)、[progress.py](chat_agent_gateway/progress.py) |
| Codex 调用 | [backends/codex.py](chat_agent_gateway/backends/codex.py) |
| 仓库和工作树定位 | [workspaces.py](chat_agent_gateway/workspaces.py) |
| 渠道和后端的启动入口 | [__main__.py](chat_agent_gateway/__main__.py) |
| OpenProject 工单更新和任务执行 | [openproject_watch.py](chat_agent_gateway/openproject_watch.py)、[workflow.py](chat_agent_gateway/workflow.py) |
| 审批状态、去重、结果投递记录 | [approval.py](chat_agent_gateway/approval.py) |
| Mattermost 的 @ 提醒 | [mattermost_watch.py](chat_agent_gateway/channels/mattermost_watch.py) |
| macOS 服务安装和启停 | [manage.py](manage.py) |

在服务目录运行测试。测试使用临时配置、模拟消息和临时数据库。修改依赖时更新 `pyproject.toml`，运行 `uv lock` 并一并提交 `uv.lock`。

```sh
.venv/bin/python -m unittest discover -s tests -t .
ruff check .
ruff format --check .
```
