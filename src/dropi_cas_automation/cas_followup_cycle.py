"""Fail-closed CAS follow-up cycle; remote writes are explicit and durable."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import secrets
import signal
import subprocess
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Protocol

API_BASE = "https://api-v2.dropi.co/cas"
APP_ORIGIN = "https://app.dropi.co"
OPEN_STATES = ("queues", "active")
INVENTORY_STATES = ("queues", "active", "postponed")
CLIENT_ROLES = frozenset({"SELLER", "DROPSHIPPER"})
KNOWN_INTERACTION_TYPES = frozenset({
    "MESSAGE", "STATUS_CHANGED", "CAS_REQUEST_CREATED", "CAS_CHAT_CREATED",
    "CAS_TICKET_CREATED", "CAS_TICKET_NOTE_CREATED", "CAS_SOLVED",
    "CAS_CHAT_TAKEN", "CAS_CHAT_TRANSFERED", "CAS_CHAT_NOTE_CREATED",
    "CAS_CHAT_MESSAGE_REPORTED", "CAS_CHAT_HIGHLIGHT_MESSAGE",
    "CAS_CHAT_REMOVE_HIGHLIGHTED_MESSAGE", "DELETED_MESSAGE", "EDITED_MESSAGE",
    "AUTOMATIC_SOLVED",
})
REPORT_FAILURE_CODES = frozenset({
    "remote_list_schema", "deadline", "row_identity", "row_state_mismatch",
    "row_not_order", "row_missing_guide", "row_ticket", "row_last_message",
    "counts_schema", "pagination_guard", "duplicate_chat", "counts_changed",
    "counts_mismatch", "thread_empty", "thread_parent_mismatch", "thread_identity",
    "thread_type_unknown", "thread_timestamp", "thread_root_missing",
    "thread_last_message_missing", "thread_last_message_timestamp",
    "thread_last_message_stale", "row_timestamp", "revalidation_not_exact",
    "revalidation_identity", "revalidation_terminal", "journal_schema",
    "browser_timeout", "browser_failed", "browser_json", "authentication",
    "journal_lock_timeout", "readback_mismatch",
})
COOLDOWN = timedelta(hours=24)
MESSAGE_TEMPLATE = (
    "Buen día. Solicito actualización del CAS de la orden {order_id}, guía {guide}: "
    "estado actual de la gestión, acción pendiente y plazo estimado de solución. "
    "Si la incidencia ya fue atendida, agradezco confirmar el resultado y su soporte "
    "por este medio. Gracias."
)
_CHAT_ID = re.compile(r"^[0-9a-f]{24}$")
_ORDER_ID = re.compile(r"^[0-9]+$")
_HASH = re.compile(r"^[0-9a-f]{64}$")


class CycleFailure(RuntimeError):
    """A local or remote condition cannot safely authorize a decision."""


class BrowserRunner(Protocol):
    def execute_json(self, code: str, timeout_seconds: int = 90) -> dict[str, Any]: ...


Request = Callable[[dict[str, Any]], dict[str, Any]]


def _listing_payload(status: str, offset: int, page_size: int, search_term: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"page_size": page_size, "offset": offset, "orderDirection": "asc", "version": "2.0.4", "status_chat": status, "filterByCategoryUser": [], "filterByCommercialId": [], "filterByCommunities": [], "filterByWhiteBrandId": [], "enterprise": []}
    if search_term is not None:
        payload["search_term"] = search_term
    return payload


def _response_rows(response: object, *, allowed_statuses: tuple[int, ...] = (200,)) -> list[dict[str, Any]]:
    if not isinstance(response, dict) or response.get("status") not in allowed_statuses or not isinstance(response.get("data"), list):
        raise CycleFailure("remote_list_schema")
    if not all(isinstance(row, dict) for row in response["data"]):
        raise CycleFailure("remote_list_schema")
    return response["data"]


def enumerate_status(request: Request, status: str, *, page_size: int = 10, max_pages: int = 10000, deadline: float | None = None) -> tuple[list[dict[str, Any]], bool]:
    if status not in INVENTORY_STATES or page_size < 1 or max_pages < 1:
        raise ValueError("invalid pagination arguments")
    rows: list[dict[str, Any]] = []
    for _page in range(max_pages):
        if deadline is not None and time.monotonic() >= deadline:
            raise CycleFailure("deadline")
        page = _response_rows(request(_listing_payload(status, len(rows), page_size)))
        if not page:
            return rows, True
        rows.extend(page)
    return rows, False


def _chat(row: object) -> dict[str, Any]:
    if not isinstance(row, dict) or not isinstance(row.get("casChat"), dict):
        raise CycleFailure("row_identity")
    return row["casChat"]


def _strict_string(value: object, reason: str) -> str:
    if not isinstance(value, str) or not value:
        raise CycleFailure(reason)
    return value


def _row_identity(row: dict[str, Any], expected_status: str | None = None) -> tuple[str, str, str, str, str, str, str | None, datetime | None]:
    """Return stable row identity plus the independently mutable last-message snapshot."""
    chat = _chat(row)
    chat_id = _strict_string(chat.get("_id"), "row_identity")
    if not _CHAT_ID.fullmatch(chat_id):
        raise CycleFailure("row_identity")
    status = chat.get("status")
    if not isinstance(status, str) or (expected_status is not None and status != expected_status):
        raise CycleFailure("row_state_mismatch")
    enterprise = _strict_string(chat.get("enterprise"), "row_identity")
    reference = row.get("referenceObject")
    if not isinstance(reference, dict) or reference.get("type") != "ORDER":
        raise CycleFailure("row_not_order")
    order_id = reference.get("id")
    # bool is intentionally not an order even though it is an int subclass.
    if isinstance(order_id, bool) or not isinstance(order_id, (str, int)) or not _ORDER_ID.fullmatch(str(order_id)):
        raise CycleFailure("row_identity")
    data = reference.get("data")
    if not isinstance(data, dict):
        raise CycleFailure("row_identity")
    guide = data.get("shipping_guide")
    if not isinstance(guide, str) or not guide:
        raise CycleFailure("row_missing_guide")
    ticket = row.get("casTicketType")
    if not isinstance(ticket, dict):
        raise CycleFailure("row_ticket")
    ticket_id, ticket_name = _strict_string(ticket.get("_id"), "row_ticket"), _strict_string(ticket.get("name"), "row_ticket")
    last = row.get("lastMessage")
    if last is None or last == {}:
        last_id, last_at = None, None
    elif isinstance(last, dict):
        last_id = _strict_string(last.get("_id"), "row_last_message")
        last_at = _parse_time(last.get("createdAt"))
        if last_at is None:
            raise CycleFailure("row_last_message")
    else:
        raise CycleFailure("row_last_message")
    return chat_id, str(order_id), guide, enterprise, ticket_id, ticket_name, last_id, last_at


def _stable_row_identity(row: dict[str, Any]) -> tuple[str, str, str, str, str, str]:
    return _row_identity(row)[:6]


def _last_message_snapshot(row: dict[str, Any]) -> tuple[str | None, datetime | None]:
    identity = _row_identity(row)
    return identity[6], identity[7]


def inventory(request: Request, counts_request: Request, *, page_size: int = 10, max_pages: int = 10000, deadline: float | None = None) -> tuple[list[dict[str, Any]], dict[str, int]]:
    mapping = {"queues": "queued", "active": "active", "postponed": "postponed"}
    if deadline is not None and time.monotonic() >= deadline:
        raise CycleFailure("deadline")
    before = counts_request(_listing_payload("queues", 0, page_size))
    before_data = before.get("data") if isinstance(before, dict) and before.get("status") == 200 else None
    if not isinstance(before_data, dict) or any(isinstance(before_data.get(key), bool) or not isinstance(before_data.get(key), int) or before_data[key] < 0 for key in mapping.values()):
        raise CycleFailure("counts_schema")
    rows: list[dict[str, Any]] = []
    totals: dict[str, int] = {}
    seen: set[str] = set()
    for status in INVENTORY_STATES:
        group, complete = enumerate_status(request, status, page_size=page_size, max_pages=max_pages, deadline=deadline)
        if not complete:
            raise CycleFailure("pagination_guard")
        for value in group:
            chat = _chat(value)
            chat_id = chat.get("_id")
            if chat.get("status") != status:
                raise CycleFailure("row_state_mismatch")
            if not isinstance(chat_id, str) or chat_id in seen:
                raise CycleFailure("duplicate_chat")
            seen.add(chat_id)
        totals[status] = len(group)
        rows.extend(group)
    counts = counts_request(_listing_payload("queues", 0, page_size))
    data = counts.get("data") if isinstance(counts, dict) and counts.get("status") == 200 else None
    if not isinstance(data, dict) or any(isinstance(data.get(key), bool) or not isinstance(data.get(key), int) or data[key] < 0 for key in mapping.values()):
        raise CycleFailure("counts_schema")
    if any(data[mapping[state]] != before_data[mapping[state]] for state in INVENTORY_STATES):
        raise CycleFailure("counts_changed")
    if any(data[mapping[state]] != totals[state] for state in INVENTORY_STATES):
        raise CycleFailure("counts_mismatch")
    return rows, totals


def followup_text(order_id: str, guide: str) -> str:
    if not isinstance(order_id, str) or not _ORDER_ID.fullmatch(order_id) or not isinstance(guide, str) or not guide:
        raise ValueError("identity is required")
    return MESSAGE_TEMPLATE.format(order_id=order_id, guide=guide)


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _interaction_time(item: dict[str, Any]) -> datetime | None:
    return _parse_time(item.get("createdAt")) or _parse_time(item.get("deviceDateCreate"))


def inspect_thread(items: object, *, chat_id: str, expected_last_message_id: str | None, expected_last_message_at: datetime | None = None, allow_newer_messages: bool = False, now: datetime | None = None) -> tuple[set[str], datetime | None, datetime | None]:
    """Validate the complete sanitized thread without retaining its text."""
    if not isinstance(items, list) or not items:
        raise CycleFailure("thread_empty")
    ids: set[str] = set()
    client_latest: datetime | None = None
    carrier_latest: datetime | None = None
    root_count = 0
    messages: dict[str, datetime] = {}
    for item in items:
        if not isinstance(item, dict) or item.get("casChat") != chat_id:
            raise CycleFailure("thread_parent_mismatch")
        item_id = item.get("_id")
        if not isinstance(item_id, str) or not item_id or item_id in ids:
            raise CycleFailure("thread_identity")
        ids.add(item_id)
        kind = item.get("interactionType")
        if kind not in KNOWN_INTERACTION_TYPES:
            raise CycleFailure("thread_type_unknown")
        timestamp = _interaction_time(item)
        if timestamp is None or (now is not None and timestamp > now):
            raise CycleFailure("thread_timestamp")
        if kind == "CAS_REQUEST_CREATED":
            root_count += 1
            continue
        if kind != "MESSAGE":
            continue
        messages[item_id] = timestamp
        if item.get("isDeleted") is True:
            continue
        user = item.get("user")
        current = isinstance(user, dict) and user.get("is_current") is True
        role = item.get("user_rol")
        role_name = role.get("name") if isinstance(role, dict) else None
        if current or role_name in CLIENT_ROLES:
            client_latest = max(client_latest, timestamp) if client_latest else timestamp
        else:
            carrier_latest = max(carrier_latest, timestamp) if carrier_latest else timestamp
    if root_count != 1:
        raise CycleFailure("thread_root_missing")
    if expected_last_message_id is None:
        if messages:
            raise CycleFailure("thread_last_message_missing")
    else:
        last_timestamp = messages.get(expected_last_message_id)
        if last_timestamp is None:
            raise CycleFailure("thread_last_message_missing")
        if expected_last_message_at is not None and last_timestamp != expected_last_message_at:
            raise CycleFailure("thread_last_message_timestamp")
        if not allow_newer_messages and any(timestamp > last_timestamp for timestamp in messages.values()):
            raise CycleFailure("thread_last_message_stale")
    return ids, client_latest, carrier_latest


def is_due(row: dict[str, Any], interactions: object, *, journal_at: datetime | None, now: datetime) -> tuple[bool, str]:
    chat_id, _order, _guide, _enterprise, _ticket_id, _ticket_name, last_id, last_at = _row_identity(row)
    if _chat(row).get("status") == "postponed":
        return False, "postponed"
    created = _parse_time(_chat(row).get("createdAt"))
    if created is None or created > now:
        raise CycleFailure("row_timestamp")
    _ids, client, carrier = inspect_thread(interactions, chat_id=chat_id, expected_last_message_id=last_id, expected_last_message_at=last_at, now=now)
    newest = max(value for value in (created, client, carrier, journal_at) if value is not None)
    return (False, "cooldown") if now - newest < COOLDOWN else (True, "due")


def exact_revalidate(request: Request, row: dict[str, Any]) -> dict[str, Any]:
    identity = _stable_row_identity(row)
    chat_id = identity[0]
    matches: list[dict[str, Any]] = []
    for state in OPEN_STATES:
        matches.extend(_response_rows(request(_listing_payload(state, 0, 10, chat_id))))
    if len(matches) != 1:
        raise CycleFailure("revalidation_not_exact")
    candidate = matches[0]
    if _stable_row_identity(candidate) != identity:
        raise CycleFailure("revalidation_identity")
    if _chat(candidate).get("status") not in OPEN_STATES:
        raise CycleFailure("revalidation_terminal")
    return candidate


def message_hash(message: str) -> str:
    return hashlib.sha256(message.encode("utf-8")).hexdigest()


class DurableJournal:
    """Append-only fsync journal; an unresolved attempt is permanently blocking."""
    STATUSES = frozenset({"write_intent", "write_verified", "blocked_write_uncertain", "blocked_remote_readback_mismatch"})

    def __init__(self, path: Path):
        self.path = path.expanduser().resolve()

    def append(self, event: dict[str, Any]) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        encoded = (json.dumps(event, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")
        descriptor = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
            os.write(descriptor, encoded)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def assess(self, chat_id: str) -> tuple[datetime | None, str | None]:
        if not self.path.exists():
            return None, None
        intents: dict[str, str] = {}
        verified: set[str] = set()
        latest: datetime | None = None
        with self.path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise CycleFailure("journal_schema") from exc
                if not isinstance(event, dict):
                    raise CycleFailure("journal_schema")
                event_chat = event.get("chat_id")
                if not isinstance(event_chat, str):
                    raise CycleFailure("journal_schema")
                if event_chat != chat_id:
                    continue
                status, at = event.get("status"), _parse_time(event.get("at"))
                if status not in self.STATUSES or at is None:
                    return None, "journal_invalid"
                attempt_id, digest = event.get("attempt_id"), event.get("message_hash")
                if not isinstance(attempt_id, str) or not attempt_id or not isinstance(digest, str) or not _HASH.fullmatch(digest):
                    return None, "journal_invalid"
                latest = max(latest, at) if latest else at
                if status == "write_intent":
                    if attempt_id in intents:
                        return None, "journal_invalid"
                    intents[attempt_id] = digest
                elif status == "write_verified":
                    if intents.get(attempt_id) != digest or not isinstance(event.get("message_id"), str) or not event["message_id"]:
                        return None, "journal_invalid"
                    verified.add(attempt_id)
                else:
                    return None, "journal_uncertain"
        if any(attempt not in verified for attempt in intents):
            return None, "journal_unresolved"
        return latest, None

    def latest_sent_at(self, chat_id: str) -> datetime | None:
        at, blocked = self.assess(chat_id)
        if blocked:
            raise CycleFailure(blocked)
        return at


def browser_program(action: dict[str, Any]) -> str:
    """Return a browser program which projects only allowlisted fields.

    The write branch repeats the complete read-side preflight in one browser
    invocation.  It returns no remote text or PII, including on failure.
    """
    encoded = json.dumps(action, ensure_ascii=False, separators=(",", ":"))
    known = json.dumps(sorted(KNOWN_INTERACTION_TYPES), separators=(",", ":"))
    return f'''(async () => {{
const action={encoded}; const KNOWN=new Set({known}); const token=JSON.parse(localStorage.getItem('casToken')||'null'); const user=JSON.parse(localStorage.getItem('casUser')||'null');
if(!token||!user||!user._id) return {{ok:false,reason:'session_missing'}};
if(location.origin!=='{APP_ORIGIN}'||!location.pathname.startsWith('/dashboard/cas')) return {{ok:false,reason:'route_missing'}};
const headers={{Authorization:'Bearer '+token,'X-Authorization':'Bearer '+token,'Content-Type':'application/json'}};
const post=async(path,body)=>{{try{{const r=await fetch('{API_BASE}'+path,{{method:'POST',headers,body:JSON.stringify(body)}});let p=null;try{{p=await r.json()}}catch(_){{}}return {{status:r.status,data:p&&p.data}}}}catch(_){{return {{status:0,data:null}}}}}};
const value=(x)=>x===undefined?null:x;
const projectRow=(r)=>{{const c=r&&r.casChat,ref=r&&r.referenceObject,t=r&&r.casTicketType,l=r&&r.lastMessage;return {{casChat:{{_id:value(c&&c._id),status:value(c&&c.status),enterprise:value(c&&c.enterprise),createdAt:value(c&&c.createdAt),datePostponed:value(c&&c.datePostponed)}},referenceObject:{{id:value(ref&&ref.id),type:value(ref&&ref.type),data:{{shipping_guide:value(ref&&ref.data&&ref.data.shipping_guide),shipping_company:value(ref&&ref.data&&ref.data.shipping_company)}}}},casTicketType:{{_id:value(t&&t._id),name:value(t&&t.name)}},lastMessage:l==null?null:(typeof l==='object'&&!Array.isArray(l)?(Object.keys(l).length===0?null:{{_id:value(l._id),createdAt:value(l.createdAt)}}):{{_id:null,createdAt:null}})}}}};
const projectThread=(items,expected)=>Array.isArray(items)?items.map(i=>{{const o=i&&i.objectIntegrationType;return {{_id:value(i&&i._id),casChat:value(i&&i.casChat),interactionType:value(i&&i.interactionType),isDeleted:i&&i.isDeleted===true,createdAt:value(i&&i.createdAt),deviceDateCreate:value(i&&i.deviceDateCreate),user:{{is_current:!!(i&&i.user&&i.user._id===user._id)}},user_rol:{{name:value(i&&i.user_rol&&i.user_rol.name)}},message_exact:typeof(o&&o.message)==='string'&&typeof expected==='string'&&o.message===expected}}}}):null;
const same=(left,right)=>JSON.stringify(left)===JSON.stringify(right);
const validThread=(items,chat,last)=>{{if(!Array.isArray(items)||!items.length)return false;const ids=new Set();let roots=0;const messages=[];for(const i of items){{if(!i||i.casChat!==chat||typeof i._id!=='string'||!i._id||ids.has(i._id)||!KNOWN.has(i.interactionType))return false;ids.add(i._id);const at=Date.parse(i.createdAt||i.deviceDateCreate);if(!Number.isFinite(at)||at>Date.now())return false;if(i.interactionType==='CAS_REQUEST_CREATED')roots++;if(i.interactionType==='MESSAGE')messages.push(i);}}if(roots!==1)return false;if(last===null)return messages.length===0;return messages.some(i=>i._id===last._id&&i.createdAt===last.createdAt);}};
const cadenceDue=(row,items)=>{{const created=Date.parse(row.casChat.createdAt);if(!Number.isFinite(created)||Date.now()-created<86400000)return false;let newest=created;for(const i of items){{if(i.interactionType!=='MESSAGE'||i.isDeleted)continue;const role=i.user_rol.name;if(i.user.is_current||role==='SELLER'||role==='DROPSHIPPER')newest=Math.max(newest,Date.parse(i.createdAt||i.deviceDateCreate));}}return Date.now()-newest>=86400000;}};
if(action.kind==='search'){{const r=await post('/api/v1/chats/search',action.payload);return {{status:r.status,data:Array.isArray(r.data)?r.data.map(projectRow):null}};}}
if(action.kind==='counts'){{const r=await post('/api/v1/chats/counts',action.payload);const d=r.data;return {{status:r.status,data:d&&typeof d==='object'?{{queued:d.queued,active:d.active,postponed:d.postponed}}:null}};}}
if(action.kind==='thread'){{const r=await post('/api/v1/caschat/filterinteraction',{{casChat:action.chat_id}});return {{status:r.status,data:projectThread(r.data,action.expected_message)}};}}
if(action.kind==='send'){{const searches=await Promise.all(['queues','active'].map(s=>post('/api/v1/chats/search',Object.assign({{}},action.search_payload,{{status_chat:s}}))));if(searches.some(x=>x.status!==200))return {{status:0,preflight:'search_status'}};const rows=searches.flatMap(x=>Array.isArray(x.data)?x.data.map(projectRow):[]);if(rows.length!==1||!same({{casChat:rows[0].casChat,referenceObject:rows[0].referenceObject,casTicketType:rows[0].casTicketType}},action.expected_stable)||!same(rows[0].lastMessage,action.expected_last))return {{status:0,preflight:'identity'}};const th=await post('/api/v1/caschat/filterinteraction',{{casChat:action.chat_id}});if(![200,201].includes(th.status))return {{status:0,preflight:'thread_status'}};const safe=projectThread(th.data,null);if(!validThread(safe,action.chat_id,action.expected_last)||!same(safe,action.expected_thread)||!cadenceDue(rows[0],safe))return {{status:0,preflight:'thread'}};const body={{version:'2.0.4',deviceDateCreate:action.device_date,evidences:[],interactionType:'MESSAGE',casChats:[{{_id:action.chat_id,enterprise:action.enterprise}}],objectIntegrationType:{{message:action.message}},user}};const r=await post('/api/v1/caschat/interaction/create/massive',body);return {{status:r.status,preflight:'ok'}};}}
return {{ok:false,reason:'unknown_action'}};
}})()'''


class BoundedBrowserHarnessRunner:
    """Process-group-cleaning runner kept local to the follow-up feature."""
    def __init__(self, command: str = "browser-harness"):
        self.command = command

    def execute_json(self, code: str, timeout_seconds: int = 90) -> dict[str, Any]:
        if timeout_seconds < 1:
            raise CycleFailure("deadline")
        process = subprocess.Popen([self.command], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True)
        try:
            stdout, _ = process.communicate(code, timeout=timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                pass
            # A parent can exit and close its pipes while a child retains the
            # process group.  Kill the group after the grace period regardless.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.communicate()
            raise CycleFailure("browser_timeout") from exc
        if process.returncode != 0:
            raise CycleFailure("browser_failed")
        for line in reversed(stdout.splitlines()):
            if line.startswith("__JSON__"):
                try:
                    value = json.loads(line[len("__JSON__"):])
                except json.JSONDecodeError as exc:
                    raise CycleFailure("browser_json") from exc
                if isinstance(value, dict):
                    return value
        raise CycleFailure("browser_json")


class BrowserCASClient:
    def __init__(self, runner: BrowserRunner):
        self.runner, self._initialized, self._deadline = runner, False, None

    def set_deadline(self, deadline: float) -> None:
        self._deadline = deadline

    def _timeout(self) -> int:
        remaining = 90.0 if self._deadline is None else self._deadline - time.monotonic()
        if remaining <= 0:
            raise CycleFailure("deadline")
        return max(1, min(90, int(remaining)))

    def _call(self, action: dict[str, Any]) -> dict[str, Any]:
        if not self._initialized:
            source = "import json\nimport time\ntabs=list_tabs(include_chrome=False)\ntarget=next((t for t in tabs if '/dashboard/cas' in (t.get('url') or '')),None) or next((t for t in tabs if 'app.dropi.co' in (t.get('url') or '')),None)\nif target: switch_tab(target)\nelse: new_tab('https://app.dropi.co/dashboard/cas/tray')\ngoto_url('https://app.dropi.co/dashboard/cas/tray')\nwait_for_load(20)\n"
            self._initialized = True
        else:
            source = "import json\n"
        source += "print('__JSON__'+json.dumps(js(" + repr(browser_program(action)) + "),ensure_ascii=False))\n"
        result = self.runner.execute_json(source, timeout_seconds=self._timeout())
        if not isinstance(result, dict) or result.get("reason") in {"session_missing", "route_missing"}:
            raise CycleFailure("authentication")
        return result

    def request(self, payload: dict[str, Any]) -> dict[str, Any]: return self._call({"kind": "search", "payload": payload})
    def counts(self, payload: dict[str, Any]) -> dict[str, Any]: return self._call({"kind": "counts", "payload": payload})
    def thread(self, chat_id: str, *, expected_message: str | None = None) -> list[dict[str, Any]]:
        action: dict[str, Any] = {"kind": "thread", "chat_id": chat_id}
        if expected_message is not None: action["expected_message"] = expected_message
        return _response_rows(self._call(action), allowed_statuses=(200, 201))
    def send_once(self, *, chat_id: str, enterprise: str, message: str, device_date: datetime, exact_row: dict[str, Any], before_thread: list[dict[str, Any]]) -> bool:
        identity = _row_identity(exact_row)
        reference = exact_row["referenceObject"]
        expected_stable = {"casChat": {key: _chat(exact_row).get(key) for key in ("_id", "status", "enterprise", "createdAt", "datePostponed")}, "referenceObject": {"id": reference.get("id"), "type": reference.get("type"), "data": {key: (reference.get("data") or {}).get(key) for key in ("shipping_guide", "shipping_company")}}, "casTicketType": {"_id": identity[4], "name": identity[5]}}
        expected_last = None if identity[6] is None else {"_id": identity[6], "createdAt": exact_row["lastMessage"].get("createdAt")}
        result = self._call({"kind":"send","chat_id":chat_id,"enterprise":enterprise,"message":message,"device_date":device_date.isoformat(),"search_payload":_listing_payload("queues",0,10,chat_id),"expected_stable":expected_stable,"expected_last":expected_last,"expected_thread":before_thread})
        return result.get("preflight") == "ok" and result.get("status") in {200, 201}


def verify_delivery(interactions: object, *, chat_id: str, expected_last_message_id: str | None, prior_ids: set[str], message: str, intent_at: datetime, now: datetime | None = None) -> str:
    inspect_thread(interactions, chat_id=chat_id, expected_last_message_id=expected_last_message_id, allow_newer_messages=True, now=now)
    matches: list[str] = []
    for item in interactions:
        assert isinstance(item, dict)
        if item.get("_id") in prior_ids or item.get("interactionType") != "MESSAGE" or item.get("isDeleted") is True: continue
        user, at = item.get("user"), _interaction_time(item)
        if not (isinstance(user, dict) and user.get("is_current") is True and at is not None and at >= intent_at and item.get("message_exact") is True): continue
        matches.append(item["_id"])
    if len(matches) != 1: raise CycleFailure("readback_mismatch")
    return matches[0]


@contextmanager
def journal_lock(path: Path, *, timeout_seconds: float) -> Iterator[None]:
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB); break
            except BlockingIOError:
                if time.monotonic() >= deadline: raise CycleFailure("journal_lock_timeout")
                time.sleep(min(.1, max(0.0, deadline - time.monotonic())))
        yield
    finally:
        try: fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally: os.close(descriptor)


def _reason(counts: dict[str, int], reasons: dict[str, int], code: str) -> None:
    counts["blocked"] += 1; reasons[code] = reasons.get(code, 0) + 1


def _failure_code(exc: Exception, fallback: str) -> str:
    """Keep aggregate reports finite without hiding explicit safe-stop codes."""
    if isinstance(exc, CycleFailure) and str(exc) in REPORT_FAILURE_CODES:
        return str(exc)
    return fallback


def _message_timestamp(items: list[dict[str, Any]], message_id: str) -> datetime:
    for item in items:
        if item.get("_id") == message_id:
            timestamp = _interaction_time(item)
            if timestamp is not None:
                return timestamp
    raise CycleFailure("readback_mismatch")


def run_followup_cycle(client: Any, journal: DurableJournal, *, allow_external_read: bool, allow_external_writes: bool = False, now: datetime | None = None, clock: Callable[[], datetime] | None = None, budget_seconds: int = 7200, reserve_seconds: int = 240, deadline: float | None = None) -> dict[str, Any]:
    if not allow_external_read: raise PermissionError("External reads are disabled. Pass --allow-external-read explicitly.")
    if budget_seconds <= reserve_seconds or reserve_seconds < 0: raise ValueError("invalid cycle budget")
    clock = clock or (lambda: datetime.now(timezone.utc))
    now = now or clock()
    deadline = deadline if deadline is not None else time.monotonic() + budget_seconds
    if hasattr(client, "set_deadline"): client.set_deadline(deadline)
    counts = {key: 0 for key in ("inventory", "reviewed", "due", "cooldown", "postponed", "blocked", "sent", "verified")}; reasons: dict[str, int] = {}
    try:
        rows, _totals = inventory(client.request, client.counts, deadline=deadline)
    except Exception:
        _reason(counts, reasons, "inventory")
        return sanitize_summary(**counts, reasons=reasons)
    counts["inventory"] = len(rows)
    for row in rows:
        if time.monotonic() >= deadline: _reason(counts, reasons, "deadline"); break
        counts["reviewed"] += 1
        try:
            chat_id, order_id, guide, enterprise, _tid, _tname, last_id, _last_at = _row_identity(row)
            if _chat(row).get("status") == "postponed": counts["postponed"] += 1; continue
            if _chat(row).get("status") not in OPEN_STATES: _reason(counts,reasons,"state"); continue
            journal_at, journal_block = journal.assess(chat_id)
            if journal_block: _reason(counts,reasons,journal_block); continue
            prior_thread = client.thread(chat_id)
            due, reason = is_due(row, prior_thread, journal_at=journal_at, now=now)
        except Exception as exc: _reason(counts,reasons,_failure_code(exc, "preflight")); continue
        if not due:
            if reason == "cooldown": counts["cooldown"] += 1
            else: _reason(counts,reasons,reason)
            continue
        counts["due"] += 1
        if not allow_external_writes: continue
        if deadline - time.monotonic() <= reserve_seconds: _reason(counts,reasons,"reserve"); continue
        message, digest = followup_text(order_id, guide), message_hash(followup_text(order_id, guide))
        try:
            with journal_lock(journal.path, timeout_seconds=min(90.0, max(0.0, deadline-time.monotonic()))):
                journal_at, journal_block = journal.assess(chat_id)
                if journal_block: _reason(counts,reasons,journal_block); continue
                exact = exact_revalidate(client.request, row)
                exact_last_id, exact_last_at = _last_message_snapshot(exact)
                before_thread = client.thread(chat_id)
                prior_ids, _client, _carrier = inspect_thread(before_thread, chat_id=chat_id, expected_last_message_id=exact_last_id, expected_last_message_at=exact_last_at, now=now)
                redue, reason = is_due(exact, before_thread, journal_at=journal_at, now=now)
                if not redue: counts["cooldown"] += 1; continue
                if deadline - time.monotonic() <= reserve_seconds: _reason(counts,reasons,"reserve"); continue
                # Intent time is the real instant immediately before the only
                # write.  A frozen inventory clock must never authenticate an
                # older, look-alike message on readback.
                intent_at, attempt_id = clock(), secrets.token_hex(16)
                journal.append({"status":"write_intent","chat_id":chat_id,"attempt_id":attempt_id,"at":intent_at.isoformat(),"message_hash":digest})
                acknowledged = False
                try: acknowledged = bool(client.send_once(chat_id=chat_id, enterprise=enterprise, message=message, device_date=intent_at, exact_row=exact, before_thread=before_thread))
                except Exception: acknowledged = False
                message_id: str | None = None
                for _ in range(3):
                    if time.monotonic() >= deadline: break
                    try:
                        after_row = exact_revalidate(client.request, row)
                        after_thread = client.thread(chat_id, expected_message=message)
                        readback_now = clock()
                        candidate_message_id = verify_delivery(after_thread, chat_id=chat_id, expected_last_message_id=exact_last_id, prior_ids=prior_ids, message=message, intent_at=intent_at, now=readback_now)
                        fresh_last_id, fresh_last_at = _last_message_snapshot(after_row)
                        if fresh_last_id != candidate_message_id or fresh_last_at != _message_timestamp(after_thread, candidate_message_id):
                            raise CycleFailure("readback_mismatch")
                        # Publish success only after every readback condition passes.
                        message_id = candidate_message_id
                        break
                    except (CycleFailure, OSError, ValueError): continue
                if message_id is None:
                    journal.append({"status":"blocked_remote_readback_mismatch","chat_id":chat_id,"attempt_id":attempt_id,"at":clock().isoformat(),"message_hash":digest})
                    _reason(counts,reasons,"readback"); continue
                journal.append({"status":"write_verified","chat_id":chat_id,"attempt_id":attempt_id,"at":clock().isoformat(),"message_hash":digest,"message_id":message_id})
                counts["verified"] += 1
                if acknowledged: counts["sent"] += 1
        except Exception as exc: _reason(counts,reasons,_failure_code(exc, "write"))
    return sanitize_summary(**counts, reasons=reasons)


def atomic_report(path: Path, payload: dict[str, Any]) -> None:
    report = path.expanduser().resolve(); report.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temp = report.parent / f".{report.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    with temp.open("x", encoding="utf-8") as handle:
        os.chmod(temp, 0o600); json.dump(payload, handle, separators=(",", ":"), sort_keys=True); handle.write("\n"); handle.flush(); os.fsync(handle.fileno())
    os.replace(temp, report); os.chmod(report, 0o600)


def sanitize_summary(**counts: Any) -> dict[str, Any]:
    reasons = counts.get("reasons")
    safe_reasons = reasons if isinstance(reasons, dict) else {}
    blocked = int(counts.get("blocked", 0))
    processed = sum(int(counts.get(key, 0)) for key in ("reviewed", "due", "cooldown", "postponed", "sent", "verified"))
    # The report is the durable operational record: never use a raw exception
    # or remote response as a reason.  A compact primary code is useful to the
    # systemd post-phase, while the counted map remains auditable.
    return {"ok": blocked == 0, "partial": blocked > 0 and processed > 0, "block_reason": (next(iter(sorted(safe_reasons))) if len(safe_reasons) == 1 else ("multiple" if safe_reasons else None)), **counts}
