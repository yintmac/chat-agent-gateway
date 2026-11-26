# Chat Agent Gateway

Chat Agent Gateway connects chat platforms to AI agents so you can assign tasks, continue conversations, and track progress from chat. It ships with Lark and Mattermost channels and a Codex CLI backend. You can install additional chat channels and agent backends as Python packages.

## Using the gateway

The built-in Lark bot uses Chinese labels and commands. The examples below show the exact text to send.

- Send a task as a text message in your private chat with the Lark bot. Follow-up messages stay in the current conversation, including after a restart. Send `新对话` (new conversation) to start a new topic, or put the command and your request in one message, such as `新对话 Summarize this issue`.
- With OpenProject configured, send `查一下 OP 上我有哪些未关闭工单` (list my open OpenProject work packages). The bot queries OpenProject and returns the IDs, statuses, and links for open work packages assigned to you.
- Open the pinned **任务进度** (Task Progress) card to see the current step, elapsed time, queue order, and up to five recent tasks that finished or hit a blocker. The card updates in place and shows when it last refreshed. Send `进度` (progress) to find it.
- With the relevant integrations enabled, OpenProject updates and Mattermost @mentions appear as notices in Lark. Use **Reply** on a notice and say what you want done, for example, “Explain this status change.” The bot replies to your message. Replying to an older conversation message brings that topic back into focus.
- On macOS, double-click **Chat Agent Gateway.app** to open the desktop monitor. On Windows, run the monitor command in the setup section below. The monitor shows whether the gateway is running, the queue size, elapsed time, and steps reported by the backend. It follows the current task by default. You can select a recent task or keep the window on top. The gateway continues running when you close the monitor.
- You can still use the buttons on existing approval cards or reply to the card with a request. To send the result of an old approval to a colleague, use a command such as `确认 审批1 colleague@example.com`. The recipient must be within the app's availability scope.

## Installation and startup

