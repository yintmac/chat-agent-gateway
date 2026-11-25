import json
import tempfile
import unittest
from pathlib import Path

from chat_agent_gateway.notify import BridgeError, notifications_after, poll


def item(notification_id):
    return {
        "id": notification_id,
        "reason": "mentioned",
        "_links": {
            "resource": {"href": "/api/v3/work_packages/12", "title": "Fix [test]"},
        },
    }


def page(ids, total, next_path=None):
    return {
        "total": total,
        "_embedded": {"elements": [item(notification_id) for notification_id in ids]},
        "_links": {"nextByOffset": {"href": next_path}} if next_path else {},
    }


class FakeApi:
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.calls = []

    def request(self, path, body=None):
        self.calls.append((path, body))
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response


class NotificationTests(unittest.TestCase):
    def test_reads_more_than_one_page_in_delivery_order(self):
        next_path = "/api/v3/notifications?offset=2"
        api = FakeApi(page([5, 4], 4, next_path), page([3, 2], 4), page([5], 4))
        self.assertEqual([it["id"] for it in notifications_after(api, 2)], [3, 4, 5])
        self.assertEqual(api.calls[1][0], next_path)

    def test_changed_collection_fails_before_delivery(self):
        api = FakeApi(page([5, 4, 3], 3), page([6], 4))
        with self.assertRaisesRegex(BridgeError, "changed during polling"):
            notifications_after(api, 4)

    def test_truncated_collection_fails_explicitly(self):
        api = FakeApi(page([5, 4], 3))
        with self.assertRaisesRegex(BridgeError, "ended early"):
            notifications_after(api, 2)

    def test_baseline_and_successful_post_are_persisted(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            mattermost = FakeApi(
                {"type": "D", "id": "dm", "name": "bot__recipient"},
                {"id": "post", "channel_id": "dm"},
            )
            poll(FakeApi(page([5], 1)), mattermost, state_path, "bot", "recipient")
            self.assertEqual(mattermost.calls, [])
            openproject = FakeApi(page([6, 5], 2), page([6], 2))
            poll(openproject, mattermost, state_path, "bot", "recipient")
            self.assertEqual(json.loads(state_path.read_text())["cursor"], 6)
            self.assertEqual(
                mattermost.calls[1][1]["props"]["openproject_notification_id"], 6
            )
            self.assertIn(r"Fix \[test\]", mattermost.calls[1][1]["message"])
            poll(
                FakeApi(page([6, 5], 2), page([6], 2)),
                mattermost,
                state_path,
                "bot",
                "recipient",
            )
            self.assertEqual(len(mattermost.calls), 2)

    def test_uncertain_post_is_not_resent_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            mattermost = FakeApi(
                {"type": "D", "id": "dm", "name": "recipient__bot"},
                BridgeError("request timed out"),
            )
            poll(FakeApi(page([5], 1)), mattermost, state_path, "bot", "recipient")
            with self.assertRaisesRegex(BridgeError, "request timed out"):
                poll(
                    FakeApi(page([6, 5], 2), page([6], 2)),
                    mattermost,
                    state_path,
                    "bot",
                    "recipient",
                )
            state = json.loads(state_path.read_text())
            self.assertEqual((state["cursor"], state["pending"]), (5, 6))
            with self.assertRaisesRegex(BridgeError, "needs reconciliation"):
                poll(FakeApi(), mattermost, state_path, "bot", "recipient")
            self.assertEqual(len(mattermost.calls), 2)

    def test_unexpected_dm_never_receives_a_post(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            mattermost = FakeApi({"type": "D", "id": "dm", "name": "bot__someone"})
            poll(FakeApi(page([5], 1)), mattermost, state_path, "bot", "recipient")
            with self.assertRaisesRegex(BridgeError, "unexpected participants"):
                poll(
                    FakeApi(page([6, 5], 2), page([6], 2)),
                    mattermost,
                    state_path,
                    "bot",
                    "recipient",
                )
            self.assertEqual(len(mattermost.calls), 1)


if __name__ == "__main__":
    unittest.main()
