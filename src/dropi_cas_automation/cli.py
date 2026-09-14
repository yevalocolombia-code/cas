from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

from .browser_harness import BrowserHarnessError, BrowserHarnessRunner
from .candidates import load_candidates
from .case_creation import DropiCaseCreator
from .case_store import record_created_case
from .case_validation import DropiCaseValidator
from .config import AppConfig, initialize_workspace, load_config
from .dropi_adapter import DropiSessionAdapter
from .evidence import EvidenceCapture
from .followup import DropiFollowupSender, followup_message
from .followup_store import load_due_followups, mark_followup_sent, mark_followup_skipped
from .history_reader import DropiHistoryReader
from .history_refresh import refresh_guide_history
from .mcp_orders import fetch_mcp_orders, import_mcp_orders
from .models import OrderSnapshot
from .orders_import import import_orders_xlsx
from .report_download import OrdersReportDownloader
from .reporting import build_report
from .rules import EligibilityPolicy, evaluate_order
from .storage import initialize_database


def _setup(config_path: str) -> AppConfig:
    config = load_config(Path(config_path))
    initialize_workspace(config)
    initialize_database(config.database_path)
    return config


def _order_from_json(raw: dict[str, Any]) -> OrderSnapshot:
    movement = raw.get("last_movement_at")
    return OrderSnapshot(
        order_id=str(raw["order_id"]),
        guide=str(raw.get("guide") or ""),
        carrier=str(raw.get("carrier") or ""),
        current_status=str(raw.get("current_status") or ""),
        last_movement_at=datetime.fromisoformat(movement) if movement else None,
    )


def _record_results(config: AppConfig, input_path: Path, results: list[dict[str, Any]]) -> None:
    with sqlite3.connect(config.database_path) as connection:
        cursor = connection.execute(
            "INSERT INTO evaluation_runs(input_path, evaluated_count) VALUES(?, ?)",
            (str(input_path), len(results)),
        )
        run_id = cursor.lastrowid
        connection.executemany(
            """INSERT INTO evaluation_results(run_id, order_id, guide, decision_status, reason, hours_without_movement)
               VALUES(?, ?, ?, ?, ?, ?)""",
            [
                (run_id, row["order_id"], row["guide"], row["status"], row["reason"], row["hours_without_movement"])
                for row in results
            ],
        )


def command_init(args: argparse.Namespace) -> int:
    config = _setup(args.config)
    print(json.dumps({"ok": True, "workspace": str(config.workspace_root), "database": str(config.database_path)}))
    return 0


def command_doctor(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config))
    payload = {
        "ok": True,
        "external_connections_checked": False,
        "workspace": str(config.workspace_root),
        "database": str(config.database_path),
        "minimum_hours_without_movement": config.minimum_hours_without_movement,
        "movement_timezone": config.movement_timezone,
        "notes": ["Doctor is local-only; it does not open a browser, contact an API, or send messages."],
    }
    print(json.dumps(payload))
    return 0


def command_evaluate(args: argparse.Namespace) -> int:
    config = _setup(args.config)
    input_path = Path(args.input).expanduser().resolve()
    raw_orders = json.loads(input_path.read_text(encoding="utf-8"))
    if not isinstance(raw_orders, list):
        raise ValueError("Input must be a JSON array of order objects.")
    policy = EligibilityPolicy(
        minimum_hours_without_movement=config.minimum_hours_without_movement,
        movement_timezone=config.movement_timezone,
    )
    results: list[dict[str, Any]] = []
    for raw in raw_orders:
        order = _order_from_json(raw)
        decision = evaluate_order(order, policy)
        results.append(
            {
                "order_id": order.order_id,
                "guide": order.guide,
                "status": decision.status,
                "reason": decision.reason,
                "hours_without_movement": decision.hours_without_movement,
            }
        )
    _record_results(config, input_path, results)
    print(json.dumps({"ok": True, "mode": "local_evaluation", "summary": dict(Counter(row["status"] for row in results)), "results": results}))
    return 0


def command_diagnose_dropi(args: argparse.Namespace) -> int:
    _ = load_config(Path(args.config))
    adapter = DropiSessionAdapter(
        BrowserHarnessRunner(command=args.browser_command),
        allow_external_read=args.allow_external_read,
    )
    diagnosis = adapter.diagnose()
    print(json.dumps({"ok": True, "mode": "external_read_only", "url": diagnosis.url, "logged_in": diagnosis.logged_in, "orders_visible": diagnosis.orders_visible}))
    return 0


