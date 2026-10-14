from __future__ import annotations

import importlib.util
import json
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


MODULE_PATH = Path(__file__).parents[1] / "src" / "dropi_cas_automation" / "cas_followup_cycle.py"


def load_module():
    spec = importlib.util.spec_from_file_location("cas_followup_cycle", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CHAT = "a" * 24


def row(chat_id: str = CHAT, status: str = "active", *, guide: str = "GUIDE-1", order: int = 55) -> dict[str, object]:
    return {
        "casChat": {"_id": chat_id, "status": status, "enterprise": "enterprise-1", "createdAt": "2026-01-01T00:00:00Z"},
        "referenceObject": {"id": order, "type": "ORDER", "data": {"shipping_guide": guide}},
        "casTicketType": {"_id": "ticket", "name": "unknown"},
        "lastMessage": {"_id": "last", "createdAt": "2026-01-01T00:00:00Z"},
    }


def root(chat_id: str = CHAT) -> dict[str, object]:
    return {"_id": "root", "casChat": chat_id, "interactionType": "CAS_REQUEST_CREATED", "isDeleted": False, "createdAt": "2026-01-01T00:00:00Z"}


def old_message(chat_id: str = CHAT, *, at: str = "2026-01-01T00:00:00Z") -> dict[str, object]:
    return {"_id": "last", "casChat": chat_id, "interactionType": "MESSAGE", "isDeleted": False, "createdAt": at, "user_rol": {"name": "AGENT"}, "user": {"_id": "agent"}}


def client_message(chat_id: str = CHAT, *, at: str = "2026-01-01T00:00:00Z") -> dict[str, object]:
    return {"_id": "client", "casChat": chat_id, "interactionType": "MESSAGE", "isDeleted": False, "createdAt": at, "user_rol": {"name": "SELLER"}, "user": {"_id": "buyer"}}


class PaginationAndIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = load_module()

    def test_inventory_continues_after_short_page_until_empty(self) -> None:
        calls: list[dict[str, object]] = []
        pages = [[{"casChat": {"_id": "a", "status": "queues"}}], [{"casChat": {"_id": "b", "status": "queues"}}], []]

        def request(payload: dict[str, object]) -> dict[str, object]:
            calls.append(payload)
            return {"status": 200, "data": pages.pop(0)}

        rows, complete = self.module.enumerate_status(request, "queues", page_size=10)
        self.assertTrue(complete)
        self.assertEqual([item["casChat"]["_id"] for item in rows], ["a", "b"])
        self.assertEqual([call["offset"] for call in calls], [0, 1, 2])

    def test_inventory_blocks_duplicate_and_count_mismatch(self) -> None:
        def duplicate(payload: dict[str, object]) -> dict[str, object]:
            status, offset = payload["status_chat"], payload["offset"]
            values = {"queues": [row("same", "queues")], "active": [row("same", "active")], "postponed": []}[status] if offset == 0 else []
            return {"status": 200, "data": values}
        with self.assertRaisesRegex(self.module.CycleFailure, "duplicate_chat"):
            self.module.inventory(duplicate, lambda _: {"status": 200, "data": {"queued": 1, "active": 1, "postponed": 0}})

        def normal(payload: dict[str, object]) -> dict[str, object]:
            values = {"queues": [], "active": [row()], "postponed": []}[payload["status_chat"]] if payload["offset"] == 0 else []
            return {"status": 200, "data": values}
        with self.assertRaisesRegex(self.module.CycleFailure, "counts_mismatch"):
            self.module.inventory(normal, lambda _: {"status": 200, "data": {"queued": 0, "active": 2, "postponed": 0}})

        count_calls = 0
        def drifting_counts(_payload: dict[str, object]) -> dict[str, object]:
            nonlocal count_calls
            count_calls += 1
            return {"status": 200, "data": {"queued": 0, "active": count_calls, "postponed": 0}}
        with self.assertRaisesRegex(self.module.CycleFailure, "counts_changed"):
            self.module.inventory(normal, drifting_counts)

    def test_exact_revalidation_rejects_close_identity_and_duplicates(self) -> None:
        source = row()
        def wrong_identity(payload: dict[str, object]) -> dict[str, object]:
            return {"status": 200, "data": [row(guide="other")] if payload["status_chat"] == "active" else []}
        with self.assertRaisesRegex(self.module.CycleFailure, "revalidation_identity"):
            self.module.exact_revalidate(wrong_identity, source)

        def duplicate(payload: dict[str, object]) -> dict[str, object]:
            return {"status": 200, "data": [source]}
        with self.assertRaisesRegex(self.module.CycleFailure, "revalidation_not_exact"):
            self.module.exact_revalidate(duplicate, source)


class ThreadCooldownAndReadbackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = load_module()
        self.now = datetime(2026, 1, 3, tzinfo=timezone.utc)

    def test_thread_parent_and_new_case_cooldown_fail_closed(self) -> None:
        with self.assertRaisesRegex(self.module.CycleFailure, "thread_parent_mismatch"):
            self.module.inspect_thread([root("other")], chat_id=CHAT, expected_last_message_id="root")
        fresh = row(); fresh["lastMessage"] = None
        fresh["casChat"]["createdAt"] = "2026-01-02T12:00:00Z"
        due, reason = self.module.is_due(fresh, [root()], journal_at=None, now=self.now)
        self.assertFalse(due)
        self.assertEqual(reason, "cooldown")

    def test_client_and_carrier_recent_messages_hold_followup(self) -> None:
        source = row()
        recent = (self.now - timedelta(hours=2)).isoformat()
        client = client_message(at=recent); client["_id"] = "last"; source["lastMessage"] = {"_id": "last", "createdAt": recent}
        due, reason = self.module.is_due(source, [root(), client], journal_at=None, now=self.now)
        self.assertFalse(due)
        self.assertEqual(reason, "cooldown")
        carrier = dict(client_message(at=recent))
        carrier["_id"] = "last"
        carrier["user_rol"] = {"name": "CARRIER"}
        due, reason = self.module.is_due(source, [root(), carrier], journal_at=None, now=self.now)
        self.assertFalse(due)
        self.assertEqual(reason, "cooldown")

    def test_listing_last_message_must_be_message_with_matching_latest_timestamp(self) -> None:
        source = row()
        mismatched = old_message(at="2026-01-01T00:00:01Z")
        with self.assertRaisesRegex(self.module.CycleFailure, "thread_last_message_timestamp"):
            self.module.is_due(source, [root(), mismatched], journal_at=None, now=self.now)
        newer = {**old_message(), "_id": "newer", "createdAt": "2026-01-02T00:00:00Z"}
        with self.assertRaisesRegex(self.module.CycleFailure, "thread_last_message_stale"):
            self.module.is_due(source, [root(), old_message(), newer], journal_at=None, now=self.now)

    def test_readback_requires_new_exact_non_deleted_message(self) -> None:
        before = datetime.now(timezone.utc)
        message = self.module.followup_text("55", "GUIDE-1")
        result = self.module.verify_delivery([root(), old_message(), {"_id": "new", "casChat": CHAT, "interactionType": "MESSAGE", "isDeleted": False, "createdAt": datetime.now(timezone.utc).isoformat(), "user": {"is_current": True}, "user_rol": {"name": "AGENT"}, "message_exact": True}], chat_id=CHAT, expected_last_message_id="last", prior_ids={"root", "last"}, message=message, intent_at=before)
        self.assertEqual(result, "new")
        with self.assertRaisesRegex(self.module.CycleFailure, "readback_mismatch"):
            self.module.verify_delivery([root(), old_message(), {"_id": "old", "casChat": CHAT, "interactionType": "MESSAGE", "isDeleted": True, "createdAt": datetime.now(timezone.utc).isoformat(), "user": {"is_current": True}, "user_rol": {"name": "AGENT"}, "message_exact": True}], chat_id=CHAT, expected_last_message_id="last", prior_ids={"root", "last"}, message=message, intent_at=before)

    def test_readback_rejects_matching_message_predating_real_intent(self) -> None:
        intent_at = datetime(2026, 10, 14, 21, 32, tzinfo=timezone.utc)
        earlier = intent_at - timedelta(minutes=4)
        with self.assertRaisesRegex(self.module.CycleFailure, "readback_mismatch"):
            self.module.verify_delivery([root(), old_message(), {"_id": "lookalike", "casChat": CHAT, "interactionType": "MESSAGE", "isDeleted": False, "createdAt": earlier.isoformat(), "user": {"is_current": True}, "user_rol": {"name": "AGENT"}, "message_exact": True}], chat_id=CHAT, expected_last_message_id="last", prior_ids={"root", "last"}, message="ignored", intent_at=intent_at, now=intent_at)


class BrowserProgramTests(unittest.TestCase):
    def test_node_mock_auth_and_send_payload_are_safe(self) -> None:
        module = load_module()
        message = module.followup_text("55", "GUIDE-1")
        program = module.browser_program({"kind": "send", "chat_id": "chat-1", "enterprise": "enterprise-1", "message": message, "device_date": "2026-01-03T00:00:00+00:00"})
        source = f"""
const calls=[];
global.location={{origin:'https://app.dropi.co',pathname:'/dashboard/cas/tray'}};
global.localStorage={{getItem:(key)=>key==='casToken'?'\\\"token-secret\\\"':key==='casUser'?'{{\\\"_id\\\":\\\"operator\\\"}}':null}};
global.fetch=async (url, options)=>{{calls.push([url, options]); return {{status:200,json:async()=>({{data:[]}})}};}};
eval({json.dumps(program)}).then((result)=>console.log(JSON.stringify({{result,calls}})));
"""
        completed = subprocess.run(["node", "-e", source], check=True, capture_output=True, text=True)
        output = json.loads(completed.stdout)
        self.assertEqual(output["result"].get("preflight"), "identity")
        self.assertEqual(len(output["calls"]), 2)
        self.assertTrue(all(url.endswith("/api/v1/chats/search") for url, _ in output["calls"]))

    def test_node_mock_missing_auth_never_fetches(self) -> None:
        module = load_module()
        program = module.browser_program({"kind": "search", "payload": {}})
        source = f"global.location={{origin:'https://app.dropi.co',pathname:'/dashboard/cas/tray'}};global.localStorage={{getItem:()=>null}};global.fetch=()=>{{throw new Error('must not fetch')}};eval({json.dumps(program)}).then(x=>console.log(JSON.stringify(x)));"
        completed = subprocess.run(["node", "-e", source], check=True, capture_output=True, text=True)
        self.assertEqual(json.loads(completed.stdout), {"ok": False, "reason": "session_missing"})
    def test_node_thread_readback_is_sanitized_and_accepts_201(self) -> None:
        module = load_module()
        expected = module.followup_text("55", "GUIDE-1")
        program = module.browser_program({"kind": "thread", "chat_id": "chat-1", "expected_message": expected})
        source = f"""
global.location={{origin:'https://app.dropi.co',pathname:'/dashboard/cas/tray'}};
global.localStorage={{getItem:(key)=>key==='casToken'?'\\\"token\\\"':'{{\\\"_id\\\":\\\"operator\\\"}}'}};
global.fetch=async ()=>({{status:201,json:async()=>({{data:[{{_id:'new',casChat:'chat-1',interactionType:'MESSAGE',createdAt:'2026-01-01T00:00:00Z',user:{{_id:'operator'}},user_rol:{{name:'AGENT'}},objectIntegrationType:{{message:{json.dumps(expected)}}},secret:'buyer PII'}}]}})}});
eval({json.dumps(program)}).then(x=>console.log(JSON.stringify(x)));
"""
        completed = subprocess.run(["node", "-e", source], check=True, capture_output=True, text=True)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["status"], 201)
        self.assertTrue(payload["data"][0]["message_exact"])
        self.assertTrue(payload["data"][0]["user"]["is_current"])
        self.assertNotIn("buyer PII", completed.stdout)
        self.assertNotIn("objectIntegrationType", completed.stdout)

    def test_node_atomic_preflight_uses_full_projection_and_one_post(self) -> None:
        module = load_module()
        chat = "chat-live-shape"
        raw_row = {"casChat": {"_id": chat, "status": "active", "enterprise": "enterprise", "createdAt": "2020-01-01T00:00:00Z"}, "referenceObject": {"id": "55", "type": "ORDER", "data": {"shipping_guide": "guide"}}, "casTicketType": {"_id": "ticket", "name": "ticket-name"}, "lastMessage": {"_id": "old", "createdAt": "2020-01-02T00:00:00Z"}}
        raw_thread = [
            {"_id": "root", "casChat": chat, "interactionType": "CAS_REQUEST_CREATED", "createdAt": "2020-01-01T00:00:00Z"},
            {"_id": "old", "casChat": chat, "interactionType": "MESSAGE", "createdAt": "2020-01-02T00:00:00Z", "user": {"_id": "other"}, "user_rol": {"name": "AGENT"}},
        ]
        expected_thread = [
            {"_id": "root", "casChat": chat, "interactionType": "CAS_REQUEST_CREATED", "isDeleted": False, "createdAt": "2020-01-01T00:00:00Z", "deviceDateCreate": None, "user": {"is_current": False}, "user_rol": {"name": None}, "message_exact": False},
            {"_id": "old", "casChat": chat, "interactionType": "MESSAGE", "isDeleted": False, "createdAt": "2020-01-02T00:00:00Z", "deviceDateCreate": None, "user": {"is_current": False}, "user_rol": {"name": "AGENT"}, "message_exact": False},
        ]
        action = {"kind": "send", "chat_id": chat, "enterprise": "enterprise", "message": "exact safe text", "device_date": "2026-10-14T21:32:00+00:00", "search_payload": {"offset": 0, "limit": 10, "search_term": chat}, "expected_stable": {"casChat": {"_id": chat, "status": "active", "enterprise": "enterprise", "createdAt": "2020-01-01T00:00:00Z", "datePostponed": None}, "referenceObject": {"id": "55", "type": "ORDER", "data": {"shipping_guide": "guide", "shipping_company": None}}, "casTicketType": {"_id": "ticket", "name": "ticket-name"}}, "expected_last": raw_row["lastMessage"], "expected_thread": expected_thread}

        def node_result(row_value, thread_value, *, search_status=200, action_value=action):
            source = f'''const calls=[];const row={json.dumps(row_value)};const thread={json.dumps(thread_value)};global.location={{origin:'https://app.dropi.co',pathname:'/dashboard/cas/tray'}};global.localStorage={{getItem:k=>k==='casToken'?'"token"':'{{"_id":"operator"}}'}};global.fetch=async(url,options)=>{{calls.push([url,JSON.parse(options.body)]);if(url.endsWith('/chats/search')){{const q=calls[calls.length-1][1];return {{status:{search_status},json:async()=>({{data:q.status_chat==='queues'?[row]:[]}})}}}}if(url.endsWith('/filterinteraction'))return {{status:201,json:async()=>({{data:thread}})}};return {{status:201,json:async()=>({{data:{{}}}})}};}};eval({json.dumps(module.browser_program(action_value))}).then(result=>console.log(JSON.stringify({{result,calls}})));'''
            return json.loads(subprocess.run(["node", "-e", source], check=True, capture_output=True, text=True).stdout)

        valid = node_result(raw_row, raw_thread)
        self.assertEqual((valid["result"]["preflight"], valid["result"]["status"]), ("ok", 201))
        self.assertEqual(len(valid["calls"]), 4)
        self.assertEqual(valid["calls"][-1][1]["objectIntegrationType"]["message"], "exact safe text")
        self.assertEqual(valid["calls"][-1][1]["casChats"], [{"_id": chat, "enterprise": "enterprise"}])

        self.assertEqual(len(node_result(raw_row, raw_thread, search_status=500)["calls"]), 2)
        ticket_changed = json.loads(json.dumps(raw_row)); ticket_changed["casTicketType"]["_id"] = "changed"
        self.assertEqual(len(node_result(ticket_changed, raw_thread)["calls"]), 2)
        edited = json.loads(json.dumps(raw_thread)); edited[1]["createdAt"] = "2020-01-02T00:00:01Z"
        self.assertEqual(len(node_result(raw_row, edited)["calls"]), 3)
        unknown = json.loads(json.dumps(raw_thread)); unknown[0]["interactionType"] = "UNRECOGNIZED"
        self.assertEqual(len(node_result(raw_row, unknown)["calls"]), 3)
        nullable_row = json.loads(json.dumps(raw_row)); nullable_row["lastMessage"] = None
        nullable_action = json.loads(json.dumps(action)); nullable_action["expected_last"] = None; nullable_action["expected_thread"] = [expected_thread[0]]
        self.assertEqual(len(node_result(nullable_row, [raw_thread[0]], action_value=nullable_action)["calls"]), 4)

    def test_node_search_projection_preserves_empty_last_message_semantics_and_blocks_malformed_values(self) -> None:
        """Exercise the generated JS projection before Python reviews the rows."""
        module = load_module()

        def project(rows):
            program = module.browser_program({"kind": "search", "payload": {"status_chat": "active"}})
            source = f'''global.location={{origin:'https://app.dropi.co',pathname:'/dashboard/cas/tray'}};global.localStorage={{getItem:k=>k==='casToken'?'"token"':'{{"_id":"operator"}}'}};global.fetch=async()=>({{status:200,json:async()=>({{data:{json.dumps(rows)}}})}});eval({json.dumps(program)}).then(x=>console.log(JSON.stringify(x)));'''
            completed = subprocess.run(["node", "-e", source], check=True, capture_output=True, text=True)
            return json.loads(completed.stdout)["data"]

        fixture = json.loads((Path(__file__).parent / "fixtures" / "cas_sanitized.json").read_text(encoding="utf-8"))
        projected_rows = project([entry["row"] for entry in fixture])
        self.assertEqual(len(projected_rows), 14)
        by_chat = {entry["row"]["casChat"]["_id"]: entry for entry in fixture}

        class ProjectedReadOnlyClient:
            def request(self, payload):
                values = [item for item in projected_rows if item["casChat"]["status"] == payload["status_chat"]]
                if payload.get("search_term"):
                    values = [item for item in values if item["casChat"]["_id"] == payload["search_term"]]
                return {"status": 200, "data": values if payload["offset"] == 0 else []}
            def counts(self, _payload):
                return {"status": 200, "data": {"queued": 4, "active": 10, "postponed": 0}}
            def thread(self, chat_id, *, expected_message=None):
                assert expected_message is None, "read-only cycle must not send"
                return by_chat[chat_id]["thread"]
            def send_once(self, **_kwargs):
                raise AssertionError("read-only cycle must not send")

        with tempfile.TemporaryDirectory() as directory:
            result = module.run_followup_cycle(ProjectedReadOnlyClient(), module.DurableJournal(Path(directory) / "journal.jsonl"), allow_external_read=True, allow_external_writes=False, now=datetime(2026, 10, 14, 21, 32, tzinfo=timezone.utc))
        self.assertEqual((result["inventory"], result["reviewed"], result["due"], result["cooldown"], result["blocked"]), (14, 14, 10, 4, 0))

        no_message = row()
        no_message_chat = no_message["casChat"]
        assert isinstance(no_message_chat, dict)
        no_message_chat["createdAt"] = "2026-01-01T00:00:00Z"
        for name, raw in (("empty", {**no_message, "lastMessage": {}}), ("null", {**no_message, "lastMessage": None}), ("missing", dict(no_message))):
            if name == "missing":
                raw.pop("lastMessage")
            with self.subTest(name=name):
                projected = project([raw])[0]
                self.assertIsNone(projected["lastMessage"])
                self.assertEqual(module.is_due(projected, [root()], journal_at=None, now=datetime(2026, 1, 3, tzinfo=timezone.utc)), (True, "due"))

        malformed = {
            "null_fields": {"_id": None, "createdAt": None},
            "id_only": {"_id": "last"},
            "timestamp_only": {"createdAt": "2026-01-01T00:00:00Z"},
            "string": "not-an-object",
            "array": ["not-an-object"],
            "boolean": False,
        }
        for name, raw_last_message in malformed.items():
            with self.subTest(name=name):
                raw = {**no_message, "lastMessage": raw_last_message}
                projected = project([raw])[0]
                if name in {"string", "array", "boolean"}:
                    self.assertEqual(projected["lastMessage"], {"_id": None, "createdAt": None})
                with self.assertRaisesRegex(module.CycleFailure, "row_last_message"):
                    module._row_identity(projected)


