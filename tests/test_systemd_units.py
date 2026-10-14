from pathlib import Path
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
        self.assertIn("TimeoutStartSec=7500", text)
        self.assertIn("KillMode=control-group", text)
        self.assertIn("/usr/bin/flock --exclusive --timeout 125 --no-fork ${DROPI_SHARED_LOCK}", text)
        self.assertIn("ProtectSystem=strict", text)
        self.assertIn("ProtectHome=read-only", text)
        self.assertNotIn("Persistent=true", text)

    def test_daily_timer_is_daily_bogota_without_catchup(self):
        text = (ROOT / "deploy/systemd/dropi-cas-daily.timer").read_text(encoding="utf-8")
        self.assertIn("OnCalendar=*-*-* 08,16:00:00 America/Bogota", text)
        self.assertIn("Persistent=false", text)
        self.assertIn("Unit=dropi-cas-daily.service", text)


if __name__ == "__main__":
    unittest.main()
