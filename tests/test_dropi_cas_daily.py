from __future__ import annotations

import importlib.util
import io
import json
import sqlite3
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path


SCRIPT_PATH = Path(__file__).parents[1] / "scripts" / "dropi-cas-daily.py"


def load_runner_module():
    spec = importlib.util.spec_from_file_location("dropi_cas_daily", SCRIPT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeCommands:
    def __init__(self, responses: dict[str, list[object]]):
        self.responses = {name: list(values) for name, values in responses.items()}
        self.calls: list[tuple[list[str], float]] = []

    def __call__(self, command: list[str], timeout_seconds: float) -> dict[str, object]:
        self.calls.append((command, timeout_seconds))
        name = command[1]
        value = self.responses[name].pop(0)
        if isinstance(value, Exception):
            raise value
        return value  # type: ignore[return-value]


class DailyCasRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = load_runner_module()

    def _config(self, root: Path, *, timezone: str = "America/Bogota", hours: int = 48) -> Path:
        config = root / "config.toml"
        config.write_text(
            "[workspace]\nroot = \"runtime\"\n\n[rules]\n"
            f"minimum_hours_without_movement = {hours}\n"
            f"movement_timezone = \"{timezone}\"\n",
            encoding="utf-8",
        )
        return config

    def _arguments(self, config: Path, journal: Path, report: Path) -> list[str]:
        return [
            "--config", str(config),
            "--case-service-type-id", "service-1",
            "--allow-external-writes",
            "--journal", str(journal),
            "--output-report", str(report),
        ]

    @staticmethod
    def _auth() -> dict[str, object]:
        return {"ok": True, "logged_in": True, "orders_visible": True}

    @staticmethod
    def _candidate(order_id: str = "1", guide: str = "G-1") -> dict[str, str]:
        return {"order_id": order_id, "guide": guide}

    def _payload(self, output: io.StringIO) -> dict[str, object]:
        return json.loads(output.getvalue())

    def test_requires_explicit_write_authorization_and_writes_sanitized_report(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = self._config(root)
            report = root / "private" / "report.json"
            output = io.StringIO()
            with redirect_stdout(output):
                result = self.module.main([
                    "--config", str(config),
                    "--case-service-type-id", "service-1",
                    "--journal", str(root / "journal.jsonl"),
                    "--output-report", str(report),
                ])
            self.assertEqual(result, 2)
            payload = self._payload(output)
            self.assertEqual(payload["bloqueado"], 1)
            self.assertFalse(payload["escrituras_autorizadas"])
            self.assertEqual(json.loads(report.read_text(encoding="utf-8")), payload)
            self.assertEqual(report.stat().st_mode & 0o777, 0o600)

    def test_created_case_requires_preflight_and_readback_identity_match(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = self._config(root)
            journal = root / "private" / "journal.jsonl"
            report = root / "private" / "report.json"
            commands = FakeCommands({
                "diagnose-dropi": [self._auth()],
                "sync": [{"ok": True}],
                "candidates": [{"ok": True, "candidates": [self._candidate()]}],
                "preflight": [
                    {"ok": True, "status": "eligible", **self._candidate()},
                    {"ok": True, "status": "existing_case", "chat_id": "chat-1", **self._candidate()},
                ],
                "run": [{"ok": True, "results": [{"status": "created", "chat_id": "chat-1", "guide": "G-1"}]}],
            })
            output = io.StringIO()
            with redirect_stdout(output):
                result = self.module.main(self._arguments(config, journal, report), run_command=commands)

            self.assertEqual(result, 0)
            payload = self._payload(output)
            self.assertEqual((payload["creados"], payload["verificados"], payload["errores"]), (1, 1, 0))
            self.assertNotIn("G-1", output.getvalue())
            self.assertNotIn("chat-1", output.getvalue())
            run_call = next(call for call, _timeout in commands.calls if call[1] == "run")
            self.assertIn("--execute", run_call)
            self.assertNotIn("--limit", run_call)
            preflight_call = next(call for call, _timeout in commands.calls if call[1] == "preflight")
            self.assertEqual(preflight_call[preflight_call.index("--order-id") + 1], "1")
            self.assertGreaterEqual(next(timeout for call, timeout in commands.calls if call[1] == "run"), 240)
            events = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()]
            self.assertEqual([event["status"] for event in events], ["write_intent", "write_verified"])
            self.assertEqual(json.loads(report.read_text(encoding="utf-8")), payload)

    def test_eligible_preflight_identity_mismatch_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = self._config(root)
            journal, report = root / "journal.jsonl", root / "report.json"
            commands = FakeCommands({
                "diagnose-dropi": [self._auth()], "sync": [{"ok": True}],
                "candidates": [{"ok": True, "candidates": [self._candidate()]}],
                "preflight": [{"ok": True, "status": "eligible", **self._candidate(order_id="other")}],
            })
            with redirect_stdout(io.StringIO()) as output:
                result = self.module.main(self._arguments(config, journal, report), run_command=commands)
            self.assertEqual(result, 2)
            payload = self._payload(output)
            self.assertEqual((payload["bloqueado"], payload["existentes"], payload["excluidos"]), (1, 0, 0))
            event = json.loads(journal.read_text(encoding="utf-8"))
            self.assertEqual((event["status"], event["reason"]), ("blocked_preflight_identity_mismatch", "preflight_identity_mismatch"))
            self.assertFalse(any(call[1] == "run" for call, _timeout in commands.calls))

    def test_preflight_exception_records_a_sanitized_reason(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = self._config(root)
            journal, report = root / "journal.jsonl", root / "report.json"
            commands = FakeCommands({
                "diagnose-dropi": [self._auth()], "sync": [{"ok": True}],
                "candidates": [{"ok": True, "candidates": [self._candidate()]}],
                "preflight": [self.module.CommandFailure("untrusted browser detail")],
            })
            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(self.module.main(self._arguments(config, journal, report), run_command=commands), 2)
            event = json.loads(journal.read_text(encoding="utf-8"))
            self.assertEqual((event["status"], event["reason"]), ("blocked_preflight_uncertain", "preflight_command_failed"))
            self.assertNotIn("untrusted browser detail", journal.read_text(encoding="utf-8"))
            self.assertFalse(any(call[1] == "run" for call, _timeout in commands.calls))

    def test_known_not_eligible_is_excluded_not_existing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = self._config(root)
            journal, report = root / "journal.jsonl", root / "report.json"
            commands = FakeCommands({
                "diagnose-dropi": [self._auth()], "sync": [{"ok": True}],
                "candidates": [{"ok": True, "candidates": [self._candidate()]}],
                "preflight": [{
                    "ok": True,
                    "status": "not_eligible_after_history_refresh",
                    "decision_status": "excluded_status",
                    "reason": "Status is excluded: DEVOLUCION.",
                    **self._candidate(),
                }],
            })
            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(self.module.main(self._arguments(config, journal, report), run_command=commands), 0)
            payload = self._payload(output)
            self.assertEqual((payload["bloqueado"], payload["existentes"], payload["excluidos"]), (0, 0, 1))
            event = json.loads(journal.read_text(encoding="utf-8"))
            self.assertEqual(
                (event["status"], event["decision_status"], event["reason"]),
                ("excluded_not_eligible", "excluded_status", "excluded_by_status"),
            )
            self.assertNotIn("DEVOLUCION", journal.read_text(encoding="utf-8"))

    def test_excluded_preflight_missing_order_identity_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = self._config(root)
            journal, report = root / "journal.jsonl", root / "report.json"
            commands = FakeCommands({
                "diagnose-dropi": [self._auth()], "sync": [{"ok": True}],
                "candidates": [{"ok": True, "candidates": [self._candidate()]}],
                "preflight": [{
                    "ok": True,
                    "status": "not_eligible_after_history_refresh",
                    "decision_status": "excluded_status",
                    "reason": "Status is excluded: DEVOLUCION.",
                    "guide": "G-1",
                }],
            })
            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(self.module.main(self._arguments(config, journal, report), run_command=commands), 2)
            event = json.loads(journal.read_text(encoding="utf-8"))
            self.assertEqual((event["status"], event["reason"]), ("blocked_preflight_identity_mismatch", "preflight_identity_mismatch"))
            self.assertFalse(any(call[1] == "run" for call, _timeout in commands.calls))

    def test_missing_preflight_status_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = self._config(root)
            journal, report = root / "journal.jsonl", root / "report.json"
            commands = FakeCommands({
                "diagnose-dropi": [self._auth()], "sync": [{"ok": True}],
                "candidates": [{"ok": True, "candidates": [self._candidate()]}],
                "preflight": [{"ok": True, **self._candidate()}],
            })
            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(self.module.main(self._arguments(config, journal, report), run_command=commands), 2)
            event = json.loads(journal.read_text(encoding="utf-8"))
            self.assertEqual((event["status"], event["reason"]), ("blocked_preflight_uncertain", "preflight_status_missing"))
            self.assertFalse(any(call[1] == "run" for call, _timeout in commands.calls))

    def test_existing_cases_make_followup_total_unknown_and_pending_review(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = self._config(root)
            database = root / "runtime" / "data" / "automation.sqlite3"
            database.parent.mkdir(parents=True)
            with sqlite3.connect(database) as connection:
                connection.execute("CREATE TABLE followups(status TEXT NOT NULL)")
            journal, report = root / "journal.jsonl", root / "report.json"
            commands = FakeCommands({
                "diagnose-dropi": [self._auth()], "sync": [{"ok": True}],
                "candidates": [{"ok": True, "candidates": [self._candidate()]}],
                "preflight": [{"ok": True, "status": "existing_case", "chat_id": "chat-1", **self._candidate()}],
            })
            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(self.module.main(self._arguments(config, journal, report), run_command=commands), 0)
            payload = self._payload(output)
            self.assertIsNone(payload["seguimientos_pendientes"])
            self.assertEqual(payload["seguimientos_pendientes_revision"], 1)
            self.assertEqual(payload["inventario_seguimientos"], "incompleto")

    def test_identity_mismatch_before_or_after_write_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = self._config(root)
            journal, report = root / "journal.jsonl", root / "report.json"
            commands = FakeCommands({
                "diagnose-dropi": [self._auth()], "sync": [{"ok": True}],
                "candidates": [{"ok": True, "candidates": [self._candidate()]}],
                "preflight": [
                    {"ok": True, "status": "eligible", **self._candidate()},
                    {"ok": True, "status": "existing_case", "chat_id": "chat-1", **self._candidate(order_id="other")},
                ],
                "run": [{"ok": True, "results": [{"status": "created", "chat_id": "chat-1", "guide": "G-1"}]}],
            })
            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(self.module.main(self._arguments(config, journal, report), run_command=commands), 2)
            self.assertEqual(self._payload(output)["verificados"], 0)
            events = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(events[-1]["status"], "blocked_remote_readback_mismatch")

    def test_write_failure_preserves_prior_verified_progress_and_blocks_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = self._config(root)
            journal, report = root / "journal.jsonl", root / "report.json"
            commands = FakeCommands({
                "diagnose-dropi": [self._auth()], "sync": [{"ok": True}],
                "candidates": [{"ok": True, "candidates": [self._candidate("1", "G-1"), self._candidate("2", "G-2")] }],
                "preflight": [
                    {"ok": True, "status": "eligible", **self._candidate("1", "G-1")},
                    {"ok": True, "status": "existing_case", "chat_id": "chat-1", **self._candidate("1", "G-1")},
                    {"ok": True, "status": "eligible", **self._candidate("2", "G-2")},
                ],
                "run": [
                    {"ok": True, "results": [{"status": "created", "chat_id": "chat-1", "guide": "G-1"}]},
                    self.module.CommandFailure("transport interrupted"),
                ],
            })
            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(self.module.main(self._arguments(config, journal, report), run_command=commands), 2)
            payload = self._payload(output)
            self.assertEqual((payload["creados"], payload["verificados"], payload["bloqueado"]), (1, 1, 1))
            events = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(events[-1]["status"], "blocked_write_uncertain")

    def test_deadline_starts_before_diagnostic_and_reserves_240_seconds_before_write(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = self._config(root)
            journal, report = root / "journal.jsonl", root / "report.json"
            commands = FakeCommands({"diagnose-dropi": [self._auth()]})
            args = self.module.build_parser().parse_args(self._arguments(config, journal, report) + ["--max-runtime-seconds", "10"])
            ticks = iter([0.0, 2.0, 2.0])
            status, payload = self.module.run_daily(args, commands, monotonic=lambda: next(ticks))
            self.assertEqual(status, 2)
            self.assertEqual(commands.calls[0][1], 8.0)
            self.assertEqual(payload["bloqueado"], 1)

            commands = FakeCommands({
                "diagnose-dropi": [self._auth()], "sync": [{"ok": True}],
                "candidates": [{"ok": True, "candidates": [self._candidate()]}],
                "preflight": [{"ok": True, "status": "eligible", **self._candidate()}],
            })
            args = self.module.build_parser().parse_args(self._arguments(config, journal, report) + ["--max-runtime-seconds", "239"])
            status, payload = self.module.run_daily(args, commands, monotonic=lambda: 0.0)
            self.assertEqual(status, 0)
            self.assertEqual(payload["pendientes"], 1)
            self.assertFalse(any(call[1] == "run" for call, _timeout in commands.calls))

    def test_prior_day_deferred_checkpoint_is_prioritized_without_sorting_fresh_work(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = self._config(root)
            journal, report = root / "journal.jsonl", root / "report.json"
            yesterday = "2026-10-13T23:00:00+00:00"
            journal.write_text(json.dumps({"at": yesterday, "guide": "G-old", "status": "deferred_runtime_cap"}) + "\n", encoding="utf-8")
            commands = FakeCommands({
                "diagnose-dropi": [self._auth()], "sync": [{"ok": True}],
                "candidates": [{"ok": True, "candidates": [self._candidate("3", "G-new-first"), self._candidate("1", "G-old"), self._candidate("2", "G-new-second")]}],
                "preflight": [
                    {"ok": True, "status": "not_eligible_after_history_refresh", "decision_status": "under_threshold", **self._candidate("1", "G-old")},
                    {"ok": True, "status": "not_eligible_after_history_refresh", "decision_status": "under_threshold", **self._candidate("3", "G-new-first")},
                    {"ok": True, "status": "not_eligible_after_history_refresh", "decision_status": "under_threshold", **self._candidate("2", "G-new-second")},
                ],
            })
            args = self.module.build_parser().parse_args(self._arguments(config, journal, report))
            status, _payload = self.module.run_daily(
                args, commands, monotonic=lambda: 0.0,
                utcnow=lambda: datetime(2026, 10, 14, tzinfo=timezone.utc),
            )
            self.assertEqual(status, 0)
            self.assertEqual([call[0][call[0].index("--guide") + 1] for call in commands.calls if call[0][1] == "preflight"], ["G-old", "G-new-first", "G-new-second"])

    def test_subprocess_timeout_kills_process_group(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            marker = Path(temp_dir) / "child-ran"
            command = [
                sys.executable, "-c",
                "import pathlib,subprocess,sys,time; "
                "subprocess.Popen([sys.executable, '-c', \"import pathlib,time; time.sleep(.4); pathlib.Path(sys.argv[1]).write_text('ran')\", sys.argv[1]]); "
                "time.sleep(5)", str(marker),
            ]
            with self.assertRaises(self.module.CommandFailure):
                self.module.subprocess_command(command, 0.05)
            time.sleep(0.7)
            self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