def command_report(args: argparse.Namespace) -> int:
    config = _setup(args.config)
    print(json.dumps({"ok": True, **build_report(config.database_path)}))
    return 0


def command_candidates(args: argparse.Namespace) -> int:
    config = _setup(args.config)
    rows = load_candidates(
        config.database_path,
        minimum_hours_without_movement=config.minimum_hours_without_movement,
        movement_timezone=config.movement_timezone,
    )
    candidates = [
        {
            "order_id": row.order_id,
            "guide": row.guide,
            "carrier": row.carrier,
            "current_status": row.current_status,
            "last_movement_at": row.last_movement_at.isoformat(sep=" ") if row.last_movement_at else None,
        }
        for row in rows
    ]
    print(json.dumps({"ok": True, "count": len(candidates), "candidates": candidates}))
    return 0


def command_sync(args: argparse.Namespace) -> int:
    config = _setup(args.config)
    if not args.allow_external_read:
        raise PermissionError("sync requires --allow-external-read because it reads the configured external order source.")
    if config.orders_source == "mcp":
        if config.mcp_config_path is None:
            raise ValueError("[orders_source].mcp_config_path is required when provider is 'mcp'.")
        orders, meta = fetch_mcp_orders(config.mcp_config_path, window_days=config.mcp_window_days)
        source_ref = f"mcp:{(meta.get('range') or {}).get('from', '?')}:{(meta.get('range') or {}).get('until', '?')}"
        result = import_mcp_orders(config.database_path, orders, source_ref=source_ref)
        print(json.dumps({"ok": True, "source": "mcp", "meta": meta, **result}))
        return 0
    downloaded = OrdersReportDownloader(BrowserHarnessRunner(command=args.browser_command), config.workspace_root / "downloads").download(allow_external_read=True)
    result = import_orders_xlsx(config.database_path, downloaded)
    print(json.dumps({"ok": True, "source": "excel", "report_path": str(downloaded), **result}))
    return 0


def command_refresh_history(args: argparse.Namespace) -> int:
    config = _setup(args.config)
    if not args.allow_external_read:
        raise PermissionError("refresh-history requires --allow-external-read because it opens an order in Dropi.")
    result = refresh_guide_history(config.database_path, DropiHistoryReader(BrowserHarnessRunner(command=args.browser_command)), args.guide, allow_external_read=True)
    print(json.dumps({"ok": True, **result}))
    return 0


def command_preflight(args: argparse.Namespace) -> int:
    """Refresh and validate one guide without creating or messaging a CAS."""
    config = _setup(args.config)
    if not args.allow_external_read:
        raise PermissionError("preflight requires --allow-external-read because it inspects Dropi and captures local evidence.")

    runner = BrowserHarnessRunner(command=args.browser_command)
    refresh_guide_history(
        config.database_path,
        DropiHistoryReader(runner),
        args.guide,
        allow_external_read=True,
    )
    candidates = load_candidates(
        config.database_path,
        minimum_hours_without_movement=config.minimum_hours_without_movement,
        movement_timezone=config.movement_timezone,
    )
    item = next((candidate for candidate in candidates if candidate.guide == args.guide), None)
    if item is None:
        print(
            json.dumps(
                {
                    "ok": True,
                    "mode": "read_only_preflight",
                    "guide": args.guide,
                    "status": "not_eligible_after_history_refresh",
                }
            )
        )
        return 0

    validation = DropiCaseValidator(
        runner,
        case_service_type_id=args.case_service_type_id,
    ).validate(item.order_id, item.carrier, allow_external_read=True)
    payload = {
        "ok": True,
        "mode": "read_only_preflight",
        "order_id": item.order_id,
        "guide": item.guide,
        "status": validation.status,
    }
    if validation.chat_id:
        payload["chat_id"] = validation.chat_id
    if validation.status != "eligible":
        print(json.dumps(payload))
        return 0

    evidence_path = EvidenceCapture(runner, config.evidence_dir).capture(
        item.guide,
        allow_external_read=True,
    )
    payload["evidence_path"] = str(evidence_path)
    print(json.dumps(payload))
    return 0


