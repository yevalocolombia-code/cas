# Dropi CAS Automation

Portable, account-neutral Dropi CAS automation package. It owns its workspace, SQLite database, browser operations, evidence, and reports without depending on another installation.

## What version 0.1.0 does

- Installs in its own Python virtual environment.
- Reads a private TOML configuration file.
- Creates only its configured workspace: SQLite data, evidence, and report directories.
- Imports an official orders XLSX or, when configured, a fresh ecommerce360 MCP order snapshot.
- Reads a guide's visible order history and stores real movements.
- Evaluates local candidates against a configurable no-movement threshold.
- Validates remote eligibility, captures evidence, creates a CAS through the official UI, records it locally, and supports protected follow-up sending.
- Generates local summaries for orders, open cases, and follow-ups.

Local commands remain offline by default. Browser reads require `--allow-external-read`; case creation and follow-up writes require `--allow-external-writes`. Browser tokens remain in the authenticated browser session and are never printed or stored by the package.

## Isolation guarantees

The package does not contain account-specific paths, identifiers, channel names, credentials, browser profiles, historical records, or operational reports.

Its default runtime is relative to the private configuration file. Example: a configuration at `/opt/dropi-cas/config.toml` with `root = "./runtime"` creates data only under `/opt/dropi-cas/runtime/`.

## Install

```bash
cd dropi-cas-automation
./scripts/install.sh
cp config.example.toml config.toml
```

The installer defaults to `.venv` inside this package. To use another location:

```bash
./scripts/install.sh /opt/dropi-cas/.venv
```

## Validate safely

```bash
.venv/bin/dropi-cas doctor --config ./config.toml
.venv/bin/dropi-cas init --config ./config.toml
```

`doctor` is local-only. `init` creates the configured local directories and SQLite database only.

## Evaluate local JSON

Example input:

```json
[
  {
    "order_id": "example-001",
    "guide": "GUIDE-001",
    "carrier": "carrier-name",
    "current_status": "EN TRANSPORTE",
    "last_movement_at": "2026-07-20T10:00:00+00:00"
  }
]
```

Run:

```bash
.venv/bin/dropi-cas evaluate --config ./config.toml --input ./orders.json
```

The command returns JSON decisions and saves an audit record to the package's configured SQLite database.

## Read-only Dropi session diagnostic

Prerequisite: the user must already have an authenticated browser session available to browser-harness.

```bash
.venv/bin/dropi-cas diagnose-dropi --config ./config.toml --allow-external-read
```

Without the opt-in flag, the command stops before starting a browser runner:

```bash
.venv/bin/dropi-cas diagnose-dropi --config ./config.toml
```

The result only includes the page URL and booleans for login/orders visibility. It never prints browser storage, cookies, or tokens.

## Order source: Excel or MCP

Select the input source in the private `config.toml`:

```toml
[orders_source]
# Use "excel" if this installation does not have ecommerce360 MCP access.
provider = "excel"

# Use this alternative only after ecommerce360 MCP was connected in Hermes:
# provider = "mcp"
# mcp_config_path = "~/.hermes/config.yaml"
# mcp_window_days = 25
```

### Excel mode — works for every installation

```toml
[orders_source]
provider = "excel"
```

`sync` uses the authenticated Dropi browser session to request and import the official XLSX. Keep the browser logged in before running it.

### MCP mode — faster fresh snapshot

MCP access is optional. It must be enabled by the installation owner in their private Hermes setup, not copied from another account.

1. Install and configure Hermes on that computer.
2. Connect the owner's authenticated `ecommerce360` MCP server in Hermes.
3. Verify the connection without exposing credentials:

```bash
hermes mcp list
hermes mcp test ecommerce360
```

4. In the portable package's private `config.toml`, select MCP and point only to that local Hermes config:

```toml
[orders_source]
provider = "mcp"
mcp_config_path = "~/.hermes/config.yaml"
mcp_window_days = 25
```

5. Run the normal read-only sync:

```bash
.venv/bin/dropi-cas sync --config ./config.toml --allow-external-read
```

MCP mode reads the current order snapshot, imports it into the package's own SQLite and rejects a truncated range. It does not open the browser for the import and it never exports or prints credentials. The TOML contains only a local path; the MCP token remains in the owner's private Hermes configuration.

Both sources feed the same local SQLite and the same subsequent history/CAS workflow. MCP data is an initial snapshot: actual case execution still refreshes the visible guide history before validation, evidence and creation.

## Operational commands

```bash
# Read-only Dropi operations
.venv/bin/dropi-cas sync --config ./config.toml --allow-external-read
.venv/bin/dropi-cas refresh-history --config ./config.toml --guide GUIDE --allow-external-read
.venv/bin/dropi-cas preflight --config ./config.toml --guide GUIDE \
  --allow-external-read --case-service-type-id "YOUR_SERVICE_TYPE_ID"

# Local-only operations
.venv/bin/dropi-cas candidates --config ./config.toml
.venv/bin/dropi-cas run --config ./config.toml --dry-run
.venv/bin/dropi-cas followups --config ./config.toml --dry-run
.venv/bin/dropi-cas report --config ./config.toml

# Real case creation: exactly one previously reviewed guide.
.venv/bin/dropi-cas run --config ./config.toml --execute --guide GUIDE \
  --allow-external-read --allow-external-writes \
  --case-service-type-id "YOUR_SERVICE_TYPE_ID"

# Real follow-ups: rechecks movement and active-case status before sending.
.venv/bin/dropi-cas followups --config ./config.toml --execute \
  --allow-external-read --allow-external-writes \
  --case-service-type-id "YOUR_SERVICE_TYPE_ID"
```

`run --execute` affects real operations: it refreshes the guide, validates it, captures evidence, creates a case only when Dropi confirms it is not already open, and records confirmed cases locally.

`preflight` is the approval gate for one controlled guide. It refreshes the visible
history, asks Dropi whether an active CAS already exists, and captures local
evidence when the guide remains eligible. It never creates a CAS or sends a
message.

## Persistent browser and read-only watchdog

Production installations can use the checked-in user-service templates in
`deploy/systemd/` to keep an Xvfb display and the authenticated Chrome profile
available after reboot. Copy the units to `~/.config/systemd/user/`, run
`systemctl --user daemon-reload`, and enable both services. The Chrome debugging
endpoint is bound to loopback only.

`scripts/dropi-browser-harness` wraps the audited browser-harness installation
with a dedicated workspace, loopback CDP endpoint, disabled telemetry, disabled
recording, and browser autospawn disabled. Install the wrapper on `PATH` or pass
it explicitly through `--browser-command`.

`scripts/dropi-cas-watchdog.py` runs only the diagnostic, MCP sync, candidate
count, follow-up dry-run, and local report. It uses an exclusive lock, emits no
order/guide identifiers, suppresses unchanged status, and never passes
`--execute` or `--allow-external-writes`. Schedule this script with the local
service manager of choice; case creation and follow-up sending must remain a
separate approval-gated operation.

## Per-installation customization

`config.toml` controls the workspace path and no-movement threshold. CLI flags provide the browser command, batch limit, and account-specific CAS service type. Every installation needs its own authenticated browser session; no credentials belong in `config.toml` or this repository.

## Test

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```
