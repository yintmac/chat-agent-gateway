import asyncio
import atexit
import json
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from lark_oapi.api.im.v1 import P2ImMessageReceiveV1
from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTrigger
from lark_oapi.ws import client as lark_ws

from chat_agent_gateway.channels import lark as pilot
from chat_agent_gateway.approval import Approvals, Reply, Request
from chat_agent_gateway.channels import lark_progress
from chat_agent_gateway.channels.mattermost_watch import enqueue_mention
from chat_agent_gateway.notify import (
    MATTERMOST,
    OPENPROJECT,
    OPENPROJECT_USER_ID,
    BridgeError,
    write_state,
)

OWNER = "ou_owner"
INBOX = "lark-inbox"
POST_ID = "om_approval"


@atexit.register
def close_test_websocket_loop():
    # lark-oapi creates its event loop on import, even when transport is mocked.
    if not lark_ws.loop.is_running() and not lark_ws.loop.is_closed():
        lark_ws.loop.close()


def callback(action, *, owner=OWNER, post_id=POST_ID, diagnostic=""):
    return P2CardActionTrigger(
        {
            "event": {
                "operator": {"open_id": owner},
                "context": {"open_message_id": post_id},
                "action": {"value": {"decision": action, "diagnostic": diagnostic}},
            }
        }
    )


def direct_message(
    text,
    *,
    owner=OWNER,
    chat_type="p2p",
    chat="oc_dm",
    message="om_user",
    parent=None,
    root=None,
    sender_type="user",
):
    return P2ImMessageReceiveV1(
        {
            "event": {
                "sender": {"sender_type": sender_type, "sender_id": {"open_id": owner}},
                "message": {
                    "chat_type": chat_type,
                    "message_type": "text",
                    "chat_id": chat,
                    "message_id": message,
                    "parent_id": parent,
                    "root_id": root,
                    "content": json.dumps({"text": text}),
                },
            }
        }
    )


class LarkPilotTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name)
        self.approvals = Approvals(self.path / "approved.sqlite", OWNER, INBOX)
        self.source = f"{OPENPROJECT}/work_packages/123"
        self.request = Request("op:1", "ticket:123", self.source, "Ticket #123", "", "")
        self.messages = Mock()
        self.messages.send.side_effect = [POST_ID, "om_result"]
        self.messages.reply.return_value = "om_reply"
        self.run_job = AsyncMock(return_value=("已核对。", False))
        self.op = Mock()
        self.pilot = pilot.LarkChannel(
            self.op,
            self.messages,
            self.approvals,
            self.path / "cursor",
            run_job=self.run_job,
        )

    def pending_legacy_card(self):
        task = self.approvals.enqueue(self.request)
        self.approvals.start_approval(task)
        self.approvals.record_approval_scope(task, None, auto_route=True)
        self.approvals.approval_posted(task, POST_ID)
        return task

    def test_progress_command_is_private_and_does_not_enqueue_work(self):
        for ignored in (
            direct_message("进度", owner="ou_other"),
            direct_message("进度", chat_type="group"),
            direct_message("进度", message=None),
        ):
            self.pilot.receive_message(ignored)
        self.pilot.board.refresh()
        self.messages.send_progress_card.assert_not_called()

        self.messages.send_progress_card.return_value = ("om_board", 123)
        command = direct_message(" 进度 ", parent="om_old")
        self.pilot.receive_message(command)
        self.pilot.receive_message(command)
        self.pilot.board.refresh()
        self.messages.send_progress_card.assert_called_once()
        args, kwargs = self.messages.send_progress_card.call_args
        self.assertEqual((args[0], kwargs["uuid"]), ("oc_dm", "om_user-progress"))
        self.assertTrue(args[1]["config"]["update_multi"])
        self.assertFalse(args[1]["config"]["enable_forward"])
        self.assertEqual(self.approvals.tasks_in("approved"), [])

        restarted = lark_progress.ProgressBoard(self.approvals, self.messages)
        restarted.request("oc_dm", "om_later")
        restarted.refresh()
        self.messages.send_progress_card.assert_called_once()
        self.messages.update_card.assert_called_once()
        self.assertEqual(self.messages.update_card.call_args.args[0], "om_board")
        self.assertIn(
            "openChatId=oc_dm&position=123", self.messages.reply.call_args.args[1]
        )
        restarted.refresh()
        self.messages.update_card.assert_called_once()

    def test_owner_openproject_query_reads_all_pages_and_replies_without_codex(self):
        next_page = "/api/v3/work_packages?offset=2"

        def page(ticket_id, next_path=None):
            return {
                "total": 2,
                "_links": {"nextByOffset": {"href": next_path}} if next_path else {},
                "_embedded": {
                    "elements": [
                        {
                            "id": ticket_id,
                            "subject": f"Ticket {ticket_id}",
                            "_links": {
                                "assignee": {
                                    "href": f"/api/v3/users/{OPENPROJECT_USER_ID}"
                                },
                                "status": {"title": "In progress"},
                            },
                        }
                    ]
                },
            }

        self.op.request.side_effect = [page(10, next_page), page(20)]
        incoming = direct_message("查一下op 上有我什么没关闭的ticket")
        self.pilot.receive_message(incoming)
        self.pilot.receive_message(incoming)
        asyncio.run(self.pilot.execute())
        self.pilot.deliver()
        self.run_job.assert_not_called()
        self.assertEqual(self.op.request.call_count, 2)
        self.assertEqual(self.op.request.call_args.args, (next_page,))
        self.messages.reply.assert_called_once()
        root, text = self.messages.reply.call_args.args
        self.assertEqual(root, "om_user")
        self.assertIn("2 条未关闭工单", text)
        for number in (10, 20):
            self.assertIn(f"#{number} Ticket {number} · In progress", text)
            self.assertIn(f"{OPENPROJECT}/work_packages/{number}", text)

    def test_openproject_query_failure_returns_an_error_without_codex(self):
        self.op.request.side_effect = BridgeError("OpenProject 请求超时")
        self.pilot.receive_message(
            direct_message("我还有哪些未关闭的 OpenProject 工单？")
        )
        asyncio.run(self.pilot.execute())
        self.pilot.deliver()
        self.run_job.assert_not_called()
        self.messages.reply.assert_called_once_with(
            "om_user", "这次处理卡住了。OpenProject 请求超时"
        )

    def test_openproject_query_reports_lark_limit_without_returning_a_partial_list(
        self,
    ):
        self.pilot.receive_message(direct_message("OpenProject 上我名下的未关闭工单"))
        with (
            patch.object(pilot, "LARK_TEXT_LIMIT_BYTES", 150),
            patch.object(
                pilot, "assigned_open_ticket_report", return_value=("工单\n" * 150, 80)
            ),
        ):
            asyncio.run(self.pilot.execute())
        self.pilot.deliver()
        self.run_job.assert_not_called()
        self.messages.reply.assert_called_once_with(
            "om_user",
            "这次处理卡住了。OpenProject 查到 80 条未关闭工单，完整列表超过 Lark 单条文本上限。",
        )

    def test_openproject_query_cannot_be_triggered_by_other_users_or_mixed_commands(
        self,
    ):
        question = "OP 上我名下的未关闭工单"
        self.pilot.receive_message(direct_message(question, owner="ou_other"))
        self.pilot.receive_message(
            direct_message("查一下 OP 上我名下未关闭的工单，再关闭它们")
        )
        asyncio.run(self.pilot.execute())
        self.op.request.assert_not_called()
        self.run_job.assert_awaited_once()

    def test_board_updates_during_execution_and_preserves_result_delivery(self):
        self.messages.send_progress_card.return_value = ("om_board", 123)

        async def scenario():
            running = asyncio.Event()
            finish = asyncio.Event()

            async def run_job(request, *, progress):
                progress("正在运行测试")
                running.set()
                await finish.wait()
                return "请核对失败项。", True

            self.pilot.run_job = run_job
            self.pilot.receive_message(direct_message("检查项目", message="om_task"))
            worker = asyncio.create_task(self.pilot.execute())
            try:
                await asyncio.wait_for(running.wait(), 3)
                self.pilot.receive_message(direct_message("下一条", message="om_next"))
                self.pilot.receive_message(direct_message("进度"))
                await asyncio.to_thread(self.pilot.board.refresh)
                _, card = self.messages.send_progress_card.call_args.args
                active, queued, recent, *_ = card["elements"]
                self.assertIn("检查项目", active["text"]["content"])
                self.assertIn("正在运行测试", active["text"]["content"])
                self.assertIn("已用", active["text"]["content"])
                self.assertIn("排队中 · 1\n#2 下一条", queued["text"]["content"])
                self.assertTrue(recent["text"]["content"].endswith("暂无"))
                self.assertFalse(worker.done())
            finally:
                finish.set()
                await worker

            self.pilot.deliver()
            await asyncio.to_thread(self.pilot.board.refresh)
            self.messages.reply.assert_called_once_with("om_task", "请核对失败项。")
            message, card = self.messages.update_card.call_args.args
            self.assertEqual(message, "om_board")
            self.assertIn("检查项目 · 受阻", card["elements"][2]["text"]["content"])
            self.assertIn("最后更新", card["elements"][-1]["elements"][0]["content"])

        asyncio.run(scenario())

    def test_board_caps_visible_queue_but_counts_all_jobs(self):
        for i in range(8):
            self.pilot.receive_message(
                direct_message(f"需求 {i}", message=f"om_msg{i}")
            )
        self.messages.send_progress_card.return_value = ("om_board", None)
        self.pilot.receive_message(direct_message("进度"))
        self.pilot.board.refresh()
        card = self.messages.send_progress_card.call_args.args[1]
        queue = card["elements"][1]["text"]["content"].splitlines()
        self.assertEqual(queue[0], "排队中 · 8")
        self.assertEqual(queue[1:6], [f"#{i + 1} 需求 {i}" for i in range(5)])
        self.assertEqual(queue[6:], ["还有 3 个未展开"])

    def test_expired_or_deleted_board_is_only_replaced_after_an_owner_command(self):
        self.messages.send_progress_card.side_effect = [
            ("om_first", 123),
            ("om_second", 124),
            ("om_third", 125),
        ]
        self.pilot.board.request("oc_dm", "om_first_command")
        with patch.object(lark_progress, "time", return_value=1000):
            self.pilot.board.refresh()
        with patch.object(
            lark_progress,
            "time",
            return_value=1000 + lark_progress.CARD_LIFETIME_SECONDS,
        ):
            self.pilot.board.refresh()
            self.messages.send_progress_card.assert_called_once()
            self.messages.update_card.assert_not_called()
            self.pilot.board.request("oc_dm", "om_second_command")
            self.pilot.board.refresh()
            self.assertEqual(self.pilot.board.post.message, "om_second")

            self.messages.update_card.side_effect = (
                lark_progress.ProgressCardUnavailable()
            )
            self.pilot.receive_message(direct_message("看下这个项目"))
            self.pilot.board.refresh()
            self.assertIsNone(self.pilot.board.post)
            self.assertEqual(self.messages.send_progress_card.call_count, 2)
            self.pilot.board.request("oc_dm", "om_third_command")
            self.pilot.board.refresh()
            self.assertEqual(self.pilot.board.post.message, "om_third")

    def test_transient_patch_failure_keeps_the_same_card_and_does_not_resend(self):
        self.messages.send_progress_card.return_value = ("om_board", 123)
        self.pilot.board.request("oc_dm", "om_first_command")
        self.pilot.board.refresh()
        self.messages.update_card.side_effect = BridgeError("卡片请求超时")
        self.pilot.board.request("oc_dm", "om_second_command")
        self.pilot.board.refresh()
        self.assertEqual(self.pilot.board.post.message, "om_board")
        self.messages.send_progress_card.assert_called_once()
        self.assertIn("没能刷新", self.messages.reply.call_args.args[1])
        self.pilot.board.refresh()
        self.messages.update_card.assert_called_once()

    def test_pinned_board_is_published_without_a_chat_command_and_rotates(self):
        board = lark_progress.ProgressBoard(self.approvals, self.messages, pin=True)
        self.messages.send_owner_progress_card.side_effect = [
            ("oc_dm", "om_first", 1),
            ("oc_dm", "om_second", 2),
        ]
        with patch.object(lark_progress, "time", return_value=1000):
            board.refresh()
        self.messages.pin_progress_card.assert_called_once_with("om_first")
        self.messages.unpin_progress_card.assert_not_called()
        self.messages.reply.assert_not_called()

        board = lark_progress.ProgressBoard(self.approvals, self.messages, pin=True)
        rotation = (
            1000
            + lark_progress.CARD_LIFETIME_SECONDS
            - lark_progress.PIN_ROTATION_LEAD_SECONDS
        )
        with patch.object(lark_progress, "time", return_value=rotation - 1):
            board.refresh()
        self.messages.send_owner_progress_card.assert_called_once()
        self.messages.pin_progress_card.assert_called_once()
        with patch.object(lark_progress, "time", return_value=rotation):
            board.refresh()
        self.assertEqual(
            [call.args[0] for call in self.messages.pin_progress_card.call_args_list],
            ["om_first", "om_second"],
        )
        self.messages.unpin_progress_card.assert_called_once_with("om_first")
        restored = lark_progress.ProgressBoard(self.approvals, self.messages, pin=True)
        self.assertEqual(
            (restored.post.message, restored.pinned_message), ("om_second", "om_second")
        )

    def test_pin_failure_keeps_the_existing_card_for_next_attempt(self):
        self.messages.send_owner_progress_card.return_value = ("oc_dm", "om_first", 1)
        self.messages.pin_progress_card.side_effect = [BridgeError("网络超时"), None]
        first = lark_progress.ProgressBoard(self.approvals, self.messages, pin=True)
        first.refresh()
        self.assertIsNone(first.pinned_message)
        self.assertEqual(first.post.message, "om_first")

        restarted = lark_progress.ProgressBoard(self.approvals, self.messages, pin=True)
        restarted.refresh()
        self.messages.send_owner_progress_card.assert_called_once()
        self.assertEqual(restarted.pinned_message, "om_first")

    def test_pinned_card_ambiguous_send_is_not_repeated_after_restart(self):
        self.messages.send_owner_progress_card.side_effect = BridgeError("发送超时")
        first = lark_progress.ProgressBoard(self.approvals, self.messages, pin=True)
        first.refresh()
        self.assertTrue(first.publish_pending)
        restarted = lark_progress.ProgressBoard(self.approvals, self.messages, pin=True)
        restarted.refresh()
        self.messages.send_owner_progress_card.assert_called_once()
        self.messages.pin_progress_card.assert_not_called()

    def test_pin_and_unpin_use_only_the_given_board_message(self):
        adapter = pilot.LarkMessages(
            {"app_id": "cli_mock", "app_secret": "mock", "owner": OWNER}
        )
        resource = adapter.client.im.v1.pin
        with (
            patch.object(resource, "create") as create,
            patch.object(resource, "delete") as delete,
        ):
            create.return_value.data.pin.message_id = "om_new"
            adapter.pin_progress_card("om_new")
            adapter.unpin_progress_card("om_old")
            self.assertEqual(create.call_args.args[0].request_body.message_id, "om_new")
            self.assertEqual(delete.call_args.args[0].message_id, "om_old")

    def test_notice_displays_source_and_only_an_owner_message_starts_work(self):
        summary = "#123 工单标题\n\n工单正文和更新记录"
        update = "状态 从 新建 改为 进行中"
        task = self.approvals.enqueue(
            replace(self.request, summary=summary, update=update)
        )
        self.pilot.deliver()
        kind, card = self.messages.send.call_args.args
        self.assertEqual(kind, "interactive")
        visible, panel, note, controls = card["elements"]
        self.assertEqual(visible["text"]["content"], f"#123 工单标题\n\n{update}")
        self.assertEqual(panel["tag"], "collapsible_panel")
        self.assertFalse(panel["expanded"])
        self.assertEqual(panel["elements"][0]["text"]["content"], "工单正文和更新记录")
        buttons = controls["actions"]
        self.assertIn("回复", note["elements"][0]["content"])
        self.assertEqual([button["text"]["content"] for button in buttons], ["看工单"])
        self.assertEqual(buttons[0]["url"], self.source)
        self.assertEqual(self.approvals.tasks_in("notified"), [task])
        self.assertEqual(self.pilot.receive(callback("确认")).toast.type, "warning")
        asyncio.run(self.pilot.execute())
        self.run_job.assert_not_called()
        incoming = direct_message("先解释这次状态变化", parent=POST_ID)
        for ignored in (
            direct_message("外人", owner="ou_other", parent=POST_ID),
            direct_message("群里", chat_type="group"),
            direct_message("机器人", sender_type="app"),
            direct_message("   "),
            direct_message("缺少消息编号", message=None),
        ):
            self.pilot.receive_message(ignored)
        self.assertEqual(self.approvals.tasks_in("approved"), [])
        self.pilot.receive_message(incoming)
        self.pilot.receive_message(incoming)
        asyncio.run(self.pilot.execute())
        self.run_job.assert_awaited_once()
        request = self.run_job.call_args.args[0]
        self.assertEqual(request["summary"], "先解释这次状态变化")
        self.assertEqual(request["reference"], f"{summary}\n\n{update}")
        self.assertIsNone(request["workspace"])
        self.pilot.deliver()
        self.messages.reply.assert_called_once_with("om_user", "已核对。")
        self.assertEqual(self.approvals.tasks_in("discussed"), [task])

    def test_legacy_card_buttons_still_check_owner_and_run_once(self):
        task = self.pending_legacy_card()
        for event in (
            callback("确认", owner="ou_someone_else"),
            callback("确认", post_id="om_wrong"),
            callback("确认", diagnostic="test"),
            callback(["invalid"]),
        ):
            self.assertEqual(self.pilot.receive(event).toast.type, "warning")
        asyncio.run(self.pilot.execute())
        self.run_job.assert_not_called()

        self.assertEqual(self.pilot.receive(callback("确认")).toast.type, "success")
        self.assertEqual(self.pilot.receive(callback("确认")).toast.type, "warning")
        asyncio.run(self.pilot.execute())
        self.run_job.assert_awaited_once()
        self.assertEqual(
            self.run_job.call_args.args[0]["summary"], self.request.summary
        )
        self.assertTrue(self.run_job.call_args.args[0]["auto_route"])
        self.pilot.deliver()
        self.messages.send.assert_called_with(
            "text", {"text": "审批1 · 工单 #123\n\n已核对。"}, recipient=None
        )
        self.assertEqual(self.approvals.tasks_in("sent"), [task])

    def test_legacy_short_body_stays_visible_and_long_body_is_folded(self):
        for length in (120, 121, 2001):
            with self.subTest(length=length):
                card = pilot.ticket_card(
                    1,
                    {"summary": f"#123 标题\n\n{'内' * length}", "source": self.source},
                )
                if length == 120:
                    self.assertEqual(len(card["elements"]), 3)
                    self.assertIn("内" * length, card["elements"][0]["text"]["content"])
                else:
                    text = card["elements"][1]["elements"][0]["text"]["content"]
                    expected = (
                        "内" * length
                        if length == 121
                        else "内" * 2000 + "\n\n剩余内容见工单。"
                    )
                    self.assertEqual(text, expected)

    def test_cancelled_card_cannot_be_approved_afterwards(self):
        task = self.pending_legacy_card()
        self.pilot.receive(callback("取消"))
        self.pilot.receive(callback("确认"))
        asyncio.run(self.pilot.execute())
        self.run_job.assert_not_called()
        self.assertEqual(self.approvals.tasks_in("cancelled"), [task])

    def test_mention_notifies_once_and_loads_ticket_only_on_reply(self):
        person = {"id": "a" * 26, "username": "operator"}
        record = {
            "id": "b" * 26,
            "user_id": "c" * 26,
            "channel_id": "d" * 26,
            "message": "@operator 麻烦处理 #123",
        }
        event = {
            "event": "posted",
            "data": {"post": json.dumps(record), "mentions": "[]"},
        }
        enqueue_mention(event, person, self.approvals)
        self.assertEqual(self.approvals.tasks_in("queued"), [])
        event["data"]["mentions"] = json.dumps([person["id"]])
        enqueue_mention(event, person, self.approvals)
        enqueue_mention(event, person, self.approvals)
        self.assertEqual(self.approvals.tasks_in("queued"), [1])
        self.pilot.deliver()
        _, card = self.messages.send.call_args.args
        self.assertIn("Mattermost @你 · #123", card["elements"][0]["text"]["content"])
        link = card["elements"][-1]["actions"][-1]
        self.assertEqual(link["text"]["content"], "看原消息")
        self.assertEqual(link["url"], f"{MATTERMOST}/_redirect/pl/{record['id']}")

        asyncio.run(self.pilot.execute())
        self.op.request.assert_not_called()
        self.run_job.assert_not_called()
        self.op.request.return_value = {"id": 123, "subject": "工单标题"}
        self.pilot.receive_message(direct_message("解释这条消息", parent=POST_ID))
        asyncio.run(self.pilot.execute())
        self.op.request.assert_called_once_with("/api/v3/work_packages/123")
        self.assertIn("#123 工单标题", self.run_job.call_args.args[0]["reference"])
        self.assertEqual(self.run_job.call_args.args[0]["summary"], "解释这条消息")

    def test_mattermost_request_without_ticket_can_be_discussed(self):
        request = replace(
            self.request,
            event_id="mm:post",
            topic="mm:post",
            source=f"{MATTERMOST}/_redirect/pl/post",
            summary="@operator 帮我核对一下发布文档",
        )
        self.approvals.enqueue(request)
        self.pilot.deliver()
        self.pilot.receive_message(direct_message("帮我看看", root=POST_ID))
        asyncio.run(self.pilot.execute())
        self.op.request.assert_not_called()
        self.assertEqual(self.run_job.call_args.args[0]["reference"], request.summary)

    def test_only_owner_can_choose_a_colleague_in_the_bot_dm(self):
        task = self.pending_legacy_card()
        command = f"确认 审批{task} teammate@example.com"
        for message in (
            direct_message(command, owner="ou_other"),
            direct_message(command, chat_type="group"),
            direct_message(f"确认 审批{task} invalid@email"),
        ):
            self.pilot.receive_message(message)
        self.assertEqual(self.approvals.tasks_in("awaiting"), [task])
        self.pilot.receive_message(direct_message(command))
        self.pilot.receive(callback("确认"))
        asyncio.run(self.pilot.execute())
        self.run_job.assert_awaited_once()
        self.pilot.deliver()
        self.messages.send.assert_called_with(
            "text",
            {"text": "审批1 · 工单 #123\n\n已核对。"},
            recipient="teammate@example.com",
        )

    def test_ambiguous_send_is_not_automatically_retried(self):
        task = self.approvals.enqueue(self.request)
        self.messages.send.side_effect = BridgeError("connection closed")
        with self.assertRaises(BridgeError):
            self.pilot.deliver()
        self.pilot.deliver()
        self.messages.send.assert_called_once()
        self.assertEqual(self.approvals.tasks_in("posting"), [task])

    def test_new_notices_deduplicate_event_ids_and_preserve_distinct_updates(self):
        first = self.approvals.enqueue(self.request)
        self.pilot.deliver()
        self.assertEqual(self.approvals.enqueue(self.request), first)
        later = self.approvals.enqueue(
            replace(self.request, event_id="op:2", update="截止日期调整")
        )
        self.assertNotEqual(first, later)
        self.pilot.deliver()
        self.assertEqual(self.approvals.tasks_in("notified"), [first, later])
        self.run_job.assert_not_called()

    def test_conversation_replies_keep_comparisons_and_do_not_activate_mentions(self):
        self.run_job.return_value = (
            'x < 5，泛型 List<T>；<at user_id="all">所有人</at>',
            False,
        )
        self.pilot.receive_message(direct_message("解释这条表达式"))
        asyncio.run(self.pilot.execute())
        self.pilot.deliver()
        self.messages.reply.assert_called_once_with(
            "om_user", 'x < 5，泛型 List<T>；＜at user_id="all">所有人＜/at>'
        )

    def test_cutover_preserves_cursor_and_blocks_if_mattermost_has_pending_task(self):
        source = self.path / "mattermost"
        source.mkdir()
        target = self.path / "lark"
        target.mkdir()
        write_state(source / "op-cursor.json", {"cursor": 12})
        previous = Approvals(source / "approvals.sqlite3", "mm", "mm-chat")
        task = previous.enqueue(self.request)
        with patch.object(pilot, "MATTERMOST_STATE", source):
            with self.assertRaisesRegex(BridgeError, "审批1"):
                pilot.import_checkpoint(target)
            self.assertFalse((target / "op-cursor.json").exists())
            previous.start_approval(task)
            previous.approval_posted(task, "mm-message")
            previous.accept_reply(Reply("mm", "mm-chat", "mm-message", "取消"))
            pilot.import_checkpoint(target)
            self.assertEqual(
                json.loads((target / "op-cursor.json").read_text()), {"cursor": 12}
            )
            write_state(target / "op-cursor.json", {"cursor": 24})
            pilot.import_checkpoint(target)
            self.assertEqual(
                json.loads((target / "op-cursor.json").read_text()), {"cursor": 24}
            )

    def test_sdk_supports_registered_callback_and_message_handler(self):
        (
            pilot.dispatcher()
            .register_p2_card_action_trigger(self.pilot.receive)
            .register_p2_im_message_receive_v1(self.pilot.receive_message)
            .build()
        )
        adapter = pilot.LarkMessages(
            {"app_id": "cli_mock", "app_secret": "mock", "owner": OWNER}
        )
        with patch.object(adapter.client.im.v1.message, "reply") as reply:
            reply.return_value.data.message_id = "om_result"
            self.assertEqual(adapter.reply("om_user", "核对完成"), "om_result")
            request = reply.call_args.args[0]
            self.assertEqual(request.message_id, "om_user")
            self.assertEqual(request.request_body.msg_type, "text")
            self.assertEqual(
                json.loads(request.request_body.content), {"text": "核对完成"}
            )

    def test_sdk_creates_interactive_card_and_patches_the_same_message(self):
        adapter = pilot.LarkMessages(
            {"app_id": "cli_mock", "app_secret": "mock", "owner": OWNER}
        )
        endpoint = adapter.client.im.v1.message
        card = lark_progress.progress_card(((), (), (), 0, 0), 1000, "oc_dm")
        with (
            patch.object(endpoint, "create") as create,
            patch.object(endpoint, "patch") as update,
        ):
            create.return_value.data = Mock(message_id="om_board", message_position=123)
            self.assertEqual(
                adapter.send_progress_card("oc_dm", card, uuid="test-board"),
                ("om_board", 123),
            )
            request = create.call_args.args[0]
            self.assertEqual(request.request_body.receive_id, "oc_dm")
            self.assertEqual(request.request_body.msg_type, "interactive")
            self.assertEqual(request.request_body.uuid, "test-board")
            self.assertEqual(json.loads(request.request_body.content), card)

            update.return_value.code = 0
            update.return_value.data = None
            adapter.update_card("om_board", card)
            request = update.call_args.args[0]
            self.assertEqual(request.message_id, "om_board")
            self.assertEqual(json.loads(request.request_body.content), card)
            update.return_value.code = 230031
            with self.assertRaises(lark_progress.ProgressCardUnavailable):
                adapter.update_card("om_board", card)

    def test_migration_preserves_approval_numbers_event_ids_and_cursor(self):
        source = self.path / "mattermost"
        source.mkdir()
        target = self.path / "lark"
        target.mkdir()
        write_state(source / "op-cursor.json", {"cursor": 12})
        write_state(source / "assigned-tickets.json", {"123": {"etag": "checkpoint"}})
        previous = Approvals(source / "approvals.sqlite3", "mm", "mm-chat")
        diagnostic = previous.enqueue(
            replace(self.request, event_id="test", topic="diagnostic")
        )
        task = previous.enqueue(self.request, related_event_id="op-activity:123")
        for task_id in (diagnostic, task):
            previous.start_approval(task_id)
            previous.approval_posted(task_id, f"mm:{task_id}")
        with patch.object(pilot, "MATTERMOST_STATE", source):
            self.assertEqual(
                pilot.migrate_mattermost(target, OWNER, INBOX), [(task, "123")]
            )
            pilot.import_checkpoint(target)
            with self.assertRaisesRegex(BridgeError, "已存在"):
                pilot.migrate_mattermost(target, OWNER, INBOX)
        self.assertEqual(previous.tasks_in("awaiting"), [diagnostic, task])
        transferred = Approvals(target / "approvals.sqlite3", OWNER, INBOX)
        self.assertEqual(transferred.tasks_in("queued"), [task])
        self.assertEqual(transferred.tasks_in("cancelled"), [diagnostic])
        self.assertEqual(
            transferred.enqueue(replace(self.request, event_id="op-activity:123")), task
        )
        with closing(transferred.connect()) as db:
            self.assertEqual(
                db.execute(
                    "SELECT approval_post_id FROM task WHERE id = ?", (task,)
                ).fetchone(),
                (None,),
            )
        for filename in ("op-cursor.json", "assigned-tickets.json"):
            self.assertEqual(
                (target / filename).read_text(), (source / filename).read_text()
            )

    def test_migration_refuses_to_transfer_an_approved_task(self):
        source = self.path / "mattermost"
        source.mkdir()
        target = self.path / "lark"
        target.mkdir()
        write_state(source / "op-cursor.json", {"cursor": 12})
        write_state(source / "assigned-tickets.json", {})
        previous = Approvals(source / "approvals.sqlite3", "mm", "mm-chat")
        task = previous.enqueue(self.request)
        previous.start_approval(task)
        previous.approval_posted(task, "mm-message")
        previous.accept_reply(Reply("mm", "mm-chat", "mm-message", "确认"))
        with patch.object(pilot, "MATTERMOST_STATE", source):
            with self.assertRaisesRegex(BridgeError, "approved"):
                pilot.migrate_mattermost(target, OWNER, INBOX)
        self.assertEqual(previous.tasks_in("approved"), [task])
        self.assertEqual(list(target.iterdir()), [])
