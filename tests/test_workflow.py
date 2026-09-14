import unittest
from datetime import datetime, timedelta, timezone

from dropi_cas_automation.models import OrderSnapshot
from dropi_cas_automation.workflow import AutomationPipeline, ExternalWriteDisabled


class FakeGateway:
    def __init__(self):
        self.created = []
        self.followups = []

    def load_orders(self):
        return [OrderSnapshot("order-1", "G-1", "carrier-a", "EN TRANSPORTE", datetime.now(timezone.utc) - timedelta(hours=60))]

    def refresh_history(self, order):
        return order

    def validate_case(self, order):
        return "eligible"

    def capture_evidence(self, order):
        return __file__

    def create_case(self, order, evidence_path):
        self.created.append((order.order_id, evidence_path))
        return "chat-1"

    def send_followup(self, order):
        self.followups.append(order.order_id)


class PipelineTests(unittest.TestCase):
    def test_dry_run_evaluates_but_never_creates_cases(self):
        gateway = FakeGateway()

        result = AutomationPipeline(gateway).run(execute=False)

        self.assertEqual(result["summary"], {"eligible": 1})
        self.assertEqual(gateway.created, [])

    def test_execute_requires_explicit_write_permission(self):
        gateway = FakeGateway()

        with self.assertRaises(ExternalWriteDisabled):
            AutomationPipeline(gateway).run(execute=True, allow_external_writes=False)

        self.assertEqual(gateway.created, [])

    def test_execute_creates_validated_candidate_with_evidence(self):
        gateway = FakeGateway()

        result = AutomationPipeline(gateway).run(execute=True, allow_external_writes=True)

        self.assertEqual(result["summary"], {"created": 1})
        self.assertEqual(gateway.created, [("order-1", __file__)])
