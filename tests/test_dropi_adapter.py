import unittest

from dropi_cas_automation.dropi_adapter import DropiSessionAdapter, ExternalReadDisabled


class FakeRunner:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def execute_json(self, code, timeout_seconds):
        self.calls.append((code, timeout_seconds))
        return self.response


class DropiAdapterTests(unittest.TestCase):
    def test_blocks_browser_access_until_external_read_is_explicitly_enabled(self):
        runner = FakeRunner({"ok": True})
        adapter = DropiSessionAdapter(runner, allow_external_read=False)

        with self.assertRaises(ExternalReadDisabled):
            adapter.diagnose()

        self.assertEqual(runner.calls, [])

    def test_diagnose_returns_session_metadata_from_browser_runner(self):
        runner = FakeRunner(
            {
                "ok": True,
                "url": "https://app.dropi.co/dashboard/orders",
                "logged_in": True,
                "orders_visible": True,
            }
        )
        adapter = DropiSessionAdapter(runner, allow_external_read=True)

        result = adapter.diagnose()

        self.assertTrue(result.logged_in)
        self.assertTrue(result.orders_visible)
        self.assertEqual(len(runner.calls), 1)
        self.assertIn("https://app.dropi.co/dashboard/orders", runner.calls[0][0])

    def test_diagnosis_uses_session_token_and_login_path_not_page_specific_text(self):
        runner = FakeRunner(
            {
                "ok": True,
                "url": "https://app.dropi.co/dashboard/cas/tray",
                "logged_in": True,
                "orders_visible": False,
            }
        )
        result = DropiSessionAdapter(runner, allow_external_read=True).diagnose()

        code = runner.calls[0][0]
        self.assertTrue(result.logged_in)
        self.assertIn("localStorage.getItem('DROPI_token')", code)
        self.assertIn("/auth/login", code)
        self.assertNotIn("'Correo' not in", code)
