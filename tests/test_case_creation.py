import tempfile
import unittest
from pathlib import Path

from dropi_cas_automation.case_creation import DropiCaseCreator


class FakeRunner:
    def __init__(self):
        self.code = ""

    def execute_json(self, code, timeout_seconds):
        self.code = code
        return {"ok": True, "status": "created", "chat_id": "chat-1", "url": "https://example.test/chat-1"}


class CaseCreationTests(unittest.TestCase):
    def test_creation_requires_service_type_binding(self):
        with self.assertRaisesRegex(ValueError, "service type"):
            DropiCaseCreator(FakeRunner(), case_service_type_id="", case_ticket_id="ticket-456")

    def test_creation_requires_explicit_write_permission(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            evidence = Path(temp_dir) / "evidence.png"
            evidence.write_bytes(b"image")
            with self.assertRaises(PermissionError):
                DropiCaseCreator(FakeRunner(), case_service_type_id="service-123", case_ticket_id="ticket-456").create("123", "034000000001", "message", evidence, allow_external_writes=False)

    def test_creation_uses_wizard_contract_and_returns_confirmed_chat(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            evidence = Path(temp_dir) / "evidence.png"
            evidence.write_bytes(b"image")
            runner = FakeRunner()
            result = DropiCaseCreator(runner, case_service_type_id="service-123", case_ticket_id="ticket-456").create("123", "034000000001", "message", evidence, allow_external_writes=True)
            self.assertEqual(result.chat_id, "chat-1")
            self.assertIn("Nueva consulta", runner.code)
            self.assertIn("034000000001", runner.code)
            self.assertIn("service-123", runner.code)
            self.assertIn("/api/v1/service-types", runner.code)
            self.assertIn("cas-types-tickets?casServiceType=", runner.code)
            self.assertIn("click_unique_exact", runner.code)
            self.assertIn("selection_ambiguous", runner.code)
