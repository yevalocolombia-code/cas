import unittest

from dropi_cas_automation.history_reader import DropiHistoryReader


class FakeRunner:
    def __init__(self):
        self.code = ""

    def execute_json(self, code, timeout_seconds):
        self.code = code
        return {"ok": True, "text": "ORDEN PARA:\nOrden #1\nNúmero de Guía: 034000000001\nCompañia de envío: carrier-a\nEstatus: EN TRANSPORTE\nHistorial de estados:\n1\t01/01/2026 10:30 AM\tEN TRANSPORTE\tuser\tcomment\nHistorial de Cartera"}


class HistoryReaderTests(unittest.TestCase):
    def test_reader_requires_read_permission(self):
        with self.assertRaises(PermissionError):
            DropiHistoryReader(FakeRunner()).read("034000000001", allow_external_read=False)

    def test_reader_returns_lupa_text_for_requested_guide(self):
        runner = FakeRunner()
        text = DropiHistoryReader(runner).read("034000000001", allow_external_read=True)
        self.assertIn("ORDEN PARA:", text)
        self.assertIn("034000000001", runner.code)
        self.assertIn("goto_url(orders_url)", runner.code)
        self.assertIn("list_tabs(include_chrome=False)", runner.code)
