"""Poll OpenProject and execute work claimed from the approval queue."""

import json
import subprocess
from typing import Protocol

from .notify import BridgeError, newest_notification, notifications_after, write_state
from .openproject_watch import personal_notification_request, poll_assigned_tickets

REASONS = ("assigned", "mentioned", "watched")


class ReportProgress(Protocol):
    def __call__(self, label: str, *, detail: str | None = None) -> None: ...


class RunJob(Protocol):
    async def __call__(
        self, request: dict, *, progress: ReportProgress | None = None
    ) -> tuple[str, bool]: ...


class ApprovalWorkflow:
    def __init__(self, op, approvals, cursor_path, run_job: RunJob):
        self.op = op
        self.approvals = approvals
        self.cursor_path = cursor_path
        self.run_job = run_job

    def poll_openproject(self):
        if not self.cursor_path.exists():
            cursor, _ = newest_notification(self.op, REASONS)
            write_state(self.cursor_path, {"cursor": cursor})
        else:
            cursor = json.loads(self.cursor_path.read_text())["cursor"]
            for item in notifications_after(self.op, cursor, REASONS):
                request = personal_notification_request(self.op, item)
                if request:
                    self.approvals.enqueue(request, related_event_id=f"op:{item['id']}")
                write_state(self.cursor_path, {"cursor": item["id"]})
        poll_assigned_tickets(
            self.op, self.approvals, self.cursor_path.with_name("assigned-tickets.json")
        )

    async def run_task(self, task_id, request):
        return await self.run_job(request)

    async def execute(self):
        for task_id in self.approvals.tasks_in("approved"):
            request = self.approvals.claim(task_id)
            if not request:
                continue
            print(f"开始处理任务 {task_id}。", flush=True)
            try:
                result, blocked = await self.run_task(task_id, request)
                self.approvals.finish(task_id, result, blocked=blocked)
            except (
                BridgeError,
                OSError,
                ValueError,
                subprocess.SubprocessError,
            ) as error:
                detail = (
                    str(error)
                    if isinstance(error, BridgeError)
                    else type(error).__name__
                )
                self.approvals.finish(
                    task_id, f"这次处理卡住了。{detail}", blocked=True
                )
