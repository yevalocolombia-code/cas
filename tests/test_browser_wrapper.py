from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
WRAPPER = ROOT / "scripts" / "dropi-browser-harness"


class DropiBrowserHarnessWrapperTests(unittest.TestCase):
    def test_sets_fail_closed_runtime_defaults_and_forwards_stdin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "fake-harness"
            fake.write_text(
                "#!/usr/bin/env python3\n"
                "import json, os, sys\n"
                "print(json.dumps({\n"
                "  'stdin': sys.stdin.read(),\n"
                "  'args': sys.argv[1:],\n"
                "  'BH_HOME': os.environ.get('BH_HOME'),\n"
                "  'BU_CDP_URL': os.environ.get('BU_CDP_URL'),\n"
                "  'BH_TELEMETRY': os.environ.get('BH_TELEMETRY'),\n"
                "  'BH_RECORD': os.environ.get('BH_RECORD'),\n"
                "  'BH_DOMAIN_SKILLS': os.environ.get('BH_DOMAIN_SKILLS'),\n"
                "  'BH_TAB_MARKER': os.environ.get('BH_TAB_MARKER'),\n"
                "  'BU_AUTOSPAWN': os.environ.get('BU_AUTOSPAWN'),\n"
                "  'BH_OPEN_LIVE_URL': os.environ.get('BH_OPEN_LIVE_URL'),\n"
                "}))\n",
                encoding="utf-8",
            )
            fake.chmod(0o700)
            env = os.environ.copy()
            env["DROPI_BROWSER_HARNESS_BIN"] = str(fake)
            result = subprocess.run(
                [str(WRAPPER), "doctor", "--json"],
                input="print('sentinel')\n",
                text=True,
                capture_output=True,
                env=env,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["stdin"], "print('sentinel')\n")
            self.assertEqual(payload["args"], ["doctor", "--json"])
            self.assertEqual(payload["BH_HOME"], "/opt/dropi-cas/runtime/browser-harness")
            self.assertEqual(payload["BU_CDP_URL"], "http://127.0.0.1:9229")
            self.assertEqual(payload["BH_TELEMETRY"], "0")
            self.assertEqual(payload["BH_RECORD"], "0")
            self.assertEqual(payload["BH_DOMAIN_SKILLS"], "0")
            self.assertEqual(payload["BH_TAB_MARKER"], "0")
            self.assertEqual(payload["BU_AUTOSPAWN"], "0")
            self.assertEqual(payload["BH_OPEN_LIVE_URL"], "0")

    def test_preserves_explicit_environment_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "fake-harness"
            fake.write_text(
                "#!/usr/bin/env python3\n"
                "import json, os\n"
                "print(json.dumps({'BU_CDP_URL': os.environ.get('BU_CDP_URL'), 'BH_RECORD': os.environ.get('BH_RECORD')}))\n",
                encoding="utf-8",
            )
            fake.chmod(0o700)
            env = os.environ.copy()
            env.update(
                {
                    "DROPI_BROWSER_HARNESS_BIN": str(fake),
                    "BU_CDP_URL": "http://127.0.0.1:9333",
                    "BH_RECORD": "1",
                }
            )
            result = subprocess.run([str(WRAPPER)], text=True, capture_output=True, env=env, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                json.loads(result.stdout),
                {"BU_CDP_URL": "http://127.0.0.1:9333", "BH_RECORD": "1"},
            )


if __name__ == "__main__":
    unittest.main()
