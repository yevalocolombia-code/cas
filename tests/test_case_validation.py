import unittest

from dropi_cas_automation.case_validation import DropiCaseValidator


class FakeRunner:
    def __init__(self, result):
        self.result = result
        self.code = ""

    def execute_json(self, code, timeout_seconds):
        self.code = code
        return self.result


class CaseValidationTests(unittest.TestCase):
    def test_validation_requires_read_permission(self):
        with self.assertRaises(PermissionError):
            DropiCaseValidator(FakeRunner({})).validate("123", "carrier-a", allow_external_read=False)

    def test_validation_returns_existing_case_without_write(self):
        checks = {"validation_ok": True, "search_ok": True, "search_schema_ok": True, "service_type_ok": True, "identity_ok": True}
        runner = FakeRunner({"ok": True, "status": "existing_case", "chat_id": "chat-1", "checks": checks})
        result = DropiCaseValidator(runner).validate("123", "carrier-a", allow_external_read=True)
        self.assertEqual(result.status, "existing_case")
        self.assertEqual(result.chat_id, "chat-1")
        self.assertIn("123", runner.code)

    def test_eligible_requires_complete_fail_closed_checks(self):
        missing_proof = DropiCaseValidator(FakeRunner({"ok": True, "status": "eligible"})).validate(
            "123", "carrier-a", allow_external_read=True
        )
        self.assertEqual(missing_proof.status, "validation_error")

        checks = {"validation_ok": True, "search_ok": True, "search_schema_ok": True, "service_type_ok": True, "identity_ok": True}
        proven = DropiCaseValidator(FakeRunner({"ok": True, "status": "eligible", "checks": checks})).validate(
            "123", "carrier-a", allow_external_read=True
        )
        self.assertEqual(proven.status, "eligible")

    def test_browser_script_fails_closed_on_http_network_and_schema_errors(self):
        runner = FakeRunner({"ok": True, "status": "validation_error"})
        DropiCaseValidator(runner).validate("123", "carrier-a", allow_external_read=True)

        self.assertIn("if (!validationResponse.ok)", runner.code)
        self.assertIn("if (!searchResponse.ok)", runner.code)
        self.assertIn("Array.isArray(searchPayload.data)", runner.code)
        self.assertIn("Number.isSafeInteger", runner.code)
        self.assertIn("ambiguous_case_search_response", runner.code)
        self.assertIn("checks.identity_ok", runner.code)
        self.assertIn("version:'2.0.4'", runner.code)
        self.assertIn("cas-types-tickets?casServiceType=", runner.code)
        self.assertIn("service_type_ok", runner.code)
        self.assertIn("catch (error)", runner.code)

    def test_invalid_or_ambiguous_identity_never_becomes_eligible(self):
        checks = {"validation_ok": True, "search_ok": True, "search_schema_ok": True, "identity_ok": False}
        result = DropiCaseValidator(
            FakeRunner({"status": "ambiguous_case_search_response", "checks": checks})
        ).validate("123", "carrier-a", allow_external_read=True)
        self.assertEqual(result.status, "ambiguous_case_search_response")

        missing_identity = DropiCaseValidator(
            FakeRunner({"status": "eligible", "checks": checks})
        ).validate("123", "carrier-a", allow_external_read=True)
        self.assertEqual(missing_identity.status, "validation_error")
