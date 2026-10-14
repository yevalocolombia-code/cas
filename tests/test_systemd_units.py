import fcntl
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class SystemdUnitTests(unittest.TestCase):
    def test_browser_runs_as_dedicated_user_with_chrome_sandbox(self):
        text = (ROOT / "deploy/systemd/dropi-browser.service").read_text(encoding="utf-8")
        self.assertIn("User=dropicas", text)
        self.assertIn("Group=dropicas", text)
        self.assertNotIn("--no-sandbox", text)
        self.assertIn("--remote-debugging-address=127.0.0.1", text)
        self.assertIn("NoNewPrivileges=yes", text)
        self.assertIn("ProtectSystem=full", text)

    def test_xvfb_runs_as_same_dedicated_user(self):
        text = (ROOT / "deploy/systemd/dropi-xvfb.service").read_text(encoding="utf-8")
        self.assertIn("User=dropicas", text)
        self.assertIn("Group=dropicas", text)
        self.assertIn("-nolisten tcp", text)

    def test_daily_service_is_bounded_explicit_and_uses_private_environment_file(self):
        text = (ROOT / "deploy/systemd/dropi-cas-daily.service").read_text(encoding="utf-8")
        self.assertIn("Type=oneshot", text)
        self.assertIn("EnvironmentFile=/opt/dropi-cas/runtime/daily-cas.env", text)
        self.assertIn("/opt/dropi-cas/.venv/bin/python /opt/dropi-cas/scripts/dropi-cas-daily.py", text)
        self.assertIn("--case-service-type-id ${CAS_SERVICE_TYPE_ID}", text)
        self.assertIn("--allow-external-writes", text)
        self.assertIn("--minimum-hours 48", text)
        self.assertIn("--movement-timezone America/Bogota", text)
        self.assertIn("--max-runtime-seconds 7200", text)
        self.assertIn("--output-report /opt/dropi-cas/runtime/daily-cas-report.json", text)
        self.assertIn("TimeoutStartSec=9600", text)
        self.assertIn("KillMode=control-group", text)
        self.assertIn("/usr/bin/flock --exclusive --timeout 125 --no-fork ${DROPI_SHARED_LOCK}", text)
        self.assertIn("ProtectSystem=strict", text)
        self.assertIn("ProtectHome=read-only", text)
        self.assertNotIn("Persistent=true", text)

    def test_daily_service_runs_followups_after_known_partial_creation_result(self):
        text = (ROOT / "deploy/systemd/dropi-cas-daily.service").read_text(encoding="utf-8")
        expected_followups = (
            "ExecStartPost=/opt/dropi-cas/.venv/bin/python "
            "/opt/dropi-cas/scripts/dropi-cas-followups.py "
            "--allow-external-read --allow-external-writes --budget-seconds 1800 "
            "--journal /opt/dropi-cas/runtime/cas-followups-journal.jsonl "
            "--report /opt/dropi-cas/runtime/reports/cas-followups-report.json "
            "--browser-command /opt/dropi-cas/scripts/dropi-browser-harness"
        )

        self.assertIn("SuccessExitStatus=2", text)
        self.assertIn(expected_followups, text)
        self.assertNotIn("ExecStartPost=/usr/bin/flock", text)
        self.assertLess(text.index("ExecStart="), text.index("ExecStartPost="))

    def test_daily_outer_lock_is_released_before_independent_followup_phase(self):
        """The post phase starts only after the no-fork daily lock holder exits."""
        text = (ROOT / "deploy/systemd/dropi-cas-daily.service").read_text(encoding="utf-8")
        self.assertIn("/usr/bin/flock --exclusive --timeout 125 --no-fork ${DROPI_SHARED_LOCK}", text)
        self.assertIn("ExecStartPost=/opt/dropi-cas/.venv/bin/python", text)

        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            lock_path = temporary_path / "shared.lock"
            daily_event = temporary_path / "daily-finished"
            followup_event = temporary_path / "followups-finished"

            subprocess.run(
                [
                    "/usr/bin/flock",
                    "--exclusive",
                    "--timeout",
                    "1",
                    "--no-fork",
                    str(lock_path),
                    sys.executable,
                    "-c",
                    "from pathlib import Path; import sys; Path(sys.argv[1]).write_text('daily')",
                    str(daily_event),
                ],
                check=True,
            )

            with lock_path.open("a+", encoding="utf-8") as lock_file:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        "from pathlib import Path; import sys; Path(sys.argv[1]).write_text('followups')",
                        str(followup_event),
                    ],
                    check=True,
                )

            self.assertEqual("daily", daily_event.read_text(encoding="utf-8"))
            self.assertEqual("followups", followup_event.read_text(encoding="utf-8"))

    def test_daily_timer_is_daily_bogota_without_catchup(self):
        text = (ROOT / "deploy/systemd/dropi-cas-daily.timer").read_text(encoding="utf-8")
        self.assertIn("OnCalendar=*-*-* 08,16:00:00 America/Bogota", text)
        self.assertIn("Persistent=false", text)
        self.assertIn("Unit=dropi-cas-daily.service", text)


if __name__ == "__main__":
    unittest.main()