Install Python 3.11 or later and [uv](https://docs.astral.sh/uv/). Install and sign in to your chosen agent client as required by its backend. On Windows, the Codex backend requires native `codex.exe` on your `PATH`.

From the repository root, install the locked dependencies.

```sh
uv sync --locked
```

You can use Lark with an agent without creating a configuration file. To enable OpenProject, Mattermost, or workspace routing, copy [config.example.json](config.example.json) to `config.local.json` and fill in the settings you need. Git ignores this file. Use `CHAT_AGENT_GATEWAY_CONFIG` to select a different file. On macOS, run `chmod 600 config.local.json`. On Windows, keep the file in a private user directory and restrict its ACL to your user account, SYSTEM, and local administrators.

| Setting | Purpose |
| --- | --- |
| `openproject.origin`, `openproject.user_id`, `openproject.api_key` | Optional for Lark. Supply the HTTPS site, numeric user ID, and personal API key. The gateway verifies the account at startup. The Mattermost self-chat approval channel requires OpenProject. |
| `mattermost.origin`, `mattermost.email` | Optional. Set the HTTPS site and login email before binding a Mattermost session. |
| `workspace_root` | Optional. Set the directory containing repositories available to Codex, for example `~/projects`. Relative paths resolve from the configuration file's directory. |
| `workspace_branches` | Optional. Map repository directory names to branches to select worktrees, for example `{"demo-project": "main"}`. |

After pairing the Lark bot, run the gateway in a terminal on macOS or Windows. To open the desktop monitor, run the second command in a separate terminal with a Python installation that includes Tk.

```sh
uv run --locked --no-sync python -m chat_agent_gateway --backend codex lark run
uv run --locked --no-sync python -m chat_agent_gateway.monitor
```

To stop a foreground process, wait for active tasks and deliveries to finish, then press Ctrl+C.

### macOS background service

To start the gateway automatically at login, install a LaunchAgent for your macOS user.

```sh
.venv/bin/python manage.py install
.venv/bin/python manage.py status
```

Use `.venv/bin/python manage.py restart` to reload after code changes. Use `stop` and `start` to control the installed service. If the service is posting a notice, running a task, or delivering a reply, the stop command reports the task ID. Wait for that task before trying again.

### Pinned progress card

Run `uv run --locked --no-sync python -m chat_agent_gateway lark pin-progress`, then restart the gateway. Lark allows card updates for 14 days after posting. The gateway pins a replacement before the current card expires, then unpins the old one. If the gateway reports an unconfirmed card delivery, check the private chat for the new card.

### Local state and logs

The gateway stores state under the current user's account.

| Platform | State directory |
| --- | --- |
| macOS | `~/Library/Application Support/Chat Agent Gateway` |
| Windows | `%LOCALAPPDATA%\Chat Agent Gateway` |

Lark conversations, tasks, and progress events are stored in `lark/approvals.sqlite3` under the state directory. The `lark` directory also contains the bot binding and OpenProject checkpoints. When run as a macOS LaunchAgent, the gateway writes startup and connection messages to `lark/service.stdout.log` and errors to `lark/service.stderr.log`. The desktop monitor reads local state without changing it.

If startup reports a task interrupted during execution or delivery, check the local changes or Lark chat to determine what happened.

## Creating and pairing a Lark bot

1. Create a custom app in the Lark Developer Console and add a bot. Grant permission to send messages as the bot (`im:message:send_as_bot`) and to read private messages sent to it (`im:message.p2p_msg:readonly`). [Lark Developer Console](https://open.larksuite.com/app)
2. From the repository root, run the pairing command. The terminal hides the App Secret as you enter it.

   ```sh
   uv run --locked --no-sync python -m chat_agent_gateway lark pair
   ```

3. Keep the pairing command running. In **Events & Callbacks** in the Developer Console, select the long-connection option for both events and callbacks. Add the **Receive messages** event (`im.message.receive_v1`) and the **Card action trigger** callback (`card.action.trigger`). Set the app's availability to your own account and publish the version. [Event connection setup](https://open.larksuite.com/document/ukTMukTMukTM/uYDNxYjL2QTM24iN0EjN/event-subscription-configure-/use-websocket) · [Callback connection setup](https://open.larksuite.com/document/uAjLw4CM/ukTMukTMukTM/event-subscription-guide/callback-subscription/configure-callback-request-address)
4. Open a private chat with the bot and send the pairing message printed in the terminal, including the `绑定` (pair) prefix. The gateway saves the binding in `lark/app.json` under the state directory.

## Checking the connection and switching channels

To test card delivery and callbacks, wait for active tasks to finish, stop the gateway, and run the check below. In Lark, click **确认** (Confirm) or **取消** (Cancel) on the test card, then start the gateway again.

```sh
uv run --locked --no-sync python -m chat_agent_gateway lark check
```

If you're switching from the legacy Mattermost approval workflow to Lark, stop both services and migrate pending OpenProject approvals. The gateway posts a fresh Lark notice for each pending item and preserves the approval IDs and OpenProject checkpoints. Review approved, running, or sending tasks before migrating.

```sh
uv run --locked --no-sync python -m chat_agent_gateway lark migrate
```

When configured, the gateway checks OpenProject every 60 seconds. The Lark channel accepts new conversation requests only as private text messages from the paired account. Repeated delivery of the same message queues it once. If you switch back to Mattermost, check the pending tasks and OpenProject checkpoints on both sides.

## Receiving Mattermost mentions in Lark

After configuring Mattermost, run the binding command in a terminal. Sign in with your Mattermost account and enter the verification code if prompted.

```sh
uv run --locked --no-sync python -m chat_agent_gateway lark bind-mm
```

The gateway stores the session in macOS Keychain or Windows Credential Manager and loads it automatically. Run `bind-mm` again when the session expires. New messages that directly mention you appear as Lark notices. Click **看原消息** (View original message) to open Mattermost. If the notice references exactly one OpenProject work package, the gateway reads that work package when you reply to the notice. The listener reconnects after a disconnect. Check Mattermost for messages received while it was offline.

## Channels and agent backends

Select the backend with `--backend` and the chat platform with the channel subcommand. The foreground startup example uses `codex` and `lark`. Lark provides private conversations and notices. The `mattermost` channel provides the legacy self-chat approval workflow. Both channels share a single-instance lock.

### Installing adapters

Install adapters as separate Python packages in the gateway's virtual environment. A chat platform needs an interface for sending and receiving messages with verifiable sender identities. An agent needs a callable CLI, API, or SDK. The gateway discovers adapters through Python entry points and lets you choose a channel and a backend independently.

```toml
[project.entry-points."chat_agent_gateway.channels"]
example-chat = "example_chat.adapter"

[project.entry-points."chat_agent_gateway.backends"]
example-agent = "example_agent.backend:run_job"
```

- A channel module exposes `configure_parser(parser)` and `start(args, *, run_job)`. It receives messages, verifies the sender and their authorization, deduplicates messages by ID, calls the backend, and delivers replies.
- An agent backend exposes an asynchronous `run_job(request, *, progress=None)` and returns a pair containing the result text and a boolean indicating whether the task was blocked. Conversation requests include the current instruction, quoted context, and chronological history. The backend can report steps with `progress("Reading the workspace", detail="Optional local detail")`.

Install the adapter after running `uv sync --locked`. Use `--no-sync` to keep separately installed adapters available when you run the gateway.

```sh
uv pip install ./example-adapter
uv run --locked --no-sync python -m chat_agent_gateway --backend example-agent example-chat --help
```

## Development

| Area | Source |
| --- | --- |
| Lark notices, private messages, and approval callbacks | [channels/lark.py](chat_agent_gateway/channels/lark.py) |
| Conversation routing, deduplication, history, and progress events | [conversations.py](chat_agent_gateway/conversations.py) |
| Desktop monitor | [monitor.py](chat_agent_gateway/monitor.py) |
| Lark progress card and shared progress queries | [channels/lark_progress.py](chat_agent_gateway/channels/lark_progress.py), [progress.py](chat_agent_gateway/progress.py) |
| Codex backend | [backends/codex.py](chat_agent_gateway/backends/codex.py) |
| Repository and worktree selection | [workspaces.py](chat_agent_gateway/workspaces.py) |
| Channel and backend selection | [__main__.py](chat_agent_gateway/__main__.py) |
| OpenProject updates and task execution | [openproject_watch.py](chat_agent_gateway/openproject_watch.py), [workflow.py](chat_agent_gateway/workflow.py) |
| Approvals, deduplication, and delivery records | [approval.py](chat_agent_gateway/approval.py) |
| Mattermost mentions | [channels/mattermost_watch.py](chat_agent_gateway/channels/mattermost_watch.py) |
| macOS service management | [manage.py](manage.py) |

Run these checks from the repository root. The tests use temporary configuration and databases with mock messages. When dependencies change, update `pyproject.toml`, run `uv lock`, and commit the updated `uv.lock`.

```sh
uv run --locked --no-sync python -m unittest discover -s tests -t .
ruff check .
ruff format --check .
```
