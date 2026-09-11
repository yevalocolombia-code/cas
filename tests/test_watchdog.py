from __future__ import annotations

import importlib.util
from io import StringIO
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "dropi-cas-watchdog.py"


def load_watchdog():
    spec = importlib.util.spec_from_file_location("dropi_cas_watchdog", SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError("watchdog module cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DropiCasWatchdogTests(unittest.TestCase):
    def _fake_cli(self, root: Path) -> tuple[Path, Path]:
        calls = root / "calls.jsonl"
        cli = root / "fake-dropi-cas"
        cli.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "with open(os.environ['WATCHDOG_CALLS'], 'a', encoding='utf-8') as fh:\n"
            "    fh.write(json.dumps(sys.argv[1:]) + '\\n')\n"
            "command = sys.argv[1]\n"
            "payloads = {\n"
            " 'diagnose-dropi': {'ok': True, 'logged_in': True, 'orders_visible': True},\n"
            " 'sync': {'ok': True, 'source': 'mcp', 'imported': 1443, 'meta': {'hasMore': False, 'truncated': False}},\n"
            " 'candidates': {'ok': True, 'count': 3, 'candidates': [{'guide': 'private'}]},\n"
            " 'followups': {'ok': True, 'due_count': 1, 'followups': [{'guide': 'private'}]},\n"
            " 'report': {'ok': True, 'orders': 1443, 'cases': 2, 'pending_followups': 1},\n"
            "}\n"
            "print(json.dumps(payloads[command]))\n",
            encoding="utf-8",
        )
        cli.chmod(0o700)
        return cli, calls

    def test_collect_summary_uses_only_read_only_commands_and_returns_no_identifiers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cli, calls = self._fake_cli(root)
            with patch.dict(os.environ, {"WATCHDOG_CALLS": str(calls)}):
                module = load_watchdog()
                summary = module.collect_summary(str(cli), "config.toml", "browser-harness")

            invoked = [json.loads(line) for line in calls.read_text(encoding="utf-8").splitlines()]
            flattened = " ".join(arg for call in invoked for arg in call)
            self.assertNotIn("--execute", flattened)
            self.assertNotIn("--allow-external-writes", flattened)
            self.assertEqual(
                [call[0] for call in invoked],
                ["diagnose-dropi", "sync", "candidates", "followups", "report"],
            )
            self.assertEqual(summary["candidate_count"], 3)
            self.assertEqual(summary["followups_due"], 1)
            self.assertEqual(summary["orders"], 1443)
            self.assertNotIn("guide", json.dumps(summary))
            self.assertNotIn("order_id", json.dumps(summary))

    def test_main_emits_on_state_change_then_stays_silent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cli, calls = self._fake_cli(root)
            state = root / "state.json"
            lock = root / "watchdog.lock"
            env = {
                "WATCHDOG_CALLS": str(calls),
                "DROPI_CAS_CLI": str(cli),
                "DROPI_CAS_CONFIG": "config.toml",
                "DROPI_CAS_BROWSER_COMMAND": "browser-harness",
                "DROPI_CAS_WATCHDOG_STATE": str(state),
                "DROPI_CAS_WATCHDOG_LOCK": str(lock),
            }
            module = load_watchdog()
            first = StringIO()
            with patch.dict(os.environ, env, clear=False), patch("sys.stdout", first):
                self.assertEqual(module.main(), 0)
            self.assertIn("CAS Dropi operativo", first.getvalue())
            self.assertNotIn("private", first.getvalue())

            second = StringIO()
            with patch.dict(os.environ, env, clear=False), patch("sys.stdout", second):
                self.assertEqual(module.main(), 0)
            self.assertEqual(second.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
