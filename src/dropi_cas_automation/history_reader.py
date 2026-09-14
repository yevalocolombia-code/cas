from __future__ import annotations

import json
from typing import Any, Protocol


class BrowserRunner(Protocol):
    def execute_json(self, code: str, timeout_seconds: int) -> dict[str, Any]:
        ...


class DropiHistoryReader:
    """Reads one order's visible status history through the normal Dropi UI."""

    def __init__(self, runner: BrowserRunner):
        self.runner = runner

    def read(self, guide: str, *, allow_external_read: bool) -> str:
        if not allow_external_read:
            raise PermissionError("External reads are disabled. Explicit read authorization is required to inspect an order history.")
        result = self.runner.execute_json(self._script(guide), timeout_seconds=150)
        if not result.get("ok") or not result.get("text"):
            raise RuntimeError(str(result.get("error") or "Dropi did not expose the requested order history."))
        return str(result["text"])

    @staticmethod
    def _script(guide: str) -> str:
        return r'''
import json, time
GUIDE = __GUIDE__
orders_url = 'https://app.dropi.co/dashboard/orders'
tabs = list_tabs(include_chrome=False)
target = next((tab for tab in tabs if '/dashboard/orders' in (tab.get('url') or '')), None)
if not target:
    target = next((tab for tab in tabs if 'app.dropi.co' in (tab.get('url') or '')), None)
if target:
    switch_tab(target)
else:
    new_tab(orders_url)
goto_url(orders_url)
wait_for_load(25)
time.sleep(3)
if '/dashboard/orders' not in (page_info().get('url') or ''):
    print('__JSON__' + json.dumps({'ok':False, 'error':'orders_view_not_loaded'})); raise SystemExit
if not js("!!document.querySelector('textarea')"):
    js("document.querySelector('button[title=\"Mostrar Filtros\"]')?.click()")
    time.sleep(1)
js("document.querySelector('#radio_shipping_guide')?.click()")
js(f"""(() => {{
  const field = document.querySelector('textarea');
  if (!field) return false;
  const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set;
  setter.call(field, {json.dumps(GUIDE)});
  field.dispatchEvent(new Event('input', {{bubbles:true}}));
  field.dispatchEvent(new Event('change', {{bubbles:true}}));
  return true;
}})()""")
button = js("""(() => [...document.querySelectorAll('button')].find(b => (b.innerText || '').trim() === 'Ok')?.click() || false)()""")
time.sleep(4); wait_for_load()
opened = js(f"""(() => {{
  const row = [...document.querySelectorAll('table tbody tr')].find(item => item.innerText.includes({json.dumps(GUIDE)}));
  const link = row?.querySelector('a[title="Información de la Orden"]');
  if (!link) return false;
  link.scrollIntoView({{block:'center'}}); link.click(); return true;
}})()""")
if not opened:
    print('__JSON__' + json.dumps({'ok':False, 'error':'order_not_found'})); raise SystemExit
for _ in range(20):
    time.sleep(.5); wait_for_load()
    text = js("document.body.innerText || ''")
    if 'ORDEN PARA:' in text and 'Historial de estados' in text:
        print('__JSON__' + json.dumps({'ok':True, 'text':text}, ensure_ascii=False)); raise SystemExit
print('__JSON__' + json.dumps({'ok':False, 'error':'order_detail_not_loaded'}))
'''.replace("__GUIDE__", json.dumps(guide))
