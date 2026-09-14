from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .models import OrderSnapshot
from .rules import EligibilityPolicy, evaluate_order, movement_instant


def load_candidates(
    database_path: Path,
    *,
    minimum_hours_without_movement: float,
    movement_timezone: str = "America/Bogota",
    now: datetime | None = None,
) -> list[OrderSnapshot]:
    policy = EligibilityPolicy(
        minimum_hours_without_movement=minimum_hours_without_movement,
        movement_timezone=movement_timezone,
    )
    current_time = now or datetime.now(timezone.utc)
    candidates: list[OrderSnapshot] = []
    with sqlite3.connect(database_path) as connection:
        for order_id, guide, status, carrier, last_movement_at in connection.execute(
            "SELECT order_id, guide, status, carrier, last_movement_at FROM orders WHERE guide IS NOT NULL AND last_movement_at IS NOT NULL"
        ):
            try:
                movement = datetime.fromisoformat(last_movement_at)
            except (TypeError, ValueError):
                continue
            snapshot = OrderSnapshot(str(order_id), str(guide), str(carrier or ""), str(status or ""), movement)
            if evaluate_order(snapshot, policy, now=current_time).status == "eligible":
                candidates.append(snapshot)
    def sort_key(item: OrderSnapshot) -> datetime:
        if item.last_movement_at is None:
            return datetime.max.replace(tzinfo=timezone.utc)
        return movement_instant(item.last_movement_at, movement_timezone)

    return sorted(candidates, key=sort_key)