def command_run(args: argparse.Namespace) -> int:
    config = _setup(args.config)
    candidates = load_candidates(
        config.database_path,
        minimum_hours_without_movement=config.minimum_hours_without_movement,
        movement_timezone=config.movement_timezone,
    )
    if args.guide:
        candidates = [candidate for candidate in candidates if candidate.guide == args.guide]
    if args.limit is not None:
        if args.limit < 1:
            raise ValueError("--limit must be at least 1.")
        candidates = candidates[:args.limit]
    if args.dry_run:
        print(json.dumps({"ok": True, "mode": "dry_run", "candidate_count": len(candidates), "candidates": [{"order_id": item.order_id, "guide": item.guide} for item in candidates]}))
        return 0
    if not args.guide or args.limit not in (None, 1):
        raise PermissionError(
            "run --execute requires exactly one explicit --guide; --limit must be omitted or set to 1."
        )
    if not (args.allow_external_read and args.allow_external_writes and args.case_service_type_id):
        raise PermissionError(
            "run --execute requires --allow-external-read, --allow-external-writes, "
            "and --case-service-type-id so duplicate detection cannot be skipped."
        )
    if len(candidates) != 1:
        raise PermissionError(
            f"run --execute requires exactly one candidate for --guide; found {len(candidates)}."
        )
    runner = BrowserHarnessRunner(command=args.browser_command)
    validator = DropiCaseValidator(runner, case_service_type_id=args.case_service_type_id)
    evidence = EvidenceCapture(runner, config.evidence_dir)
    results = []
    for item in candidates:
        refresh_guide_history(config.database_path, DropiHistoryReader(runner), item.guide, allow_external_read=True)
        refreshed_candidates = load_candidates(
            config.database_path,
            minimum_hours_without_movement=config.minimum_hours_without_movement,
            movement_timezone=config.movement_timezone,
        )
        refreshed_matches = [
            candidate
            for candidate in refreshed_candidates
            if candidate.guide == item.guide
        ]
        if not refreshed_matches:
            results.append({"guide": item.guide, "status": "skipped_not_eligible_after_refresh"})
            continue
        if len(refreshed_matches) != 1 or refreshed_matches[0].order_id != item.order_id:
            results.append({"guide": item.guide, "status": "blocked_ambiguous_after_refresh"})
            continue
        item = refreshed_matches[0]
        validation = validator.validate(item.order_id, item.carrier, allow_external_read=True)
        if validation.status != "eligible":
            results.append({"guide": item.guide, "status": validation.status})
            continue
        path = evidence.capture(item.guide, allow_external_read=True)
        threshold = f"{config.minimum_hours_without_movement:g}"
        message = f"Buen día. La guía lleva {threshold} horas o más sin actualización. Solicito validar y gestionar avance prioritario. Gracias."
        creator = DropiCaseCreator(
            runner,
            case_service_type_id=args.case_service_type_id,
            case_ticket_id=str(validation.ticket_id),
        )
        created = creator.create(item.order_id, item.guide, message, path, allow_external_writes=True)
        if created.status == "existing_case":
            results.append({"guide": item.guide, "status": "existing_case"})
            continue
        stored = record_created_case(config.database_path, item.order_id, item.guide, str(created.chat_id), str(path), message, followup_hours=48)
        results.append({"guide": item.guide, "status": "created", **stored})
    print(json.dumps({"ok": True, "mode": "execute", "results": results}))
    return 0


