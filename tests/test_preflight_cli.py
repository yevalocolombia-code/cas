from __future__ import annotations

import argparse
import json
import sqlite3
from contextlib import redirect_stdout
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from dropi_cas_automation.case_validation import CaseValidationResult
from dropi_cas_automation.cli import build_parser, command_preflight
from dropi_cas_automation.models import OrderSnapshot
from dropi_cas_automation.storage import initialize_database


class PreflightCliTests(unittest.TestCase):
    def _args(self, config: str, **overrides: object) -> argparse.Namespace:
        values = {
            "config": config,
            "guide": "GUIDE-001",
            "order_id": "order-1",
            "allow_external_read": True,
            "case_service_type_id": "service-123",
            "browser_command": "browser-harness",
        }
        values.update(overrides)
        return argparse.Namespace(**values)

    def test_parser_requires_service_type_for_existing_case_detection(self) -> None:
        parser = build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(
                [
                    "preflight",
                    "--config",
                    "config.toml",
                    "--guide",
                    "GUIDE-001",
                    "--allow-external-read",
                ]
            )

    def test_preflight_requires_explicit_external_read_permission(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.toml"
            config.write_text('[workspace]\nroot = "./runtime"\n', encoding="utf-8")
            with self.assertRaises(PermissionError):
                command_preflight(self._args(str(config), allow_external_read=False))

    @patch("dropi_cas_automation.cli.EvidenceCapture")
    @patch("dropi_cas_automation.cli.DropiCaseValidator")
    @patch("dropi_cas_automation.cli.DropiHistoryReader")
    @patch("dropi_cas_automation.cli.BrowserHarnessRunner")
    @patch("dropi_cas_automation.cli.load_candidates")
    @patch("dropi_cas_automation.cli.refresh_guide_history")
    def test_eligible_preflight_refreshes_validates_and_captures_evidence_without_writing(
        self,
        refresh: Mock,
        load_candidates: Mock,
        runner_cls: Mock,
        history_cls: Mock,
        validator_cls: Mock,
        evidence_cls: Mock,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.toml"
            config.write_text('[workspace]\nroot = "./runtime"\n', encoding="utf-8")
            order = OrderSnapshot(
                order_id="order-1",
                guide="GUIDE-001",
                carrier="ENVIA",
                current_status="NOVEDAD",
                last_movement_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
            )
            load_candidates.return_value = [order]
            validator_cls.return_value.validate.return_value = CaseValidationResult("eligible")
            evidence = Path(tmp) / "evidence.png"
            evidence.write_bytes(b"png")
            evidence_cls.return_value.capture.return_value = evidence
            output = StringIO()

            with redirect_stdout(output):
                result = command_preflight(self._args(str(config)))

            self.assertEqual(result, 0)
            refresh.assert_called_once()
            validator_cls.return_value.validate.assert_called_once_with(
                "order-1", "ENVIA", allow_external_read=True
            )
            evidence_cls.return_value.capture.assert_called_once_with(
                "GUIDE-001", allow_external_read=True
            )
            self.assertIn('"mode": "read_only_preflight"', output.getvalue())
            self.assertIn('"status": "eligible"', output.getvalue())
            self.assertIn(str(evidence), output.getvalue())

    @patch("dropi_cas_automation.cli.EvidenceCapture")
    @patch("dropi_cas_automation.cli.DropiCaseValidator")
    @patch("dropi_cas_automation.cli.DropiHistoryReader")
    @patch("dropi_cas_automation.cli.BrowserHarnessRunner")
    @patch("dropi_cas_automation.cli.load_candidates")
    @patch("dropi_cas_automation.cli.refresh_guide_history")
    def test_existing_case_stops_before_evidence_capture(
        self,
        refresh: Mock,
        load_candidates: Mock,
        runner_cls: Mock,
        history_cls: Mock,
        validator_cls: Mock,
        evidence_cls: Mock,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.toml"
            config.write_text('[workspace]\nroot = "./runtime"\n', encoding="utf-8")
            load_candidates.return_value = [
                OrderSnapshot(
                    order_id="order-1",
                    guide="GUIDE-001",
                    carrier="ENVIA",
                    current_status="NOVEDAD",
                    last_movement_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
                )
            ]
            validator_cls.return_value.validate.return_value = CaseValidationResult(
                "existing_case", chat_id="chat-1"
            )
            output = StringIO()

            with redirect_stdout(output):
                result = command_preflight(self._args(str(config)))

            self.assertEqual(result, 0)
            evidence_cls.return_value.capture.assert_not_called()
            self.assertIn('"status": "existing_case"', output.getvalue())
            self.assertIn('"chat_id": "chat-1"', output.getvalue())

    @patch("dropi_cas_automation.cli.EvidenceCapture")
    @patch("dropi_cas_automation.cli.DropiCaseValidator")
    @patch("dropi_cas_automation.cli.DropiHistoryReader")
    @patch("dropi_cas_automation.cli.BrowserHarnessRunner")
    @patch("dropi_cas_automation.cli.load_candidates", return_value=[])
    @patch("dropi_cas_automation.cli.refresh_guide_history")
    def test_post_refresh_exclusion_returns_confirmed_identity_and_decision(
        self,
        refresh: Mock,
        load_candidates: Mock,
        runner_cls: Mock,
        history_cls: Mock,
        validator_cls: Mock,
        evidence_cls: Mock,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.toml"
            config.write_text('[workspace]\nroot = "./runtime"\n', encoding="utf-8")
            database = Path(tmp) / "runtime" / "data" / "automation.sqlite3"
            initialize_database(database)
            with sqlite3.connect(database) as connection:
                connection.execute(
                    """INSERT INTO orders(order_id, guide, status, carrier, last_movement_at, raw_json)
                       VALUES(?, ?, ?, ?, ?, ?)""",
                    ("order-1", "GUIDE-001", "DEVOLUCION", "ENVIA", "2026-09-01T00:00:00+00:00", "{}"),
                )
            output = StringIO()

            with redirect_stdout(output):
                result = command_preflight(self._args(str(config)))

            self.assertEqual(result, 0)
            self.assertEqual(
                json.loads(output.getvalue()),
                {
                    "ok": True,
                    "mode": "read_only_preflight",
                    "order_id": "order-1",
                    "guide": "GUIDE-001",
                    "status": "not_eligible_after_history_refresh",
                    "decision_status": "excluded_status",
                    "reason": "Status is excluded: DEVOLUCION.",
                },
            )
            validator_cls.return_value.validate.assert_not_called()
            evidence_cls.return_value.capture.assert_not_called()


if __name__ == "__main__":
    unittest.main()
