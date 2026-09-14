from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


@dataclass(frozen=True)
class AppConfig:
    workspace_root: Path
    minimum_hours_without_movement: float = 24.0
    movement_timezone: str = "UTC"
    orders_source: str = "excel"
    mcp_config_path: Path | None = None
    mcp_window_days: int = 25

    @property
    def database_path(self) -> Path:
        return self.workspace_root / "data" / "automation.sqlite3"

    @property
    def evidence_dir(self) -> Path:
        return self.workspace_root / "evidence"

    @property
    def reports_dir(self) -> Path:
        return self.workspace_root / "reports"


def _parse_minimal_toml(text: str) -> dict[str, dict[str, Any]]:
    section = ""
    data: dict[str, dict[str, Any]] = {}
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()
            data.setdefault(section, {})
            continue
        if "=" not in line or not section:
            raise ValueError(f"Unsupported configuration line: {raw_line}")
        key, raw_value = (part.strip() for part in line.split("=", 1))
        if raw_value.startswith('"') and raw_value.endswith('"'):
            value: Any = raw_value[1:-1]
        else:
            value = float(raw_value) if "." in raw_value else int(raw_value)
        data[section][key] = value
    return data


def load_config(path: Path) -> AppConfig:
    path = path.expanduser().resolve()
    data = _parse_minimal_toml(path.read_text(encoding="utf-8"))
    workspace = data.get("workspace", {}).get("root")
    if not isinstance(workspace, str) or not workspace.strip():
        raise ValueError("[workspace].root must be a non-empty string.")
    workspace_root = Path(workspace).expanduser()
    if not workspace_root.is_absolute():
        workspace_root = path.parent / workspace_root
    threshold = data.get("rules", {}).get("minimum_hours_without_movement", 24)
    if not isinstance(threshold, (int, float)) or threshold <= 0:
        raise ValueError("[rules].minimum_hours_without_movement must be greater than zero.")
    movement_timezone = data.get("rules", {}).get("movement_timezone", "UTC")
    if not isinstance(movement_timezone, str) or not movement_timezone.strip():
        raise ValueError("[rules].movement_timezone must be a non-empty IANA timezone name.")
    try:
        ZoneInfo(movement_timezone)
    except ZoneInfoNotFoundError as error:
        raise ValueError("[rules].movement_timezone must be a valid IANA timezone name.") from error
    source = data.get("orders_source", {}).get("provider", "excel")
    if source not in {"excel", "mcp"}:
        raise ValueError("[orders_source].provider must be 'excel' or 'mcp'.")
    raw_mcp_path = data.get("orders_source", {}).get("mcp_config_path", "")
    if raw_mcp_path and not isinstance(raw_mcp_path, str):
        raise ValueError("[orders_source].mcp_config_path must be a string.")
    mcp_path = Path(raw_mcp_path).expanduser() if raw_mcp_path else None
    if mcp_path is not None and not mcp_path.is_absolute():
        mcp_path = path.parent / mcp_path
    window_days = data.get("orders_source", {}).get("mcp_window_days", 25)
    if not isinstance(window_days, int) or window_days <= 0:
        raise ValueError("[orders_source].mcp_window_days must be a positive integer.")
    return AppConfig(
        workspace_root=workspace_root.resolve(),
        minimum_hours_without_movement=float(threshold),
        movement_timezone=movement_timezone,
        orders_source=source,
        mcp_config_path=mcp_path.resolve() if mcp_path else None,
        mcp_window_days=window_days,
    )


def initialize_workspace(config: AppConfig) -> None:
    for directory in (config.workspace_root / "data", config.evidence_dir, config.reports_dir):
        directory.mkdir(parents=True, exist_ok=True)
