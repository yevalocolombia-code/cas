from __future__ import annotations

from pathlib import Path
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
WRAPPER = ROOT / "scripts" / "dropi-browser-harness"


class DropiBrowserHarnessWrapperTests(unittest.TestCase):
    def test_wrapper_has_fixed_fail_closed_runtime_configuration(self) -> None:
        text = WRAPPER.read_text(encoding="utf-8")

        expected = {
            'export BH_HOME="/opt/dropi-cas/runtime/browser-harness"',
            'export BU_CDP_URL="http://127.0.0.1:9229"',
            'export BH_TELEMETRY="0"',
            'export BH_RECORD="0"',
            'export BH_DOMAIN_SKILLS="0"',
            'export BH_TAB_MARKER="0"',
            'export BU_AUTOSPAWN="0"',
            'export BH_OPEN_LIVE_URL="0"',
            'exec /opt/browser-harness/.venv/bin/browser-harness "$@"',
        }
        for line in expected:
            with self.subTest(line=line):
                self.assertIn(line, text)
        self.assertNotIn("DROPI_BROWSER_HARNESS_BIN", text)
        self.assertNotIn("${BH_", text)
        self.assertNotIn("${BU_", text)

    def test_wrapper_has_valid_bash_syntax(self) -> None:
        result = subprocess.run(
            ["/usr/bin/bash", "-n", str(WRAPPER)],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
