import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dropi_cas_automation.cli import main


class CandidatesCliTests(unittest.TestCase):
    def test_candidates_command_returns_local_eligible_orders(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = root / "config.toml"
            config.write_text('[workspace]\nroot = "runtime"\n[rules]\nminimum_hours_without_movement = 24\n', encoding="utf-8")
            database = root / "runtime" / "data" / "automation.sqlite3"
            database.parent.mkdir(parents=True)
            from dropi_cas_automation.storage import initialize_database
            initialize_database(database)
            old = (datetime.now(timezone.utc) - timedelta(hours=30)).replace(microsecond=0).isoformat()
            with sqlite3.connect(database) as connection:
                connection.execute("INSERT INTO orders(order_id, guide, status, carrier, last_movement_at, raw_json) VALUES(?, ?, ?, ?, ?, ?)", ("123", "034000000001", "EN TRANSPORTE", "carrier-a", old, "{}"))
            stream = io.StringIO()
            with redirect_stdout(stream):
                self.assertEqual(main(["candidates", "--config", str(config)]), 0)
            self.assertEqual(json.loads(stream.getvalue())["count"], 1)
