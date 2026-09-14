from __future__ import annotations

import json
import subprocess
from typing import Any


class BrowserHarnessError(RuntimeError):
    pass


class BrowserHarnessRunner:
    def __init__(self, command: str = "browser-harness"):
        self.command = command

    def execute_json(self, code: str, timeout_seconds: int = 90) -> dict[str, Any]:
        process = subprocess.run(
            [self.command],
            input=code,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout_seconds,
            check=False,
        )
        if process.returncode != 0:
            raise BrowserHarnessError(f"Browser runner failed with exit code {process.returncode}.")
        for line in reversed(process.stdout.splitlines()):
            if line.startswith("__JSON__"):
                try:
                    payload = json.loads(line[len("__JSON__"):])
                except json.JSONDecodeError as exc:
                    raise BrowserHarnessError("Browser runner returned invalid JSON.") from exc
                if not isinstance(payload, dict):
                    raise BrowserHarnessError("Browser runner returned a non-object JSON payload.")
                return payload
        raise BrowserHarnessError("Browser runner did not return a __JSON__ payload.")
