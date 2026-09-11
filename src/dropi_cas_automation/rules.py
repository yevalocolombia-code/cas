from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import FrozenSet

from .models import EligibilityDecision, OrderSnapshot


DEFAULT_EXCLUDED_STATUSES = frozenset(
    {
        "ENTREGADO",
        "CANCELADO",
        "DEVOLUCION",
        "DEVUELTO",
        "EN DEVOLUCION",
        "RECOGIDO POR DROPI",
        "ENTREGADO A TRANSPORTADORA",
        "PREPARADO PARA TRANSPORTADORA",
        "GUIA GENERADA",
        "GUIA ANULADA",
        "PENDIENTE",
        "PENDIENTE CONFIRMACION",
        "RECHAZADO",
        "RECLAME EN OFICINA",
    }
)


@dataclass(frozen=True)
class EligibilityPolicy:
    minimum_hours_without_movement: float = 24.0
    excluded_statuses: FrozenSet[str] = field(default_factory=lambda: DEFAULT_EXCLUDED_STATUSES)


def normalize_status(value: str) -> str:
    normalized = (value or "").replace("_", " ").replace("-", " ")
    return " ".join(normalized.strip().upper().split())


def evaluate_order(order: OrderSnapshot, policy: EligibilityPolicy, now: datetime | None = None) -> EligibilityDecision:
    if not order.guide.strip():
        return EligibilityDecision("missing_guide", "The order has no shipping guide.", None)
    if order.last_movement_at is None:
        return EligibilityDecision("missing_movement", "The order has no real last-movement timestamp.", None)

    status = normalize_status(order.current_status)
    excluded = {normalize_status(item) for item in policy.excluded_statuses}
    if status in excluded:
        return EligibilityDecision("excluded_status", f"Status is excluded: {status}.", None)

    current_time = now or datetime.now(timezone.utc)
    movement_at = order.last_movement_at
    if movement_at.tzinfo is None:
        movement_at = movement_at.replace(tzinfo=timezone.utc)
    hours = max(0.0, (current_time - movement_at).total_seconds() / 3600)
    if hours <= policy.minimum_hours_without_movement:
        return EligibilityDecision("under_threshold", "Movement is still within the configured threshold.", round(hours, 1))
    return EligibilityDecision("eligible", "Order exceeds the configured no-movement threshold.", round(hours, 1))
