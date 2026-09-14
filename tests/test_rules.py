from datetime import datetime, timedelta, timezone
import unittest

from dropi_cas_automation.models import OrderSnapshot
from dropi_cas_automation.rules import EligibilityPolicy, evaluate_order


def make_order(*, status="EN BODEGA DESTINO", hours=49, guide="034000000001"):
    return OrderSnapshot(
        order_id="order-1",
        guide=guide,
        carrier="carrier-a",
        current_status=status,
        last_movement_at=datetime.now(timezone.utc) - timedelta(hours=hours),
    )


class EligibilityRuleTests(unittest.TestCase):
    def test_marks_open_order_over_threshold_as_eligible(self):
        decision = evaluate_order(make_order(), EligibilityPolicy())

        self.assertEqual(decision.status, "eligible")
        self.assertGreaterEqual(decision.hours_without_movement or 0, 49)

    def test_exact_threshold_is_eligible_in_configured_provider_timezone(self):
        order = OrderSnapshot(
            order_id="order-48h",
            guide="034000000048",
            carrier="carrier-a",
            current_status="EN TRANSPORTE",
            last_movement_at=datetime(2026, 9, 12, 13, 0),
        )
        decision = evaluate_order(
            order,
            EligibilityPolicy(minimum_hours_without_movement=48, movement_timezone="America/Bogota"),
            now=datetime(2026, 9, 14, 18, 0, tzinfo=timezone.utc),
        )

        self.assertEqual(decision.status, "eligible")
        self.assertEqual(decision.hours_without_movement, 48.0)

    def test_preserves_explicit_timestamp_offsets(self):
        order = OrderSnapshot(
            order_id="order-aware",
            guide="034000000049",
            carrier="carrier-a",
            current_status="EN TRANSPORTE",
            last_movement_at=datetime(2026, 9, 12, 18, 0, tzinfo=timezone.utc),
        )
        decision = evaluate_order(
            order,
            EligibilityPolicy(minimum_hours_without_movement=48, movement_timezone="America/Bogota"),
            now=datetime(2026, 9, 14, 18, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(decision.status, "eligible")
        self.assertEqual(decision.hours_without_movement, 48.0)

    def test_excludes_closed_status_even_when_old(self):
        decision = evaluate_order(make_order(status="ENTREGADO", hours=72), EligibilityPolicy())

        self.assertEqual(decision.status, "excluded_status")

    def test_normalizes_provider_status_separators_before_exclusion(self):
        for status in ("GUIA_GENERADA", "PENDIENTE-CONFIRMACION", "GUIA   GENERADA", "DEVOLUCIÓN"):
            with self.subTest(status=status):
                decision = evaluate_order(make_order(status=status, hours=72), EligibilityPolicy())
                self.assertEqual(decision.status, "excluded_status")

    def test_excludes_rejected_and_cancelled_guide_statuses(self):
        for status in ("RECHAZADO", "GUIA_ANULADA"):
            with self.subTest(status=status):
                decision = evaluate_order(make_order(status=status, hours=72), EligibilityPolicy())
                self.assertEqual(decision.status, "excluded_status")

    def test_requires_a_guide_and_a_real_last_movement(self):
        missing_guide = make_order(guide="")
        missing_movement = OrderSnapshot(
            order_id="order-2",
            guide="034000000002",
            carrier="carrier-a",
            current_status="EN TRANSPORTE",
            last_movement_at=None,
        )

        self.assertEqual(evaluate_order(missing_guide, EligibilityPolicy()).status, "missing_guide")
        self.assertEqual(evaluate_order(missing_movement, EligibilityPolicy()).status, "missing_movement")
