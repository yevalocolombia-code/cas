from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol


class BrowserRunner(Protocol):
    def execute_json(self, code: str, timeout_seconds: int) -> dict[str, Any]:
        ...


class ExternalReadDisabled(PermissionError):
    pass


@dataclass(frozen=True)
class SessionDiagnosis:
    url: str
    logged_in: bool
    orders_visible: bool


class DropiSessionAdapter:
    """Read-only session diagnostic adapter.

    It never creates a case, uploads evidence, writes to an external service,
    or reads browser credentials into local application storage.
    """

    def __init__(self, runner: BrowserRunner, *, allow_external_read: bool, orders_url: str = "https://app.dropi.co/dashboard/orders"):
        self._runner = runner
        self._allow_external_read = allow_external_read
        self._orders_url = orders_url

    def diagnose(self) -> SessionDiagnosis:
        if not self._allow_external_read:
            raise ExternalReadDisabled("External browser access is disabled. Re-run with explicit read-only opt-in.")
        code = f'''
import json
import time
orders_url = {json.dumps(self._orders_url)}
tabs = list_tabs(include_chrome=False)
target = next((tab for tab in tabs if 'app.dropi.co' in (tab.get('url') or '')), None)
if target:
    switch_tab(target)
else:
    new_tab(orders_url)
goto_url(orders_url)
wait_for_load(20)
time.sleep(2)

def protected_probe():
    info = page_info()
    url = str(info.get('url') or '')
    text = js("document.body.innerText || ''") or ''
    has_orders_token = bool(js("!!localStorage.getItem('DROPI_token')"))
    orders_visible = '/dashboard/orders' in url and any(marker in text for marker in ('Mis Pedidos', 'Órdenes', 'Mis ordenes'))
    return {{'url': url, 'has_orders_token': has_orders_token, 'orders_visible': orders_visible}}

first = protected_probe()
time.sleep(2)
second = protected_probe()
logged_in = all((
    first['has_orders_token'],
    first['orders_visible'],
    second['has_orders_token'],
    second['orders_visible'],
    '/auth/login' not in second['url'],
))
print('__JSON__' + json.dumps({{
    'ok': True,
    'url': second['url'],
    'logged_in': logged_in,
    'orders_visible': bool(first['orders_visible'] and second['orders_visible']),
}}, ensure_ascii=False))
'''
        payload = self._runner.execute_json(code, timeout_seconds=90)
        if not payload.get("ok"):
            raise RuntimeError(str(payload.get("error") or "Session diagnosis failed."))
        return SessionDiagnosis(
            url=str(payload.get("url") or ""),
            logged_in=bool(payload.get("logged_in")),
            orders_visible=bool(payload.get("orders_visible")),
        )
