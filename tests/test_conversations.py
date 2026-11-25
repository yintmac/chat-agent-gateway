import asyncio
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import AsyncMock, Mock

from chat_agent_gateway.approval import Approvals, Request
from chat_agent_gateway.channels.lark import LarkChannel
from chat_agent_gateway.conversations import Conversations
from chat_agent_gateway.notify import BridgeError
from tests.test_lark_pilot import INBOX, OWNER, callback, direct_message


class ConversationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name)
        self.queue = Approvals(self.path / "approvals.sqlite3", OWNER, INBOX)
        self.messages = Mock()
        self.backend = AsyncMock(return_value=("第一轮结果", False))
        self.channel = self.reopen()

    def reopen(self):
        return LarkChannel(
            None, self.messages, self.queue, self.path / "cursor", run_job=self.backend
        )

    def test_focus_persists_across_restart_and_a_notice_does_not_steal_it(self):
        self.messages.reply.side_effect = ["answer1", "answer2", "answer3", "answer4"]
        self.channel.receive_message(direct_message("第一个需求", message="first"))
        asyncio.run(self.channel.execute())
        self.channel.deliver()
        notice = Request("op:1", "ticket:3", "source", "工单三", "", "")
        self.messages.send.return_value = "notice3"
        self.queue.enqueue(notice)
        self.channel.deliver()

        self.channel = self.reopen()
        self.channel.receive_message(direct_message("然后呢", message="follow"))
        self.channel.receive_message(
            direct_message("解释标题", message="ticket", parent="notice3")
        )
        self.channel.receive_message(
            direct_message(
                "继续第一个需求", message="back", parent="answer1", root="notice3"
            )
        )
        asyncio.run(self.channel.execute())
        self.channel.deliver()
        calls = [call.args[0] for call in self.backend.await_args_list]
        self.assertEqual([r["reference"] for r in calls], ["", "", "工单三", ""])
        self.assertEqual(
            list(calls[1]["history"]()),
            [{"user": "第一个需求", "assistant": "第一轮结果"}],
        )
        self.assertEqual(list(calls[2]["history"]()), [])
        self.assertEqual(
            [t["user"] for t in calls[3]["history"]()], ["第一个需求", "然后呢"]
        )
        self.assertEqual(
            [call.args[0] for call in self.messages.reply.call_args_list],
            ["first", "follow", "ticket", "back"],
        )

    def test_new_dialog_and_unresolved_quotes_do_not_leak_another_context(self):
        dialogs = self.channel.dialogs
        first = dialogs.receive("chat", "m1", "记住第一轮", [])
        self.queue.claim(first)
        self.queue.finish(first, "记住了", blocked=False)
        unknown = dialogs.receive("other", "orphan", "解释它", ["m1"])
        self.assertIsNone(self.queue.claim(unknown))
        self.assertIn("找不到", self.queue.start_result(unknown)[3])
        reset = dialogs.receive("chat", "reset", "新对话", ["m1"])
        self.assertIsNone(self.queue.claim(reset))
        self.assertIn("已开启", self.queue.start_result(reset)[3])
        later = dialogs.receive("chat", "m2", "新话题", [])
        self.assertNotIn("记住第一轮", [t["user"] for t in dialogs.history(later)])
        fresh = dialogs.receive("chat", "fresh", "新对话 只分析新需求", ["m1"])
        self.assertEqual(self.queue.claim(fresh)["summary"], "只分析新需求")
        self.assertEqual(list(dialogs.history(fresh)), [])
        self.assertIsNone(dialogs.receive("chat", "empty", " \n ", []))

    def test_concurrent_redelivery_enqueues_once_and_identical_text_with_new_id_runs(
        self,
    ):
        dialogs = self.channel.dialogs
        with ThreadPoolExecutor(max_workers=6) as pool:
            tasks = list(
                pool.map(
                    lambda _: dialogs.receive("chat", "unique", "继续", []), range(15)
                )
            )
        self.assertEqual(len(set(tasks)), 1)
        again = dialogs.receive("chat", "different", "继续", [])
        self.assertEqual(self.queue.tasks_in("approved"), [tasks[0], again])

    def test_complete_history_across_pages_includes_queued_follow_ups_in_order(self):
        dialogs = Conversations(self.queue)
        ids = [dialogs.receive("chat", str(i), f"消息 {i}", []) for i in range(70)]
        for i, task in enumerate(ids[:-1]):
            self.queue.claim(task)
            self.queue.finish(task, f"回答 {i}", blocked=False)
        self.assertEqual(
            list(dialogs.history(ids[-1])),
            [{"user": f"消息 {i}", "assistant": f"回答 {i}"} for i in range(69)],
        )

    def test_quoting_old_card_starts_one_turn_and_disables_its_old_button(self):
        notice = self.queue.enqueue(
            Request("old", "ticket:5", "source", "原始工单", "", "")
        )
        self.queue.start_approval(notice)
        self.queue.approval_posted(notice, "old-card")
        self.channel.receive_message(direct_message("只解释范围", parent="old-card"))
        self.assertEqual(
            self.channel.receive(callback("确认", post_id="old-card")).toast.type,
            "warning",
        )
        asyncio.run(self.channel.execute())
        self.assertEqual(self.queue.tasks_in("discussed"), [notice])
        self.backend.assert_awaited_once()
        self.assertEqual(self.backend.await_args.args[0]["summary"], "只解释范围")

    def test_blocker_replies_to_origin_and_an_uncertain_send_is_never_repeated(self):
        self.backend.side_effect = BridgeError("需要补充工单号")
        self.channel.receive_message(direct_message("继续", message="original"))
        asyncio.run(self.channel.execute())
        self.messages.reply.side_effect = BridgeError("发送结果不明")
        with self.assertRaisesRegex(BridgeError, "不明"):
            self.channel.deliver()
        self.channel.deliver()
        self.channel.receive_message(direct_message("继续", message="original"))
        asyncio.run(self.channel.execute())
        self.assertEqual(self.queue.tasks_in("sending"), [1])
        self.backend.assert_awaited_once()
        self.messages.reply.assert_called_once_with(
            "original", "这次处理卡住了。需要补充工单号"
        )
        self.messages.send.assert_not_called()
