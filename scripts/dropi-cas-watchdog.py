#!/usr/bin/env python3
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any


class CycleError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _invoke(cli: str, *args: str, timeout: int = 300) -> dict[str, Any]:
    try:
        result = subprocess.run(
            [cli, *args],
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        code = "command_unavailable" if isinstance(exc, OSError) else "command_timeout"
        raise CycleError(f"{args[0]}:{code}") from exc
    if result.returncode != 0:
        raise CycleError(f"{args[0]}:exit_{result.returncode}")
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    if not lines:
        raise CycleError(f"{args[0]}:empty_output")
    try:
        payload = json.loads(lines[-1])
    except json.JSONDecodeError as exc:
        raise CycleError(f"{args[0]}:invalid_json") from exc
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        raise CycleError(f"{args[0]}:reported_failure")
    return payload


def collect_summary(cli: str, config: str, browser_command: str) -> dict[str, Any]:
    diagnosis = _invoke(
        cli,
        "diagnose-dropi",
        "--config",
        config,
        "--allow-external-read",
        "--browser-command",
        browser_command,
    )
    if diagnosis.get("logged_in") is not True:
        raise CycleError("dropi_session_not_authenticated")

    sync = _invoke(cli, "sync", "--config", config, "--allow-external-read")
    candidates = _invoke(cli, "candidates", "--config", config)
    followups = _invoke(cli, "followups", "--config", config, "--dry-run")
    report = _invoke(cli, "report", "--config", config)
    meta: dict[str, Any] = {}
    raw_meta = sync.get("meta")
    if isinstance(raw_meta, dict):
        meta = raw_meta
    return {
        "status": "ok",
        "authenticated": True,
        "orders_visible": bool(diagnosis.get("orders_visible")),
        "source": str(sync.get("source") or "unknown"),
        "imported": int(sync.get("imported") or 0),
        "truncated": bool(meta.get("truncated")),
        "orders": int(report.get("orders") or 0),
        "candidate_count": int(candidates.get("count") or 0),
        "cases_open": int(report.get("cases_open") or report.get("cases") or 0),
        "followups_pending": int(report.get("followups_pending") or 0),
        "followups_due": int(followups.get("due_count") or report.get("followups_due") or 0),
        "write_mode": "disabled",
    }


def _signature(state: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in state.items() if key != "observed_at"}


def _write_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
    os.chmod(temp, 0o600)
    temp.replace(path)


def _read_state(path: Path) -> dict[str, Any] | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return raw if isinstance(raw, dict) else None


def _success_message(state: dict[str, Any]) -> str:
    return (
        "CAS Dropi operativo: sesión autenticada; "
        f"{state['orders']} pedidos locales, {state['candidate_count']} candidatos, "
        f"{state['cases_open']} CAS abiertos y {state['followups_due']} seguimientos vencidos. "
        "Ciclo automático de lectura completado; escrituras externas deshabilitadas."
    )


def main() -> int:
    cli = os.environ.get("DROPI_CAS_CLI", "/opt/dropi-cas/.venv/bin/dropi-cas")
    config = os.environ.get("DROPI_CAS_CONFIG", "/opt/dropi-cas/config.toml")
    browser_command = os.environ.get("DROPI_CAS_BROWSER_COMMAND", "browser-harness")
    state_path = Path(
        os.environ.get(
            "DROPI_CAS_WATCHDOG_STATE",
            "/opt/dropi-cas/runtime/data/watchdog-state.json",
        )
    )
    lock_path = Path(
        os.environ.get(
            "DROPI_CAS_WATCHDOG_LOCK",
            "/opt/dropi-cas/runtime/data/watchdog.lock",
        )
    )
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0

        previous = _read_state(state_path)
        state: dict[str, Any]
        try:
            state = collect_summary(cli, config, browser_command)
            message = _success_message(state)
        except CycleError as exc:
            state = {
                "status": "error",
                "code": exc.code,
                "write_mode": "disabled",
            }
            message = (
                f"⚠️ CAS Dropi requiere atención: {exc.code}. "
                "El ciclo se detuvo de forma segura y no realizó escrituras externas."
            )
        state["observed_at"] = int(time.time())
        changed = previous is None or _signature(previous) != _signature(state)
        _write_state(state_path, state)
        if changed:
            print(message)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        print("CAS Dropi watchdog failed unexpectedly; no external writes were attempted.", file=sys.stderr)
        raise
