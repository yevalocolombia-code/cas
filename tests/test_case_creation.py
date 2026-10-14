import json
import re
import subprocess
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
    @staticmethod
    def _run_open_case_javascript(script: str, rows: list[dict[str, object]]) -> dict[str, object]:
        node_program = r'''
const fs = require('fs');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
let clickCount = 0;
const visibleElement = (text = '') => ({
  innerText: text,
  textContent: text,
  getBoundingClientRect: () => ({width: 100, height: 20}),
});
const makeRow = (definition) => {
  const cells = definition.cells.map(visibleElement);
  const guideLinks = definition.guides.map(visibleElement);
  const action = {
    ...visibleElement(),
    click: () => { clickCount += 1; },
  };
  return {
    ...visibleElement(),
    querySelectorAll: (selector) => {
      if (selector === 'td') return cells;
      if (selector === 'a') return guideLinks;
      if (selector === 'a[title="Nueva consulta"]') return [action];
      throw new Error(`unexpected selector: ${selector}`);
    },
  };
};
const rows = input.rows.map(makeRow);
global.document = {
  querySelectorAll: (selector) => {
    if (selector === 'table tbody tr') return rows;
    throw new Error(`unexpected document selector: ${selector}`);
  },
};
try {
  const result = eval(input.script);
  process.stdout.write(JSON.stringify({result, clickCount}));
} catch (error) {
  console.error(error.stack);
  process.exit(1);
}
'''
        completed = subprocess.run(
            ["node", "-e", node_program],
            input=json.dumps({"script": script, "rows": rows}),
            capture_output=True,
            check=False,
            text=True,
        )
        if completed.returncode:
            raise AssertionError(f"Node DOM execution failed:\n{completed.stderr}")
        return json.loads(completed.stdout)

    def test_generated_order_identity_javascript_clicks_only_exact_order_and_guide(self):
        script = DropiCaseCreator._script(
            "ORDER-123",
            "034000000001",
            "message",
            Path("/tmp/evidence.png"),
            "service-123",
            "ticket-456",
            "Transportadora",
            "Ordenes sin movimiento",
        )
        match = re.search(r'opened = js\(f"""(.*?)"""\)', script, re.DOTALL)
        if match is None:
            self.fail("Generated CAS script does not contain the order-opening JavaScript.")
        javascript = eval(
            f'f"""{match.group(1)}"""',
            {"json": json, "ORDER_ID": "ORDER-123", "GUIDE": "034000000001"},
        )

        exact = self._run_open_case_javascript(
            javascript,
            [{"cells": ["ORDER-123", "034000000001"], "guides": ["034000000001"]}],
        )
        partial_guide = self._run_open_case_javascript(
            javascript,
            [{"cells": ["ORDER-123", "034000000001-PARTIAL"], "guides": ["034000000001-PARTIAL"]}],
        )
        different_order = self._run_open_case_javascript(
            javascript,
            [{"cells": ["ORDER-999", "034000000001"], "guides": ["034000000001"]}],
        )

        self.assertEqual(exact, {"result": {"ok": True}, "clickCount": 1})
        self.assertEqual(partial_guide, {"result": {"ok": False, "error": "order_guide_identity_mismatch"}, "clickCount": 0})
        self.assertEqual(different_order, {"result": {"ok": False, "error": "order_guide_identity_mismatch"}, "clickCount": 0})

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
            self.assertIn("visibleRows.length !== 1", runner.code)
            self.assertIn("radio.checked === true", runner.code)
            self.assertIn("cellText.includes(ORDER_ID)", runner.code)
            self.assertIn("guideLinks.length === 1", runner.code)
