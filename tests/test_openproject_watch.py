import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

from chat_agent_gateway.approval import Approvals
from chat_agent_gateway.notify import (
    OPENPROJECT,
    OPENPROJECT_USER_ID,
    ApiError,
    BridgeError,
)
from chat_agent_gateway.openproject_watch import (
    OWN_USER,
    assigned_open_tickets,
    personal_notification_request,
    poll_assigned_tickets,
)


def ticket(ticket_id=12, status=1, due=None, updated="2026-11-19T08:06:00Z"):
    return {
        "id": ticket_id,
        "subject": "Fixture ticket",
        "dueDate": due,
        "updatedAt": updated,
        "description": {"raw": "Work package context"},
        "_links": {
            "assignee": {"href": OWN_USER},
            "status": {
                "href": f"/api/v3/statuses/{status}",
                "title": f"Status {status}",
            },
        },
    }


def activity(activity_id, details=(), comment="", actor=36):
    return {
        "id": activity_id,
        "_type": "Activity::Comment" if comment else "Activity",
        "updatedAt": "2026-11-19T08:05:00Z",
        "details": [{"raw": line} for line in details],
        "comment": {"raw": comment},
        "_links": {
            "user": {"href": f"/api/v3/users/{actor}"},
            "workPackage": {"href": "/api/v3/work_packages/12"},
        },
    }


def collection(items, *, total=None, next_path=None):
    return {
        "total": len(items) if total is None else total,
        "_embedded": {"elements": items},
        "_links": {"nextByOffset": {"href": next_path}} if next_path else {},
    }


class AssignedTicketTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        path = Path(temp.name)
        self.state = path / "assigned-tickets.json"
        self.approvals = Approvals(path / "approvals.db", "owner", "inbox")
        self.api = Mock()

    def set_collection(self, package, activities, etag):
        self.api.request.return_value = collection([package])
        self.api.request_with_headers.return_value = (
            collection(activities),
            {"ETag": etag},
        )

    def poll(self):
        poll_assigned_tickets(self.api, self.approvals, self.state)

    def test_baseline_and_unchanged_ticket_do_not_enqueue_history(self):
        self.set_collection(ticket(), [activity(1, comment="Old comment")], "first")
        self.poll()
        original = self.state.read_bytes()
        self.api.request_with_headers.side_effect = ApiError(OPENPROJECT, 304)
        self.poll()
        self.assertEqual(self.approvals.tasks_in("queued"), [])
        self.assertEqual(self.state.read_bytes(), original)
        self.api.request_with_headers.assert_called_with(
            "/api/v3/work_packages/12/activities",
            extra_headers={"If-None-Match": "first"},
        )

    def test_existing_ticket_status_deadline_and_comment_are_notified(self):
        self.set_collection(ticket(), [], "first")
        self.poll()
        self.set_collection(
            ticket(status=2, due="2026-11-27"),
            [
                activity(
                    4, ("状态 从 新建 改为 进行中", "Finish date set to 2026-11-27")
                ),
                activity(5, comment="Please check this"),
                activity(6, ("Priority changed from Normal to High",)),
                activity(7, comment="Own note", actor=OPENPROJECT_USER_ID),
            ],
            "second",
        )
        self.poll()
        task_ids = self.approvals.tasks_in("queued")
        self.assertEqual(len(task_ids), 2)
        state = json.loads(self.state.read_text())
        self.assertEqual(state["12"]["fields"]["deadline"], "2026-11-27")
        status_request = self.approvals.start_approval(task_ids[0])
        self.assertIn("状态 从 新建 改为 进行中", status_request["summary"])
        self.assertIn("Finish date set to 2026-11-27", status_request["summary"])
        self.assertEqual(
            status_request["update"],
            "状态 从 新建 改为 进行中\nFinish date set to 2026-11-27",
        )
        comment_request = self.approvals.start_approval(task_ids[1])
        self.assertIn("Please check this", comment_request["summary"])
        self.assertEqual(comment_request["update"], "Please check this")

    def test_personal_notification_and_scan_share_journal_identity(self):
        self.set_collection(ticket(), [], "first")
        self.poll()
        package = ticket(status=2)
        record = activity(
            8, ("Status changed from New to In Progress", "Other field changed")
        )
        self.api.request.side_effect = [record, package]
        notification = {
            "id": 99,
            "_links": {
                "activity": {"href": "/api/v3/activities/8"},
                "resource": {"href": "/api/v3/work_packages/12"},
            },
        }
        task = self.approvals.enqueue(
            personal_notification_request(self.api, notification)
        )
        self.api.request.side_effect = None
        self.set_collection(package, [record], "second")
        self.poll()
        self.assertEqual(self.approvals.tasks_in("queued"), [task])

    def test_closing_a_tracked_ticket_is_observed_before_removing_it(self):
        self.set_collection(ticket(), [], "first")
        self.poll()
        self.api.request.side_effect = [collection([]), ticket(status=8)]
        self.api.request_with_headers.return_value = (
            collection([activity(9, ("状态 从 新建 改为 关闭",))]),
            {"ETag": "closed"},
        )
        self.poll()
        self.assertEqual(json.loads(self.state.read_text()), {})
        self.assertEqual(len(self.approvals.tasks_in("queued")), 1)

    def test_retry_links_previous_notification_key_to_the_same_activity(self):
        self.api.request.side_effect = [
            activity(8, ("Status changed from New to In Progress",)),
            ticket(status=2),
        ]
        notice = {
            "id": 99,
            "_links": {
                "activity": {"href": "/api/v3/activities/8"},
                "resource": {"href": "/api/v3/work_packages/12"},
            },
        }
        modern = personal_notification_request(self.api, notice)
        old = replace(modern, event_id="op:99", summary="Previous version's summary")
        original_id = self.approvals.enqueue(old)
        self.assertEqual(
            self.approvals.enqueue(modern, related_event_id="op:99"), original_id
        )
        self.assertEqual(self.approvals.enqueue(modern), original_id)
        self.assertEqual(self.approvals.tasks_in("queued"), [original_id])

    def test_unmatched_field_change_leaves_baseline_unchanged(self):
        self.set_collection(ticket(), [], "first")
        self.poll()
        before = self.state.read_bytes()
        self.set_collection(ticket(status=2), [], "second")
        with self.assertRaisesRegex(BridgeError, "活动记录无法核对"):
            self.poll()
        self.assertEqual(self.state.read_bytes(), before)
        self.assertEqual(self.approvals.tasks_in("queued"), [])

    def test_activity_arriving_during_poll_is_reconciled_on_next_snapshot(self):
        package = ticket(updated="2026-11-19T08:04:00Z")
        self.set_collection(package, [], "first")
        self.poll()
        self.set_collection(
            package, [activity(10, ("状态 从 新建 改为 进行中",))], "second"
        )
        self.poll()
        self.assertEqual(len(self.approvals.tasks_in("queued")), 1)
        self.api.request.return_value = collection([ticket(status=2)])
        self.api.request_with_headers.side_effect = ApiError(OPENPROJECT, 304)
        self.poll()
        self.assertEqual(len(self.approvals.tasks_in("queued")), 1)
        self.assertEqual(
            json.loads(self.state.read_text())["12"]["observed_fields"], {}
        )

    def test_reads_all_list_pages_and_fails_on_incomplete_list(self):
        path = "/api/v3/work_packages?offset=2"
        self.api.request.side_effect = [
            collection([ticket()], total=2, next_path=path),
            collection([ticket(ticket_id=14)], total=2),
        ]
        self.assertEqual(list(assigned_open_tickets(self.api)), ["12", "14"])
        self.api.request.assert_called_with(path)
        self.api.request.side_effect = [collection([ticket()], total=2)]
        with self.assertRaisesRegex(BridgeError, "列表不完整"):
            assigned_open_tickets(self.api)


if __name__ == "__main__":
    unittest.main()
