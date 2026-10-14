#!/usr/bin/env python3
"""Run one bounded, sequential CAS creation cycle; follow-ups are never sent here.

The only external write is an explicitly authorized CAS creation. Every report is
sanitized: it contains aggregate state only, never guides, order IDs, chat IDs,
paths to evidence, or browser output.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import secrets
import signal
import sqlite3
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPOSITORY_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from dropi_cas_automation.config import load_config

MINIMUM_WRITE_RESERVE_SECONDS = 240.0
BLOCKING_JOURNAL_STATUSES = frozenset({"write_intent", "blocked_write_uncertain", "blocked_remote_readback_mismatch"})
KNOWN_EXCLUDED_PREFLIGHT_STATUSES = frozenset({"not_eligible", "not_eligible_after_history_refresh"})


class CommandFailure(RuntimeError):
    """A CLI command did not provide one trustworthy JSON result."""


CommandRunner = Callable[[list[str], float], dict[str, Any]]


class DurableJournal:
    """Append-only, fsynced journal. It is private local state, never report output."""

    def __init__(self, path: Path):
        self.path = path.expanduser().resolve()

    def events(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        events: list[dict[str, Any]] = []
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                event = json.loads(line)
                if not isinstance(event, dict) or not isinstance(event.get("guide"), str):
                    raise CommandFailure("invalid journal")
                events.append(event)
        return events

    def append(self, *, guide: str, status: str, **values: str) -> dict[str, Any]:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        event: dict[str, Any] = {
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "guide": guide,
            "status": status,
            **values,
        }
        encoded = (json.dumps(event, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")
        descriptor = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
            os.write(descriptor, encoded)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return event


@contextmanager
def exclusive_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise CommandFailure("another daily runner is active") from error
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _terminate_process_group(process: subprocess.Popen[str]) -> None:
    """Terminate and reap a timed-out command and all of its children."""
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.communicate(timeout=5)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.communicate()


def subprocess_command(command: list[str], timeout_seconds: float) -> dict[str, Any]:
    """Run a CLI command under the remaining global deadline, without orphaning it."""
    if timeout_seconds <= 0:
        raise CommandFailure("global deadline exhausted")
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
    except OSError as error:
        raise CommandFailure("command transport failed") from error
    try:
        stdout, _stderr = process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired as error:
        _terminate_process_group(process)
        raise CommandFailure("command deadline exhausted") from error
    if process.returncode != 0:
        raise CommandFailure("command failed")
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as error:
        raise CommandFailure("command returned invalid JSON") from error
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        raise CommandFailure("command returned an unsuccessful result")
    return payload


def _summary(
    *,
    blocked: int,
    created: int,
    verified: int,
    existing: int = 0,
    excluded: int = 0,
    pending: int = 0,
    previously_verified: int = 0,
    followups_pending: int | None = None,
    errors: int = 0,
    partial: bool | None = None,
    authentication: str = "no_verificada",
    writes_authorized: bool = False,
) -> dict[str, Any]:
    return {
        "ok": blocked == 0 and errors == 0,
        "parcial": (blocked > 0 or errors > 0) if partial is None else partial,
        "bloqueado": blocked,
        "errores": errors,
        "creados": created,
        "verificados": verified,
        "existentes": existing,
        "excluidos": excluded,
        "previamente_verificados": previously_verified,
        "pendientes": pending,
        "seguimientos_pendientes": followups_pending,
        "autenticacion": authentication,
        "escrituras_autorizadas": writes_authorized,
    }


def _latest_by_guide(events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for event in events:
        latest[event["guide"]] = event
    return latest


def _command(args: argparse.Namespace, name: str, *extra: str) -> list[str]:
    return [args.dropi_cas_command, name, "--config", args.config, *extra]


def _browser_args(args: argparse.Namespace) -> tuple[str, str]:
    return ("--browser-command", args.browser_command)


def _read_candidates(payload: dict[str, Any]) -> list[dict[str, str]]:
    raw = payload.get("candidates")
    if not isinstance(raw, list):
        raise CommandFailure("candidate schema is invalid")
    candidates: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict) or not isinstance(item.get("guide"), str) or not item["guide"].strip():
            raise CommandFailure("candidate identity is invalid")
        if not isinstance(item.get("order_id"), str) or not item["order_id"].strip():
            raise CommandFailure("candidate identity is invalid")
        candidates.append({"guide": item["guide"], "order_id": item["order_id"]})
    return candidates


def _preflight(args: argparse.Namespace, invoke: CommandRunner, remaining: float, guide: str) -> dict[str, Any]:
    return invoke(
        _command(
            args,
            "preflight",
            "--guide", guide,
            "--allow-external-read",
            "--case-service-type-id", args.case_service_type_id,
            *_browser_args(args),
        ),
        remaining,
    )


def _run_write(args: argparse.Namespace, invoke: CommandRunner, remaining: float, guide: str) -> dict[str, Any]:
    # Deliberately no --limit: it must not mask duplicate local candidates.
    return invoke(
        _command(
            args,
            "run",
            "--execute",
            "--guide", guide,
            "--allow-external-read",
            "--allow-external-writes",
            "--case-service-type-id", args.case_service_type_id,
            *_browser_args(args),
        ),
        remaining,
    )


def _created_chat_id(payload: dict[str, Any], candidate: dict[str, str]) -> str | None:
    results = payload.get("results")
    if not isinstance(results, list) or len(results) != 1 or not isinstance(results[0], dict):
        return None
    result = results[0]
    if result.get("guide") != candidate["guide"]:
        return None
    if result.get("status") != "created" or not isinstance(result.get("chat_id"), str) or not result["chat_id"]:
        return None
    return result["chat_id"]


def _preflight_identity_matches(payload: dict[str, Any], candidate: dict[str, str]) -> bool:
    return payload.get("guide") == candidate["guide"] and payload.get("order_id") == candidate["order_id"]


def _is_prior_day_deferred(event: dict[str, Any], today: str) -> bool:
    if event.get("status") != "deferred_runtime_cap":
        return False
    timestamp = event.get("at")
    return isinstance(timestamp, str) and timestamp[:10] < today


def _pending_followups(database_path: Path) -> int:
    with sqlite3.connect(database_path) as connection:
        value = connection.execute("SELECT COUNT(*) FROM followups WHERE status='pending'").fetchone()[0]
    return int(value)


def _write_report(path: Path, payload: dict[str, Any]) -> None:
    """Atomically write an owner-only, aggregate-only report."""
    report_path = path.expanduser().resolve()
    parent = report_path.parent
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.stat(parent).st_mode & 0o077:
        raise CommandFailure("report directory must be private")
    temporary = parent / f".{report_path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, separators=(",", ":"), sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, report_path)
        os.chmod(report_path, 0o600)
        directory = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def run_daily(
    args: argparse.Namespace,
    invoke: CommandRunner,
    *,
    monotonic: Callable[[], float] = time.monotonic,
    utcnow: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> tuple[int, dict[str, Any]]:
    # The budget begins before every configuration, auth, and browser operation.
    started = monotonic()
    deadline = started + args.max_runtime_seconds
    blocked = created = verified = existing = excluded = pending = previously_verified = errors = 0
    followups_pending: int | None = None
    authentication = "no_verificada"
    writes_authorized = bool(args.allow_external_writes and args.case_service_type_id)

    def remaining() -> float:
        return deadline - monotonic()

    def finish(exit_status: int, *, partial: bool | None = None) -> tuple[int, dict[str, Any]]:
        return exit_status, _summary(
            blocked=blocked,
            created=created,
            verified=verified,
            existing=existing,
            excluded=excluded,
            pending=pending,
            previously_verified=previously_verified,
            followups_pending=followups_pending,
            errors=errors,
            authentication=authentication,
            writes_authorized=writes_authorized,
            partial=partial,
        )

    if not writes_authorized:
        blocked += 1
        return finish(2, partial=True)
    try:
        config = load_config(Path(args.config))
        if config.movement_timezone != args.movement_timezone or config.minimum_hours_without_movement < args.minimum_hours:
            blocked += 1
            return finish(2, partial=True)
        journal = DurableJournal(Path(args.journal))
        with exclusive_lock(Path(f"{journal.path}.lock")):
            previous = _latest_by_guide(journal.events())
            if remaining() <= 0:
                blocked += 1
                errors += 1
                return finish(2, partial=True)
            diagnosis = invoke(
                _command(args, "diagnose-dropi", "--allow-external-read", *_browser_args(args)),
                remaining(),
            )
            if diagnosis.get("logged_in") is not True or diagnosis.get("orders_visible") is not True:
                authentication = "bloqueada"
                blocked += 1
                return finish(2, partial=True)
            authentication = "verificada"
            if remaining() <= 0:
                blocked += 1
                errors += 1
                return finish(2, partial=True)
            invoke(_command(args, "sync", "--allow-external-read", *_browser_args(args)), remaining())
            if config.database_path.exists():
                try:
                    followups_pending = _pending_followups(config.database_path)
                except (OSError, sqlite3.Error):
                    # Do not turn a local reporting read failure into permission to write blindly.
                    errors += 1
            if remaining() <= 0:
                blocked += 1
                errors += 1
                return finish(2, partial=True)
            candidates = _read_candidates(invoke(_command(args, "candidates"), remaining()))
            guides: dict[str, list[dict[str, str]]] = {}
            for candidate in candidates:
                guides.setdefault(candidate["guide"], []).append(candidate)

            today = utcnow().astimezone(timezone.utc).date().isoformat()
            # Preserve source ordering for fresh work. Only old checkpoints receive priority.
            ordered_guides = list(guides)
            ordered_guides.sort(key=lambda guide: not _is_prior_day_deferred(previous.get(guide, {}), today))
            for position, guide in enumerate(ordered_guides):
                if remaining() <= 0:
                    for deferred_guide in ordered_guides[position:]:
                        prior = previous.get(deferred_guide, {})
                        if prior.get("status") in BLOCKING_JOURNAL_STATUSES:
                            blocked += 1
                        else:
                            journal.append(guide=deferred_guide, status="deferred_runtime_cap", item_ref=secrets.token_urlsafe(16))
                            pending += 1
                    break
                rows = guides[guide]
                prior = previous.get(guide, {})
                if prior.get("status") in BLOCKING_JOURNAL_STATUSES:
                    blocked += 1
                    continue
                if len(rows) != 1:
                    journal.append(guide=guide, status="blocked_duplicate_candidate", item_ref=secrets.token_urlsafe(16))
                    blocked += 1
                    continue
                candidate = rows[0]
                if prior.get("status") == "write_verified":
                    previously_verified += 1
                    continue

                try:
                    preflight = _preflight(args, invoke, remaining(), guide)
                except Exception:
                    journal.append(guide=guide, status="blocked_preflight_uncertain", item_ref=secrets.token_urlsafe(16))
                    blocked += 1
                    errors += 1
                    continue
                status = preflight.get("status")
                if status == "existing_case":
                    if not _preflight_identity_matches(preflight, candidate):
                        journal.append(guide=guide, status="blocked_preflight_identity_mismatch", item_ref=secrets.token_urlsafe(16))
                        blocked += 1
                        continue
                    journal.append(guide=guide, status="existing_case", item_ref=secrets.token_urlsafe(16))
                    existing += 1
                    continue
                if status in KNOWN_EXCLUDED_PREFLIGHT_STATUSES:
                    if preflight.get("guide") != candidate["guide"]:
                        journal.append(guide=guide, status="blocked_preflight_identity_mismatch", item_ref=secrets.token_urlsafe(16))
                        blocked += 1
                    else:
                        journal.append(guide=guide, status="excluded_not_eligible", item_ref=secrets.token_urlsafe(16))
                        excluded += 1
                    continue
                if status != "eligible" or not _preflight_identity_matches(preflight, candidate):
                    journal.append(guide=guide, status="blocked_preflight_uncertain", item_ref=secrets.token_urlsafe(16))
                    blocked += 1
                    continue
                # A new write needs enough budget for its own remote verification.
                if remaining() < MINIMUM_WRITE_RESERVE_SECONDS:
                    journal.append(guide=guide, status="deferred_runtime_cap", item_ref=secrets.token_urlsafe(16))
                    pending += 1
                    continue

                journal.append(guide=guide, status="write_intent", item_ref=secrets.token_urlsafe(16))
                try:
                    write_result = _run_write(args, invoke, remaining(), guide)
                    chat_id = _created_chat_id(write_result, candidate)
                    if chat_id is None:
                        results = write_result.get("results")
                        if isinstance(results, list) and len(results) == 1 and isinstance(results[0], dict) and results[0].get("status") == "existing_case" and results[0].get("guide") == candidate["guide"]:
                            journal.append(guide=guide, status="existing_case", item_ref=secrets.token_urlsafe(16))
                            existing += 1
                            continue
                        raise CommandFailure("write outcome is uncertain")
                    readback = _preflight(args, invoke, remaining(), guide)
                    if (
                        readback.get("status") != "existing_case"
                        or not _preflight_identity_matches(readback, candidate)
                        or readback.get("chat_id") != chat_id
                    ):
                        journal.append(guide=guide, status="blocked_remote_readback_mismatch", item_ref=secrets.token_urlsafe(16))
                        blocked += 1
                        continue
                    journal.append(guide=guide, status="write_verified", chat_id=chat_id, item_ref=secrets.token_urlsafe(16))
                    created += 1
                    verified += 1
                except Exception:
                    journal.append(guide=guide, status="blocked_write_uncertain", item_ref=secrets.token_urlsafe(16))
                    blocked += 1
                    errors += 1

            return finish(2 if blocked or errors else 0, partial=bool(blocked or errors or existing or excluded or pending or previously_verified))
    except Exception:
        # Preserve any verified progress from this cycle; never replace it with a synthetic zero.
        blocked += 1
        errors += 1
        return finish(2, partial=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Safe, sequential daily CAS runner; no follow-ups are sent.")
    parser.add_argument("--config", required=True, help="Dropi CAS configuration path.")
    parser.add_argument("--case-service-type-id", required=True, help="Approved immutable CAS service type id.")
    parser.add_argument("--allow-external-writes", action="store_true", help="Explicitly authorize the CAS write command.")
    parser.add_argument("--journal", help="Private append-only journal; defaults under the configured workspace.")
    parser.add_argument("--output-report", required=True, help="Private path for an atomic aggregate JSON report.")
    parser.add_argument(
        "--dropi-cas-command",
        default=str(REPOSITORY_ROOT / ".venv" / "bin" / "dropi-cas"),
        help="CAS CLI executable (default: package virtualenv entry point).",
    )
    parser.add_argument(
        "--browser-command",
        default=str(REPOSITORY_ROOT / "scripts" / "dropi-browser-harness"),
        help="Pinned Dropi browser wrapper (default: package CDP wrapper).",
    )
    parser.add_argument("--minimum-hours", type=float, default=48.0)
    parser.add_argument("--movement-timezone", default="America/Bogota")
    parser.add_argument(
        "--max-runtime-seconds",
        type=float,
        default=7200.0,
        help="Hard global deadline covering diagnostics, sync, reads, writes, and readback (1-7200).",
    )
    return parser


def main(argv: Sequence[str] | None = None, *, run_command: CommandRunner | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.journal is None:
        args.journal = str(Path(args.config).expanduser().resolve().parent / "runtime" / "daily-cas-journal.jsonl")
    if args.max_runtime_seconds <= 0 or args.max_runtime_seconds > 7200:
        payload = _summary(blocked=1, created=0, verified=0, errors=1, partial=True)
        _write_report(Path(args.output_report), payload)
        print(json.dumps(payload, separators=(",", ":"), sort_keys=True))
        return 2
    status, payload = run_daily(args, run_command or subprocess_command)
    _write_report(Path(args.output_report), payload)
    print(json.dumps(payload, separators=(",", ":"), sort_keys=True))
    return status


if __name__ == "__main__":
    raise SystemExit(main())
