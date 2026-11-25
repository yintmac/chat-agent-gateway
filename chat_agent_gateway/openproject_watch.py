"""Collect and deduplicate updates to the OpenProject tickets assigned to the configured operator."""

import hashlib
import json
import re
from datetime import datetime
from urllib.parse import urlencode, urlsplit

from .approval import Request
from .notify import (
    OPENPROJECT,
    OPENPROJECT_USER_ID,
    REASONS,
    WORK_PACKAGE_PATH,
    ApiError,
    BridgeError,
    write_state,
)

TICKET_COLLECTION = "/api/v3/work_packages"
OWN_USER = f"/api/v3/users/{OPENPROJECT_USER_ID}"
ACTIVITY_PATH = re.compile(r"/api/v3/activities/([1-9][0-9]*)")
STATUS_CHANGE = re.compile(r"^(?:Status|状态)(?=\s|[:：]|$)", re.IGNORECASE)
DEADLINE_CHANGE = re.compile(
    r"^(?:Finish date|Due date|Date|完成日期|结束日期|截止日期|日期)(?=\s|[:：]|$)",
    re.IGNORECASE,
)


def ticket_summary(ticket):
    description = (ticket.get("description") or {}).get("raw")
    return "\n\n".join(
        part for part in (f"#{ticket['id']} {ticket['subject']}", description) if part
    )


def activity_comment(activity):
    return (activity.get("comment") or {}).get("raw") or ""


def activity_details(activity):
    return [
        change["raw"] for change in activity.get("details") or [] if change.get("raw")
    ]


def activity_request(ticket, activity, event_id, details=None, *, reason=None):
    ticket_id = ticket["id"]
    title, _, description = ticket_summary(ticket).partition("\n\n")
    comment = activity_comment(activity)
    changes = activity_details(activity) if details is None else details
    important_changes = (
        [
            change
            for change in changes
            if STATUS_CHANGE.match(change) or DEADLINE_CHANGE.match(change)
        ]
        if details is None
        else details
    )
    if not important_changes and not comment:
        important_changes = changes[:1]
    text = (title, comment, *changes, description)
    update = (REASONS.get(reason), *important_changes, comment)
    if activity:
        event_id = f"op-activity:{activity['id']}:{activity['updatedAt']}"
    return Request(
        event_id=event_id,
        topic=f"ticket:{ticket_id}",
        source=f"{OPENPROJECT}/work_packages/{ticket_id}",
        summary="\n\n".join(part for part in text if part),
        channel_id="",
        root_id="",
        update="\n".join(part for part in update if part),
    )


def personal_notification_request(api, notification):
    links = notification["_links"]
    if (links.get("actor") or {}).get("href") == OWN_USER:
        return None
    path = links["resource"].get("href", "")
    match = WORK_PACKAGE_PATH.fullmatch(path)
    if not match:
        raise BridgeError("OpenProject 返回了无法核对的工单链接。")
    activity_path = (links.get("activity") or {}).get("href")
    activity = {}
    if activity_path:
        activity_id = ACTIVITY_PATH.fullmatch(activity_path)
        if not activity_id:
            raise BridgeError("OpenProject 返回了无法核对的活动链接。")
        activity = api.request(activity_path)
        if activity["id"] != int(activity_id[1]):
            raise BridgeError("OpenProject 返回的活动与通知不符。")
    ticket = api.request(path)
    if ticket["id"] != int(match[1]):
        raise BridgeError("OpenProject 返回的工单与通知不符。")
    return activity_request(
        ticket, activity, f"op:{notification['id']}", reason=notification.get("reason")
    )


def assigned_open_tickets(api):
    filters = [
        {"assignee": {"operator": "=", "values": [str(OPENPROJECT_USER_ID)]}},
        {"status": {"operator": "o", "values": []}},
    ]
    query = urlencode(
        {
            "filters": json.dumps(filters),
            "pageSize": 100,
            "sortBy": json.dumps([["id", "asc"]]),
        }
    )
    path = f"{TICKET_COLLECTION}?{query}"
    tickets = {}
    total = None
    last_id = 0
    while path:
        if urlsplit(path).path != TICKET_COLLECTION:
            raise BridgeError("OpenProject 返回了无效的名下工单分页链接。")
        page = api.request(path)
        if total is None:
            total = page["total"]
        elif total != page["total"]:
            raise BridgeError("OpenProject 名下工单列表在分页期间发生变动。")
        items = page["_embedded"]["elements"]
        for ticket in items:
            ticket_id = ticket["id"]
            assignee = (ticket["_links"].get("assignee") or {}).get("href")
            if ticket_id <= last_id or assignee != OWN_USER:
                raise BridgeError("OpenProject 名下工单分页的顺序或负责人不符。")
            tickets[str(ticket_id)] = ticket
            last_id = ticket_id
        path = page["_links"].get("nextByOffset", {}).get("href")
        if path and not items:
            raise BridgeError("OpenProject 名下工单分页未能继续。")
    if len(tickets) != total:
        raise BridgeError("OpenProject 名下工单列表不完整。")
    return tickets


