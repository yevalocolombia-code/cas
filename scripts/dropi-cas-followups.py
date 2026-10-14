#!/usr/bin/env python3
"""Bounded, fail-closed CAS follow-up runner; it never creates or closes CAS."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPOSITORY_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from dropi_cas_automation.cas_followup_cycle import BoundedBrowserHarnessRunner, BrowserCASClient, DurableJournal, atomic_report, run_followup_cycle

SHARED_BROWSER_LOCK = Path(os.environ.get("DROPI_SHARED_LOCK", "/root/yevalo/automation/dropi-novelties/runtime/runner.lock"))
LOCK_TIMEOUT_SECONDS = 125.0
DEFAULT_REPORT = REPOSITORY_ROOT / "runtime" / "reports" / "cas-followups-report.json"
DEFAULT_JOURNAL = REPOSITORY_ROOT / "runtime" / "cas-followups-journal.jsonl"


@contextmanager
def shared_browser_lock(path: Path, *, timeout_seconds: float) -> Iterator[None]:
    """Serialize browser use with the Novedades runner without changing it."""
    # This is another service's pre-existing lock.  Do not create, chmod, or
    # otherwise mutate it: protected-home deployments intentionally expose it
    # read-only while flock still works on an O_RDONLY descriptor.
    descriptor = os.open(path, os.O_RDONLY)
    try:
        deadline = time.monotonic() + timeout_seconds
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("shared_browser_lock")
                time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-external-read", action="store_true", help="allow authenticated CAS inventory and thread reads")
    parser.add_argument("--allow-external-writes", action="store_true", help="allow one exactly revalidated follow-up per due CAS")
    parser.add_argument("--browser-command", default=str(REPOSITORY_ROOT / "scripts" / "dropi-browser-harness"))
    parser.add_argument("--journal", type=Path, default=DEFAULT_JOURNAL)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--budget-seconds", type=int, default=7200)
    parser.add_argument("--shared-lock", type=Path, default=SHARED_BROWSER_LOCK)
    return parser.parse_args(argv)


def blocked_report(reason: str = "authorization") -> dict[str, object]:
    return {
        "ok": False,
        "partial": False,
        "block_reason": reason,
        "inventory": 0,
        "reviewed": 0,
        "due": 0,
        "cooldown": 0,
        "postponed": 0,
        "blocked": 1,
        "sent": 0,
        "verified": 0,
        "reasons": {reason: 1},
    }


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    result: dict[str, object]
    if args.budget_seconds < 1 or args.budget_seconds > 7200:
        result = blocked_report("budget")
    elif args.allow_external_writes and not args.allow_external_read:
        result = blocked_report("authorization")
    elif not args.allow_external_read:
        result = blocked_report("authorization")
    else:
        deadline = time.monotonic() + args.budget_seconds
        try:
            with shared_browser_lock(args.shared_lock, timeout_seconds=min(LOCK_TIMEOUT_SECONDS, max(0.0, deadline - time.monotonic()))):
                client = BrowserCASClient(BoundedBrowserHarnessRunner(command=args.browser_command))
                result = run_followup_cycle(
                    client,
                    DurableJournal(args.journal),
                    allow_external_read=True,
                    allow_external_writes=args.allow_external_writes,
                    budget_seconds=args.budget_seconds,
                    deadline=deadline,
                )
        except Exception:  # browser/network errors are deliberately reported only as blocked
            result = blocked_report("runtime")
    result["generated_at"] = datetime.now(timezone.utc).isoformat()
    result["writes_authorized"] = bool(args.allow_external_writes and args.allow_external_read)
    atomic_report(args.report, result)
    print(json.dumps(result, separators=(",", ":"), sort_keys=True))
    return 0 if result.get("ok") is True else 2


if __name__ == "__main__":
    raise SystemExit(main())
