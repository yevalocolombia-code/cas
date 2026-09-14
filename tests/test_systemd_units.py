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


if __name__ == "__main__":
    unittest.main()
