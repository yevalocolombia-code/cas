import io
import json
import sqlite3
import tempfile
import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from dropi_cas_automation.case_validation import CaseValidationResult
from dropi_cas_automation.cli import command_followups, main
from dropi_cas_automation.followup_store import DueFollowup
from dropi_cas_automation.models import OrderSnapshot
from dropi_cas_automation.storage import initialize_database


class FollowupsCliTests(unittest.TestCase):
    def test_dry_run_lists_due_followup_without_browser_access(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = root / "config.toml"
            config.write_text('[workspace]\nroot = "./runtime"\n[rules]\nminimum_hours_without_movement = 24\n', encoding="utf-8")
            database = root / "runtime" / "data" / "automation.sqlite3"
            initialize_database(database)
            with sqlite3.connect(database) as con:
                con.execute("INSERT INTO orders(order_id,guide,status,carrier,last_movement_at,raw_json) VALUES('order-1','034','EN REPARTO','carrier','2026-01-01 00:00:00','{}')")
                con.execute("INSERT INTO cases(order_id,guide,chat_id,evidence_path,message) VALUES('order-1','034','chat-1','e.png','initial')")
                con.execute("INSERT INTO followups(case_id,chat_id,due_at) VALUES(1,'chat-1',?)", ((datetime.now()-timedelta(hours=1)).replace(microsecond=0).isoformat(sep=" "),))
            output = io.StringIO()
            with redirect_stdout(output):
                exit_code = main(["followups", "--config", str(config), "--dry-run"])
            payload = json.loads(output.getvalue())
            self.assertEqual(exit_code, 0)
            self.assertEqual(payload["due_count"], 1)
            self.assertEqual(payload["followups"][0]["chat_id"], "chat-1")

    def test_execute_blocks_when_validated_chat_differs_from_local_chat(self):
        due = DueFollowup(1, 2, "order-1", "034", "carrier", "chat-local", datetime.now())
        candidate = OrderSnapshot("order-1", "034", "carrier", "EN REPARTO", datetime.now(timezone.utc) - timedelta(hours=72))
        args = Namespace(
            config="config.toml",
            limit=1,
            dry_run=False,
            allow_external_read=True,
            allow_external_writes=True,
            case_service_type_id="service-123",
            browser_command="browser-harness",
        )
        config = SimpleNamespace(database_path=Path("/tmp/test.sqlite3"), minimum_hours_without_movement=48, movement_timezone="America/Bogota")
        with (
            patch("dropi_cas_automation.cli._setup", return_value=config),
            patch("dropi_cas_automation.cli.load_due_followups", return_value=[due]),
            patch("dropi_cas_automation.cli.refresh_guide_history"),
            patch("dropi_cas_automation.cli.load_candidates", return_value=[candidate]),
            patch("dropi_cas_automation.cli.DropiCaseValidator") as validator,
            patch("dropi_cas_automation.cli.DropiFollowupSender") as sender,
            patch("dropi_cas_automation.cli.mark_followup_skipped") as skipped,
            patch("dropi_cas_automation.cli.mark_followup_sent") as sent,
        ):
            validator.return_value.validate.return_value = CaseValidationResult("existing_case", chat_id="chat-remote")
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(command_followups(args), 0)

            self.assertEqual(json.loads(output.getvalue())["results"], [{"guide": "034", "status": "skipped_validated_chat_mismatch"}])
            sender.return_value.send.assert_not_called()
            sent.assert_not_called()
            skipped.assert_called_once_with(config.database_path, 1, "validated_chat_mismatch")
