from __future__ import annotations

import subprocess
import unittest
from unittest.mock import patch

from dropi_cas_automation.browser_harness import BrowserHarnessError, BrowserHarnessRunner


class BrowserHarnessRunnerTests(unittest.TestCase):
    @patch("dropi_cas_automation.browser_harness.subprocess.run")
    def test_runner_error_does_not_echo_browser_output(self, run) -> None:
        run.return_value = subprocess.CompletedProcess(
            args=["browser-harness"],
            returncode=9,
            stdout="customer@example.com SECRET_PAGE_TEXT",
        )

        with self.assertRaises(BrowserHarnessError) as raised:
            BrowserHarnessRunner().execute_json("print('x')")

        message = str(raised.exception)
        self.assertEqual(message, "Browser runner failed with exit code 9.")
        self.assertNotIn("customer@example.com", message)
        self.assertNotIn("SECRET_PAGE_TEXT", message)

    @patch("dropi_cas_automation.browser_harness.subprocess.run")
    def test_runner_rejects_invalid_json_without_echoing_payload(self, run) -> None:
        run.return_value = subprocess.CompletedProcess(
            args=["browser-harness"],
            returncode=0,
            stdout="__JSON__{private-data-not-json}",
        )

        with self.assertRaises(BrowserHarnessError) as raised:
            BrowserHarnessRunner().execute_json("print('x')")

        self.assertEqual(str(raised.exception), "Browser runner returned invalid JSON.")
        self.assertNotIn("private-data", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
