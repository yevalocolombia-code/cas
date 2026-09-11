import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dropi_cas_automation.mcp_orders import _parse_connection, import_mcp_orders
from dropi_cas_automation.storage import initialize_database

_HERMES_MCP_TEMPLATE = """
mcp_servers:
  ecommerce360:
    url: "https://example.test/api/mcp"
    headers:
      Authorization: "Bearer ${MCP_ECOMMERCE360_API_KEY}"
"""

_HERMES_MCP_LITERAL = """
mcp_servers:
  ecommerce360:
    url: "https://example.test/api/mcp"
    headers:
      Authorization: "Bearer literal-static-token"
"""


class McpOrdersTests(unittest.TestCase):
    def test_imports_mcp_rows_without_losing_a_text_guide(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "automation.sqlite3"
            initialize_database(database)

            result = import_mcp_orders(
                database,
                [
                    {
                        "id": "order-1",
                        "status": "EN TRANSPORTE",
                        "shippingGuide": "034000000001",
                        "carrier": "ENVIA",
                        "lastMovementAt": "2026-07-29T08:00:00",
                    }
                ],
                source_ref="mcp:2026-07-05:2026-07-29",
            )

            with sqlite3.connect(database) as connection:
                row = connection.execute("SELECT guide,status,carrier,last_movement_at FROM orders WHERE order_id='order-1'").fetchone()
                source = connection.execute("SELECT source_path FROM import_runs").fetchone()[0]
            self.assertEqual(result["rows"], 1)
            self.assertEqual(row, ("034000000001", "EN TRANSPORTE", "ENVIA", "2026-07-29T08:00:00"))
            self.assertEqual(source, "mcp:2026-07-05:2026-07-29")

    def test_resolves_hermes_header_env_template_from_process_environment(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.yaml"
            config_path.write_text(_HERMES_MCP_TEMPLATE, encoding="utf-8")
            with patch.dict(os.environ, {"MCP_ECOMMERCE360_API_KEY": "process-env-token"}, clear=False):
                url, headers = _parse_connection(config_path.read_text(encoding="utf-8"), config_path=config_path)

        self.assertEqual(url, "https://example.test/api/mcp")
        self.assertEqual(headers["Authorization"], "Bearer process-env-token")

    def test_resolves_hermes_header_env_template_from_dotenv_beside_config(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_path = root / "config.yaml"
            config_path.write_text(_HERMES_MCP_TEMPLATE, encoding="utf-8")
            (root / ".env").write_text("MCP_ECOMMERCE360_API_KEY=dotenv-token\n", encoding="utf-8")
            cleaned = {key: value for key, value in os.environ.items() if key != "MCP_ECOMMERCE360_API_KEY"}
            with patch.dict(os.environ, cleaned, clear=True):
                url, headers = _parse_connection(config_path.read_text(encoding="utf-8"), config_path=config_path)

        self.assertEqual(url, "https://example.test/api/mcp")
        self.assertEqual(headers["Authorization"], "Bearer dotenv-token")

    def test_keeps_direct_literal_headers_working(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.yaml"
            config_path.write_text(_HERMES_MCP_LITERAL, encoding="utf-8")
            url, headers = _parse_connection(config_path.read_text(encoding="utf-8"), config_path=config_path)

        self.assertEqual(url, "https://example.test/api/mcp")
        self.assertEqual(headers["Authorization"], "Bearer literal-static-token")

    def test_missing_header_env_template_errors_without_exposing_secrets(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.yaml"
            config_path.write_text(_HERMES_MCP_TEMPLATE, encoding="utf-8")
            cleaned = {key: value for key, value in os.environ.items() if key != "MCP_ECOMMERCE360_API_KEY"}
            with patch.dict(os.environ, cleaned, clear=True):
                with self.assertRaises(ValueError) as raised:
                    _parse_connection(config_path.read_text(encoding="utf-8"), config_path=config_path)

        message = str(raised.exception)
        self.assertIn("MCP_ECOMMERCE360_API_KEY", message)
        self.assertNotIn("Bearer", message)


if __name__ == "__main__":
    unittest.main()