class FakeClient:
    def __init__(self, module, *, fail_readback: bool = False) -> None:
        self.module = module
        self.item = row()
        self.sent = 0
        self.thread_calls = 0
        self.fail_readback = fail_readback

    def request(self, payload):
        if payload.get("search_term"):
            return {"status": 200, "data": [self.item] if payload["status_chat"] == "active" else []}
        if payload["offset"] == 0 and payload["status_chat"] == "active":
            return {"status": 200, "data": [self.item]}
        return {"status": 200, "data": []}

    def counts(self, _payload):
        return {"status": 200, "data": {"queued": 0, "active": 1, "postponed": 0}}

    def thread(self, chat_id, *, expected_message=None):
        self.thread_calls += 1
        values = [root(chat_id), old_message(chat_id)]
        if self.sent and not self.fail_readback:
            values.append({"_id": "sent-id", "casChat": chat_id, "interactionType": "MESSAGE", "isDeleted": False, "createdAt": self.sent_at, "user": {"is_current": True}, "user_rol": {"name": "AGENT"}, "message_exact": expected_message is not None})
        return values

    def send_once(self, **kwargs):
        self.sent += 1
        self.sent_at = kwargs["device_date"].isoformat()
        self.item["lastMessage"] = {"_id": "sent-id", "createdAt": self.sent_at}
        return True


class CycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = load_module()

    def test_dry_run_never_writes_journal_or_send(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient(self.module)
            journal = self.module.DurableJournal(Path(directory) / "journal.jsonl")
            result = self.module.run_followup_cycle(client, journal, allow_external_read=True, allow_external_writes=False, now=datetime(2026, 1, 3, tzinfo=timezone.utc))
            self.assertEqual(result["due"], 1)
            self.assertEqual(client.sent, 0)
            self.assertFalse(journal.path.exists())

    def test_send_then_exact_readback_records_hashed_journal_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient(self.module)
            journal = self.module.DurableJournal(Path(directory) / "journal.jsonl")
            result = self.module.run_followup_cycle(client, journal, allow_external_read=True, allow_external_writes=True, now=datetime(2026, 1, 3, tzinfo=timezone.utc), clock=lambda: datetime(2026, 1, 3, tzinfo=timezone.utc))
            self.assertEqual((result["sent"], result["verified"], result["blocked"]), (1, 1, 0))
            journal_text = journal.path.read_text(encoding="utf-8")
            self.assertIn('"write_intent"', journal_text)
            self.assertIn('"write_verified"', journal_text)
            self.assertNotIn("Buen día", journal_text)
            self.assertNotIn("GUIDE-1", journal_text)

    def test_failed_readback_marks_uncertainty_and_never_retries_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient(self.module, fail_readback=True)
            journal = self.module.DurableJournal(Path(directory) / "journal.jsonl")
            result = self.module.run_followup_cycle(client, journal, allow_external_read=True, allow_external_writes=True, now=datetime(2026, 1, 3, tzinfo=timezone.utc), clock=lambda: datetime(2026, 1, 3, tzinfo=timezone.utc))
            self.assertEqual(client.sent, 1)
            self.assertEqual(result["verified"], 0)
            self.assertEqual(result["blocked"], 1)
            self.assertIn("blocked_remote_readback_mismatch", journal.path.read_text(encoding="utf-8"))

    def test_close_between_post_and_readback_is_uncertain_not_verified(self) -> None:
        class ClosedAfterPost(FakeClient):
            def request(self, payload):
                if self.sent and payload.get("search_term"):
                    return {"status": 200, "data": []}
                return super().request(payload)
        with tempfile.TemporaryDirectory() as directory:
            client = ClosedAfterPost(self.module)
            journal = self.module.DurableJournal(Path(directory) / "journal.jsonl")
            result = self.module.run_followup_cycle(client, journal, allow_external_read=True, allow_external_writes=True, now=datetime(2026, 1, 3, tzinfo=timezone.utc), clock=lambda: datetime(2026, 1, 3, tzinfo=timezone.utc))
            self.assertEqual((client.sent, result["sent"], result["verified"], result["blocked"]), (1, 0, 0, 1))
            self.assertIn("blocked_remote_readback_mismatch", journal.path.read_text(encoding="utf-8"))

    def test_readback_candidate_is_not_verified_when_listing_last_message_stays_stale_or_mismatched(self) -> None:
        class StaleListingAfterPost(FakeClient):
            def __init__(self, module, fresh_last_message):
                super().__init__(module)
                self.fresh_last_message = fresh_last_message

            def send_once(self, **kwargs):
                acknowledged = super().send_once(**kwargs)
                self.item["lastMessage"] = self.fresh_last_message(self.sent_at)
                return acknowledged

        cases = {
            "stale_id": lambda _sent_at: {"_id": "last", "createdAt": "2026-01-01T00:00:00Z"},
            "timestamp_mismatch": lambda sent_at: {"_id": "sent-id", "createdAt": "2026-01-03T00:00:01+00:00" if sent_at == "2026-01-03T00:00:00+00:00" else sent_at + ".001"},
        }
        frozen = datetime(2026, 1, 3, tzinfo=timezone.utc)
        for name, fresh_last_message in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                client = StaleListingAfterPost(self.module, fresh_last_message)
                journal = self.module.DurableJournal(Path(directory) / "journal.jsonl")
                result = self.module.run_followup_cycle(client, journal, allow_external_read=True, allow_external_writes=True, now=frozen, clock=lambda: frozen)
                self.assertEqual((result["sent"], result["verified"], result["blocked"]), (0, 0, 1))
                self.assertEqual(client.sent, 1)
                self.assertEqual(journal.assess(CHAT), (None, "journal_uncertain"))
                self.assertIn("blocked_remote_readback_mismatch", journal.path.read_text(encoding="utf-8"))
                retry = self.module.run_followup_cycle(client, journal, allow_external_read=True, allow_external_writes=True, now=frozen, clock=lambda: frozen)
                self.assertEqual(client.sent, 1)
                self.assertIn("journal_uncertain", retry["reasons"])

    def test_uncertain_http_response_is_verified_by_readback_but_not_counted_sent(self) -> None:
        class Uncertain(FakeClient):
            def send_once(self, **kwargs):
                super().send_once(**kwargs)
                return False
        with tempfile.TemporaryDirectory() as directory:
            client = Uncertain(self.module)
            journal = self.module.DurableJournal(Path(directory) / "journal.jsonl")
            result = self.module.run_followup_cycle(client, journal, allow_external_read=True, allow_external_writes=True, now=datetime(2026, 1, 3, tzinfo=timezone.utc), clock=lambda: datetime(2026, 1, 3, tzinfo=timezone.utc))
            self.assertEqual((result["sent"], result["verified"]), (0, 1))
            self.assertIn('"write_verified"', journal.path.read_text(encoding="utf-8"))

    def test_old_unresolved_intent_and_crash_never_retry_after_48_hours(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient(self.module)
            journal = self.module.DurableJournal(Path(directory) / "journal.jsonl")
            journal.append({"status": "write_intent", "chat_id": CHAT, "attempt_id": "attempt-1", "at": "2026-01-01T00:00:00Z", "message_hash": "a" * 64})
            result = self.module.run_followup_cycle(client, journal, allow_external_read=True, allow_external_writes=True, now=datetime(2026, 1, 3, 1, tzinfo=timezone.utc))
            self.assertEqual(client.sent, 0)
            self.assertGreaterEqual(result["blocked"], 1)

    def test_unknown_or_malformed_journal_is_permanent_block(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            journal = self.module.DurableJournal(Path(directory) / "journal.jsonl")
            journal.path.write_text('{"chat_id":"' + CHAT + '","status":"not_a_status","at":"2026-01-01T00:00:00Z"}\n', encoding="utf-8")
            client = FakeClient(self.module)
            result = self.module.run_followup_cycle(client, journal, allow_external_read=True, allow_external_writes=True, now=datetime(2026, 1, 3, tzinfo=timezone.utc), clock=lambda: datetime(2026, 1, 3, tzinfo=timezone.utc))
            self.assertEqual(client.sent, 0)
            self.assertIn("journal_invalid", result["reasons"])
            journal.path.write_text('{bad json\n', encoding="utf-8")
            self.assertRaisesRegex(self.module.CycleFailure, "journal_schema", journal.assess, CHAT)

    def test_deadline_blocks_inventory_before_browser_work(self) -> None:
        class Never:
            def request(self, _): raise AssertionError("remote call")
            def counts(self, _): raise AssertionError("remote call")
        journal = self.module.DurableJournal(Path(tempfile.mkdtemp()) / "j")
        result = self.module.run_followup_cycle(Never(), journal, allow_external_read=True, deadline=__import__("time").monotonic() - 1)
        self.assertFalse(result["ok"])
        self.assertIn("inventory", result["reasons"])

    def test_unexpected_preflight_keeps_partial_counters_and_sanitized_reason(self) -> None:
        class Broken(FakeClient):
            def thread(self, chat_id, *, expected_message=None):
                raise RuntimeError("secret remote response must not escape")
        with tempfile.TemporaryDirectory() as directory:
            result = self.module.run_followup_cycle(Broken(self.module), self.module.DurableJournal(Path(directory) / "j"), allow_external_read=True, now=datetime(2026, 1, 3, tzinfo=timezone.utc))
        self.assertEqual((result["inventory"], result["reviewed"], result["blocked"]), (1, 1, 1))
        self.assertTrue(result["partial"])
        self.assertEqual(result["block_reason"], "preflight")


class SanitizedLiveFixtureTests(unittest.TestCase):
    """The frozen live-shape fixture is local, anonymous, and never written to."""

    def test_all_live_shape_rows_are_reviewed_read_only(self) -> None:
        module = load_module()
        fixture = json.loads((Path(__file__).parent / "fixtures" / "cas_sanitized.json").read_text(encoding="utf-8"))
        by_chat = {entry["row"]["casChat"]["_id"]: entry for entry in fixture}

        class ReadOnlyFixtureClient:
            def request(self, payload):
                rows = [entry["row"] for entry in fixture if entry["row"]["casChat"]["status"] == payload["status_chat"]]
                if payload.get("search_term"):
                    rows = [by_chat[payload["search_term"]]["row"]]
                return {"status": 200, "data": rows if payload["offset"] == 0 else []}
            def counts(self, _payload):
                return {"status": 200, "data": {"queued": 4, "active": 10, "postponed": 0}}
            def thread(self, chat_id, *, expected_message=None):
                assert expected_message is None, "read-only cycle must not read post-write state"
                return by_chat[chat_id]["thread"]
            def send_once(self, **_kwargs):
                raise AssertionError("read-only cycle must not send")

        with tempfile.TemporaryDirectory() as directory:
            result = module.run_followup_cycle(ReadOnlyFixtureClient(), module.DurableJournal(Path(directory) / "journal.jsonl"), allow_external_read=True, allow_external_writes=False, now=datetime(2026, 10, 14, 21, 32, tzinfo=timezone.utc))
        self.assertEqual((result["inventory"], result["reviewed"], result["due"], result["cooldown"], result["blocked"]), (14, 14, 10, 4, 0))


class StrictSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = load_module()

    def test_empty_thread_missing_last_message_and_ticket_change_fail_closed(self) -> None:
        with self.assertRaisesRegex(self.module.CycleFailure, "thread_empty"):
            self.module.inspect_thread([], chat_id=CHAT, expected_last_message_id="root")
        new_case = row(); new_case.pop("lastMessage")
        self.assertIsNone(self.module._row_identity(new_case)[6])
        source, changed = row(), row()
        changed["casTicketType"]["_id"] = "other"
        def request(payload):
            return {"status": 200, "data": [changed] if payload["status_chat"] == "active" else []}
        with self.assertRaisesRegex(self.module.CycleFailure, "revalidation_identity"):
            self.module.exact_revalidate(request, source)

    def test_schema_rejects_pii_ids_boolean_orders_and_future_times(self) -> None:
        invalid = row(chat_id="not-mongo")
        with self.assertRaisesRegex(self.module.CycleFailure, "row_identity"):
            self.module._row_identity(invalid)
        invalid = row(order=True)
        with self.assertRaisesRegex(self.module.CycleFailure, "row_identity"):
            self.module._row_identity(invalid)
        future = row(); future["casChat"]["createdAt"] = "2037-01-01T00:00:00Z"
        with self.assertRaisesRegex(self.module.CycleFailure, "row_timestamp"):
            self.module.is_due(future, [root()], journal_at=None, now=datetime(2026, 1, 3, tzinfo=timezone.utc))

    def test_browser_projection_never_returns_irrelevant_pii(self) -> None:
        program = self.module.browser_program({"kind":"search","payload":{}})
        source = f'''global.location={{origin:'https://app.dropi.co',pathname:'/dashboard/cas/tray'}};global.localStorage={{getItem:k=>k==='casToken'?'"t"':'{{"_id":"u"}}'}};global.fetch=async()=>({{status:200,json:async()=>({{data:[{{casChat:{{_id:'{CHAT}',status:'active',enterprise:'e',createdAt:'x',customer:'PII'}},referenceObject:{{id:'55',type:'ORDER',data:{{shipping_guide:'g',shipping_company:'c',address:'PII'}}}},casTicketType:{{_id:'t',name:'n',secret:'PII'}},lastMessage:{{_id:'m',createdAt:'x',text:'PII'}},formData:'PII'}}]}})}});eval({json.dumps(program)}).then(x=>console.log(JSON.stringify(x)));'''
        completed = subprocess.run(["node", "-e", source], check=True, capture_output=True, text=True)
        self.assertNotIn("PII", completed.stdout)

    def test_timeout_kills_child_process_group(self) -> None:
        import os
        import stat
        import time
        with tempfile.TemporaryDirectory() as directory:
            child = Path(directory) / "harness.py"
            pidfile = Path(directory) / "child.pid"
            child.write_text("#!/usr/bin/env python3\nimport subprocess,sys,time,os,signal\np=subprocess.Popen([sys.executable,'-c',\"import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)\"]);open(sys.argv[0]+'.pid','w').write(str(p.pid));signal.signal(signal.SIGTERM, lambda *_: sys.exit(0));time.sleep(30)\n", encoding="utf-8")
            child.chmod(child.stat().st_mode | stat.S_IXUSR)
            runner = self.module.BoundedBrowserHarnessRunner(str(child))
            with self.assertRaisesRegex(self.module.CycleFailure, "browser_timeout"):
                runner.execute_json("ignored", timeout_seconds=1)
            pid = int(Path(str(child) + ".pid").read_text())
            time.sleep(.1)
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)

    def test_http_201_ack_is_accepted_only_after_atomic_preflight(self) -> None:
        class Runner:
            def execute_json(self, code, timeout_seconds):
                self.code, self.timeout = code, timeout_seconds
                return {"status": 201, "preflight": "ok"}
        runner = Runner()
        client = self.module.BrowserCASClient(runner)
        identity = row()
        self.assertTrue(client.send_once(chat_id=CHAT, enterprise="enterprise-1", message="neutral", device_date=datetime(2026, 1, 3, tzinfo=timezone.utc), exact_row=identity, before_thread=[root(), old_message()]))
        self.assertIn("/dashboard/cas", runner.code)
        self.assertLessEqual(runner.timeout, 90)


class ScriptSafetyTests(unittest.TestCase):
    def test_shared_lock_is_existing_read_only_and_cli_override(self) -> None:
        import importlib.util
        import os
        script_path = MODULE_PATH.parents[2] / "scripts" / "dropi-cas-followups.py"
        spec = importlib.util.spec_from_file_location("cas_followup_script", script_path)
        assert spec and spec.loader
        script = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(script)
        with tempfile.TemporaryDirectory() as directory:
            lock = Path(directory) / "existing.lock"
            lock.touch(); lock.chmod(0o444)
            with script.shared_browser_lock(lock, timeout_seconds=.1):
                self.assertTrue(lock.exists())
            missing = Path(directory) / "missing.lock"
            with self.assertRaises(FileNotFoundError):
                with script.shared_browser_lock(missing, timeout_seconds=.1):
                    pass
            args = script.parse_args(["--shared-lock", str(lock)])
            self.assertEqual(args.shared_lock, lock)

    def test_default_requires_explicit_read_and_does_not_invoke_browser(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "report.json"
            completed = subprocess.run(
                ["python", "scripts/dropi-cas-followups.py", "--report", str(report)],
                cwd=MODULE_PATH.parents[2],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(completed.returncode, 2)
            payload = json.loads(completed.stdout)
            self.assertEqual(payload["blocked"], 1)
            self.assertEqual(payload["reasons"], {"authorization": 1})
            self.assertEqual((payload["partial"], payload["block_reason"]), (False, "authorization"))
            self.assertFalse(payload["writes_authorized"])
            self.assertEqual(report.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
