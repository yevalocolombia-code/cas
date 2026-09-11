import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path

from dropi_cas_automation.cli import main
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
            old = (datetime.now() - timedelta(hours=30)).replace(microsecond=0).isoformat(sep=" ")
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
                        "--allow-external-read",
                        "--allow-external-writes",
                    ]
                )
            self.assertEqual(result, 2)
            payload = json.loads(stream.getvalue())
            self.assertFalse(payload["ok"])
            self.assertIn("case-service-type-id", payload["error"])