def assigned_open_ticket_report(api):
    try:
        tickets = assigned_open_tickets(api)
        if not tickets:
            return "OpenProject 当前没有分配给你的未关闭工单。", 0
        lines = [f"OpenProject 当前有 {len(tickets)} 条未关闭工单分配给你。"]
        for ticket in tickets.values():
            ticket_id = ticket["id"]
            title = " ".join(ticket["subject"].split())
            status = " ".join(ticket["_links"]["status"]["title"].split())
            lines.append(
                f"#{ticket_id} {title} · {status}\n{OPENPROJECT}/work_packages/{ticket_id}"
            )
    except (AttributeError, KeyError, TypeError) as error:
        raise BridgeError("OpenProject 返回了无法核对的工单列表。") from error
    return "\n\n".join(lines), len(tickets)


def assigned_activities(api, ticket_id, etag):
    path = f"{TICKET_COLLECTION}/{ticket_id}/activities"
    headers = {"If-None-Match": etag} if etag else None
    try:
        page, response_headers = api.request_with_headers(path, extra_headers=headers)
    except ApiError as error:
        if error.status == 304 and etag:
            return None, etag
        raise
    items = page["_embedded"]["elements"]
    if page["total"] != len(items) or page["_links"].get("nextByOffset"):
        raise BridgeError("OpenProject 工单活动返回不完整。")
    activities = {}
    for activity in items:
        key = str(activity["id"])
        work_package = activity["_links"]["workPackage"].get("href")
        if key in activities or work_package != f"{TICKET_COLLECTION}/{ticket_id}":
            raise BridgeError("OpenProject 工单活动包含重复或不属于该工单的记录。")
        activities[key] = activity
    return activities, response_headers.get("ETag")


def relevant_changes(ticket, activities, previous):
    signatures = {}
    events = []
    changed_fields = {}
    for key, activity in sorted(activities.items(), key=lambda item: int(item[0])):
        # The API exports formatted journal details. The supported locales are English and Chinese.
        effective_details = activity_details(activity)
        status = [line for line in effective_details if STATUS_CHANGE.match(line)]
        deadline = [line for line in effective_details if DEADLINE_CHANGE.match(line)]
        comment = (
            activity_comment(activity)
            if activity.get("_type") == "Activity::Comment"
            else ""
        )
        signatures[key] = {
            field: hashlib.sha256(json.dumps(value).encode()).hexdigest()
            for field, value in (
                ("status", status),
                ("deadline", deadline),
                ("comment", comment),
            )
            if value
        }
        if previous is None:
            continue
        prior = previous.get(key, {})
        delta = {
            field
            for field in prior.keys() | signatures[key].keys()
            if prior.get(field) != signatures[key].get(field)
        }
        for field in delta & {"status", "deadline"}:
            timestamp = activity["updatedAt"]
            if field not in changed_fields or datetime.fromisoformat(
                timestamp
            ) > datetime.fromisoformat(changed_fields[field]):
                changed_fields[field] = timestamp
        actor = activity["_links"].get("user", {}).get("href")
        if delta and actor != OWN_USER:
            details = []
            if "status" in delta:
                details.extend(
                    status or [f"状态现为 {ticket['_links']['status']['title']}"]
                )
            if "deadline" in delta:
                date = ticket.get("dueDate", ticket.get("date"))
                details.extend(deadline or [f"截止日期现为 {date or '未设置'}"])
            events.append(activity_request(ticket, activity, "", details))
    return signatures, events, changed_fields


def poll_assigned_tickets(api, approvals, state_path):
    has_baseline = state_path.exists()
    before = json.loads(state_path.read_text()) if has_baseline else {}
    current = assigned_open_tickets(api)
    state = {}
    events = []
    for ticket_id in sorted(before.keys() | current.keys(), key=int):
        older = before.get(ticket_id)
        ticket = current.get(ticket_id)
        # Scan a departing ticket one final time so closing it doesn't hide the closing activity.
        if ticket is None:
            ticket = api.request(f"{TICKET_COLLECTION}/{ticket_id}")
        fields = {
            "status": ticket["_links"]["status"]["href"],
            "deadline": ticket.get("dueDate", ticket.get("date")),
        }
        activities, etag = assigned_activities(
            api, ticket_id, older["etag"] if older else None
        )
        if activities is None:
            signatures = older["activities"]
            changed_fields = {}
        else:
            signatures, additions, changed_fields = relevant_changes(
                ticket, activities, older["activities"] if older else None
            )
            events.extend(additions)
        # A journal created between the collection read and the activity read can precede
        # the matching field value by one scan. Retain that evidence until the list catches up.
        observed_fields = dict(older["observed_fields"]) if older else {}
        observed_fields.update(changed_fields)
        if older and any(
            older["fields"][field] != value and field not in observed_fields
            for field, value in fields.items()
        ):
            raise BridgeError(
                f"OpenProject 工单 #{ticket_id} 的状态或截止日期已变，但活动记录无法核对。"
            )
        if ticket_id in current:
            state[ticket_id] = {
                "etag": etag,
                "activities": signatures,
                "fields": fields,
                "observed_fields": {
                    field: timestamp
                    for field, timestamp in observed_fields.items()
                    if datetime.fromisoformat(timestamp)
                    > datetime.fromisoformat(ticket["updatedAt"])
                },
            }

    for request in events:
        approvals.enqueue(request)
    if not has_baseline or state != before:
        write_state(state_path, state)