def command_followups(args: argparse.Namespace) -> int:
    config = _setup(args.config)
    due = load_due_followups(config.database_path, limit=args.limit)
    if args.dry_run:
        print(json.dumps({"ok": True, "mode": "dry_run", "due_count": len(due), "followups": [{"id": item.id, "guide": item.guide, "chat_id": item.chat_id, "due_at": item.due_at.isoformat(sep=" ")} for item in due]}))
        return 0
    if not (args.allow_external_read and args.allow_external_writes and args.case_service_type_id):
        raise PermissionError("followups --execute requires --allow-external-read, --allow-external-writes, and --case-service-type-id.")
    runner = BrowserHarnessRunner(command=args.browser_command)
    validator = DropiCaseValidator(runner, case_service_type_id=args.case_service_type_id)
    sender = DropiFollowupSender(runner)
    results = []
    message = followup_message()
    for item in due:
        refresh_guide_history(config.database_path, DropiHistoryReader(runner), item.guide, allow_external_read=True)
        candidates = load_candidates(
            config.database_path,
            minimum_hours_without_movement=config.minimum_hours_without_movement,
            movement_timezone=config.movement_timezone,
        )
        if item.order_id not in {candidate.order_id for candidate in candidates}:
            mark_followup_skipped(config.database_path, item.id, "order_no_longer_eligible_after_history_refresh")
            results.append({"guide": item.guide, "status": "skipped_not_eligible"})
            continue
        validation = validator.validate(item.order_id, item.carrier, allow_external_read=True)
        if validation.status != "existing_case":
            mark_followup_skipped(config.database_path, item.id, f"active_case_not_confirmed:{validation.status}")
            results.append({"guide": item.guide, "status": "skipped_active_case_not_confirmed"})
            continue
        sender.send(item.chat_id, message, allow_external_writes=True)
        mark_followup_sent(config.database_path, item.id, message)
        results.append({"guide": item.guide, "status": "sent", "chat_id": item.chat_id})
    print(json.dumps({"ok": True, "mode": "execute", "results": results}))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dropi-cas", description="Portable CAS automation core")
    commands = parser.add_subparsers(dest="command", required=True)
    for name, handler, help_text in (
        ("init", command_init, "Create only the configured local workspace and database."),
        ("doctor", command_doctor, "Validate local configuration without external calls."),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--config", required=True)
        command.set_defaults(handler=handler)
    evaluate = commands.add_parser("evaluate", help="Evaluate local JSON input; never contacts external systems.")
    evaluate.add_argument("--config", required=True)
    evaluate.add_argument("--input", required=True)
    evaluate.set_defaults(handler=command_evaluate)
    diagnose_dropi = commands.add_parser("diagnose-dropi", help="Read-only browser session check; requires explicit opt-in.")
    diagnose_dropi.add_argument("--config", required=True)
    diagnose_dropi.add_argument("--allow-external-read", action="store_true", help="Allow a read-only browser session check against Dropi.")
    diagnose_dropi.add_argument("--browser-command", default="browser-harness", help="Browser runner command; default: browser-harness.")
    diagnose_dropi.set_defaults(handler=command_diagnose_dropi)
    report = commands.add_parser("report", help="Summarize the local automation database; never contacts external systems.")
    report.add_argument("--config", required=True)
    report.set_defaults(handler=command_report)
    candidates = commands.add_parser("candidates", help="List local candidates from persisted real movements; never contacts external systems.")
    candidates.add_argument("--config", required=True)
    candidates.set_defaults(handler=command_candidates)
    sync = commands.add_parser("sync", help="Download and import the official orders report; requires explicit read opt-in.")
    sync.add_argument("--config", required=True)
    sync.add_argument("--allow-external-read", action="store_true")
    sync.add_argument("--browser-command", default="browser-harness")
    sync.set_defaults(handler=command_sync)
    refresh_history = commands.add_parser("refresh-history", help="Read one order history from Dropi and persist it; requires explicit read opt-in.")
    refresh_history.add_argument("--config", required=True)
    refresh_history.add_argument("--guide", required=True)
    refresh_history.add_argument("--allow-external-read", action="store_true")
    refresh_history.add_argument("--browser-command", default="browser-harness")
    refresh_history.set_defaults(handler=command_refresh_history)
    preflight = commands.add_parser(
        "preflight",
        help="Refresh, validate, detect an existing CAS, and capture evidence for one guide without external writes.",
    )
    preflight.add_argument("--config", required=True)
    preflight.add_argument("--guide", required=True)
    preflight.add_argument("--allow-external-read", action="store_true")
    preflight.add_argument("--case-service-type-id", required=True)
    preflight.add_argument("--browser-command", default="browser-harness")
    preflight.set_defaults(handler=command_preflight)
    run = commands.add_parser("run", help="Run CAS candidates in dry-run mode or with explicit external read/write permissions.")
    run.add_argument("--config", required=True)
    run.add_argument("--guide", help="Operate only the explicitly approved guide; required with --execute.")
    mode = run.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--execute", action="store_true")
    run.add_argument("--limit", type=int)
    run.add_argument("--allow-external-read", action="store_true")
    run.add_argument("--allow-external-writes", action="store_true")
    run.add_argument("--case-service-type-id", default="")
    run.add_argument("--browser-command", default="browser-harness")
    run.set_defaults(handler=command_run)
    followups = commands.add_parser("followups", help="Audit or send due follow-ups with explicit read/write permissions.")
    followups.add_argument("--config", required=True)
    followup_mode = followups.add_mutually_exclusive_group(required=True)
    followup_mode.add_argument("--dry-run", action="store_true")
    followup_mode.add_argument("--execute", action="store_true")
    followups.add_argument("--limit", type=int)
    followups.add_argument("--allow-external-read", action="store_true")
    followups.add_argument("--allow-external-writes", action="store_true")
    followups.add_argument("--case-service-type-id", default="")
    followups.add_argument("--browser-command", default="browser-harness")
    followups.set_defaults(handler=command_followups)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.handler(args)
    except (OSError, ValueError, KeyError, json.JSONDecodeError, BrowserHarnessError) as error:
        print(json.dumps({"ok": False, "error": str(error)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
