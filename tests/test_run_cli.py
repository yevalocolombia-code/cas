import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from dropi_cas_automation.cli import main
from dropi_cas_automation.models import OrderSnapshot
from dropi_cas_automation.storage import initialize_database


class RunCliTests(unittest.TestCase):
    def test_run_dry_run_lists_candidates_without_external_access(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = root / "config.toml"
            config.write_text('[workspace]\nroot = "runtime"\n', encoding="utf-8")
            database = root / "runtime" / "data" / "automation.sqlite3"
            database.parent.mkdir(parents=True)
            initialize_database(database)
            old = (datetime.now(timezone.utc) - timedelta(hours=60)).replace(microsecond=0).isoformat()
            with sqlite3.connect(database) as connection:
                connection.execute("INSERT INTO orders(order_id, guide, status, carrier, last_movement_at, raw_json) VALUES(?, ?, ?, ?, ?, ?)", ("123", "034000000001", "EN TRANSPORTE", "carrier-a", old, "{}"))
            stream = io.StringIO()
            with redirect_stdout(stream):
                self.assertEqual(main(["run", "--config", str(config), "--dry-run"]), 0)
            payload = json.loads(stream.getvalue())
            self.assertEqual(payload["mode"], "dry_run")
            self.assertEqual(payload["candidate_count"], 1)

    def test_execute_requires_service_type_for_duplicate_detection(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = root / "config.toml"
            config.write_text('[workspace]\nroot = "runtime"\n', encoding="utf-8")
            stream = io.StringIO()
            with redirect_stdout(stream):
                result = main(
                    [
                        "run",
                        "--config",
                        str(config),
                        "--execute",
                        "--guide",
                        "034000000001",
                        "--allow-external-read",
                        "--allow-external-writes",
                    ]
                )
            self.assertEqual(result, 2)
            payload = json.loads(stream.getvalue())
            self.assertFalse(payload["ok"])
            self.assertIn("case-service-type-id", payload["error"])

    def test_execute_requires_one_explicit_guide_and_rejects_invalid_limit(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = root / "config.toml"
            config.write_text('[workspace]\nroot = "runtime"\n', encoding="utf-8")
            for extra_args in (
                [],
                ["--guide", "034000000001", "--limit", "-1"],
                ["--guide", "034000000001", "--limit", "2"],
            ):
                with self.subTest(extra_args=extra_args):
                    stream = io.StringIO()
                    with redirect_stdout(stream):
                        result = main(
                            [
                                "run",
                                "--config",
                                str(config),
                                "--execute",
                                "--allow-external-read",
                                "--allow-external-writes",
                                "--case-service-type-id",
                                "service-type",
                                *extra_args,
                            ]
                        )
                    self.assertEqual(result, 2)
                    payload = json.loads(stream.getvalue())
                    self.assertFalse(payload["ok"])
                    expected_word = "guide" if not extra_args or extra_args[-1] == "2" else "limit"
                    self.assertIn(expected_word, payload["error"].lower())

    def test_execute_rejects_guide_matching_multiple_orders(self):
        item1 = OrderSnapshot("101", "G-1", "ENVIA", "EN REPARTO", datetime.now(timezone.utc) - timedelta(days=3))
        item2 = OrderSnapshot("102", "G-1", "ENVIA", "EN REPARTO", datetime.now(timezone.utc) - timedelta(days=3))
        with tempfile.TemporaryDirectory() as temp_dir:
            config = Path(temp_dir) / "config.toml"
            config.write_text('[workspace]\nroot = "runtime"\n', encoding="utf-8")
            stream = io.StringIO()
            with patch("dropi_cas_automation.cli.load_candidates", return_value=[item1, item2]), redirect_stdout(stream):
                result = main(["run", "--config", str(config), "--execute", "--guide", "G-1", "--allow-external-read", "--allow-external-writes", "--case-service-type-id", "service"])
            self.assertEqual(result, 2)
            self.assertIn("exactly one candidate", json.loads(stream.getvalue())["error"])

    def test_execute_rechecks_eligibility_after_history_refresh(self):
        item = OrderSnapshot("101", "G-1", "ENVIA", "EN REPARTO", datetime.now(timezone.utc) - timedelta(days=3))
        with tempfile.TemporaryDirectory() as temp_dir:
            config = Path(temp_dir) / "config.toml"
            config.write_text('[workspace]\nroot = "runtime"\n', encoding="utf-8")
            stream = io.StringIO()
            with (
                patch("dropi_cas_automation.cli.load_candidates", side_effect=[[item], []]),
                patch("dropi_cas_automation.cli.refresh_guide_history"),
                patch("dropi_cas_automation.cli.BrowserHarnessRunner"),
                patch("dropi_cas_automation.cli.DropiCaseValidator") as validator,
                patch("dropi_cas_automation.cli.DropiCaseCreator") as creator,
                redirect_stdout(stream),
            ):
                result = main(["run", "--config", str(config), "--execute", "--guide", "G-1", "--allow-external-read", "--allow-external-writes", "--case-service-type-id", "service"])
            self.assertEqual(result, 0)
            self.assertEqual(json.loads(stream.getvalue())["results"], [{"guide": "G-1", "status": "skipped_not_eligible_after_refresh"}])
            validator.return_value.validate.assert_not_called()
            creator.return_value.create.assert_not_called()

    def test_execute_blocks_duplicate_guide_appearing_after_refresh(self):
        original = OrderSnapshot("101", "G-1", "ENVIA", "EN REPARTO", datetime.now(timezone.utc) - timedelta(days=3))
        duplicate = OrderSnapshot("102", "G-1", "ENVIA", "EN REPARTO", datetime.now(timezone.utc) - timedelta(days=3))
        with tempfile.TemporaryDirectory() as temp_dir:
            config = Path(temp_dir) / "config.toml"
            config.write_text('[workspace]\nroot = "runtime"\n', encoding="utf-8")
            stream = io.StringIO()
            with (
                patch("dropi_cas_automation.cli.load_candidates", side_effect=[[original], [original, duplicate]]),
                patch("dropi_cas_automation.cli.refresh_guide_history"),
                patch("dropi_cas_automation.cli.BrowserHarnessRunner"),
                patch("dropi_cas_automation.cli.DropiCaseValidator") as validator,
                patch("dropi_cas_automation.cli.DropiCaseCreator") as creator,
                redirect_stdout(stream),
            ):
                result = main(["run", "--config", str(config), "--execute", "--guide", "G-1", "--allow-external-read", "--allow-external-writes", "--case-service-type-id", "service"])
            self.assertEqual(result, 0)
            self.assertEqual(json.loads(stream.getvalue())["results"], [{"guide": "G-1", "status": "blocked_ambiguous_after_refresh"}])
            validator.return_value.validate.assert_not_called()
            creator.return_value.create.assert_not_called()
