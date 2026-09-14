from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


@dataclass(frozen=True)
class CaseCreationResult:
    status: str
    chat_id: str | None
    url: str | None
    raw: dict[str, Any]


class BrowserRunner(Protocol):
    def execute_json(self, code: str, timeout_seconds: int) -> dict[str, Any]:
        ...


class DropiCaseCreator:
    """UI wizard writer. This method is intentionally unavailable by default."""

    def __init__(
        self,
        runner: BrowserRunner,
        *,
        case_service_type_id: str,
        case_ticket_id: str,
        service_type_name: str = "Transportadora",
        ticket_name: str = "Ordenes sin movimiento",
    ):
        if not case_service_type_id:
            raise ValueError("A CAS service type id is required for write binding.")
        if not case_ticket_id:
            raise ValueError("A CAS ticket id is required for write binding.")
        self.runner = runner
        self.case_service_type_id = case_service_type_id
        self.case_ticket_id = case_ticket_id
        self.service_type_name = service_type_name
        self.ticket_name = ticket_name

    def create(self, order_id: str, guide: str, message: str, evidence_path: Path, *, allow_external_writes: bool) -> CaseCreationResult:
        if not allow_external_writes:
            raise PermissionError("External writes are disabled. Explicit write authorization is required to create a case.")
        if not evidence_path.is_file():
            raise FileNotFoundError(f"Evidence does not exist: {evidence_path}")
        result = self.runner.execute_json(
            self._script(
                order_id,
                guide,
                message,
                evidence_path,
                self.case_service_type_id,
                self.case_ticket_id,
                self.service_type_name,
                self.ticket_name,
            ),
            timeout_seconds=300,
        )
        if result.get("status") == "existing_case":
            return CaseCreationResult("existing_case", result.get("chat_id"), result.get("url"), result)
        if not result.get("ok") or result.get("status") != "created":
            raise RuntimeError(str(result.get("error") or "Dropi did not confirm case creation."))
        return CaseCreationResult("created", result.get("chat_id"), result.get("url"), result)

    @staticmethod
    def _script(
        order_id: str,
        guide: str,
        message: str,
        evidence_path: Path,
        case_service_type_id: str,
        case_ticket_id: str,
        service_type_name: str,
        ticket_name: str,
    ) -> str:
        return r'''
import json, re, time
ORDER_ID = __ORDER_ID__
GUIDE = __GUIDE__
MESSAGE = __MESSAGE__
EVIDENCE = __EVIDENCE__
SERVICE_TYPE_ID = __SERVICE_TYPE_ID__
TICKET_ID = __TICKET_ID__
SERVICE_TYPE_NAME = __SERVICE_TYPE_NAME__
TICKET_NAME = __TICKET_NAME__

def body(): return js("document.body.innerText || ''")
def click_unique_exact(text):
    return js(f"""(() => {{
      const normalize = value => String(value ?? '').normalize('NFD').replace(/[\\u0300-\\u036f]/g, '').trim().toUpperCase();
      const target = normalize({json.dumps(text)});
      const visible = element => {{ const r=element.getBoundingClientRect(); return r.width>0 && r.height>0; }};
      const dialogs = [...document.querySelectorAll('[role=dialog],.modal,.dropi-modal')].filter(visible);
      const root = dialogs.length ? dialogs[dialogs.length - 1] : document;
      const nodes = [...root.querySelectorAll('button,a,label,[role=button],div,span')]
        .filter(element => visible(element) && normalize(element.innerText || element.textContent) === target)
        .filter(element => ![...element.children].some(child => visible(child) && normalize(child.innerText || child.textContent) === target));
      const actions = [...new Set(nodes.map(element => element.closest('button,a,label,[role=button]') || element))];
      if (actions.length !== 1) return {{ok:false,count:actions.length}};
      const element = actions[0];
      if (element.matches(':disabled,[aria-disabled="true"]')) return {{ok:false,count:1,disabled:true}};
      element.scrollIntoView({{block:'center'}}); element.click(); return {{ok:true,count:1}};
    }})()""")

binding = js(f"""(async () => {{
  const normalize = value => String(value ?? '').normalize('NFD').replace(/[\\u0300-\\u036f]/g, '').trim().toUpperCase();
  try {{
    const token = JSON.parse(localStorage.getItem('casToken') || 'null');
    if (!token) return {{ok:false,error:'session_missing'}};
    const headers = {{Authorization:'Bearer ' + token}};
    const serviceResponse = await fetch('https://api-v2.dropi.co/cas/api/v1/service-types', {{headers}});
    if (!serviceResponse.ok) return {{ok:false,error:'service_type_http_error'}};
    const servicePayload = await serviceResponse.json();
    if (!servicePayload || !Array.isArray(servicePayload.data)) return {{ok:false,error:'service_type_schema_error'}};
    const services = servicePayload.data.filter(item => String(item?._id ?? '') === {json.dumps(SERVICE_TYPE_ID)} && normalize(item?.name) === normalize({json.dumps(SERVICE_TYPE_NAME)}));
    if (services.length !== 1) return {{ok:false,error:'service_type_binding_mismatch'}};
    const ticketResponse = await fetch('https://api-v2.dropi.co/cas/api/v1/cas-types-tickets?casServiceType=' + encodeURIComponent({json.dumps(SERVICE_TYPE_ID)}), {{headers}});
    if (!ticketResponse.ok) return {{ok:false,error:'ticket_type_http_error'}};
    const ticketPayload = await ticketResponse.json();
    if (!ticketPayload || !Array.isArray(ticketPayload.data)) return {{ok:false,error:'ticket_type_schema_error'}};
    const tickets = ticketPayload.data.filter(item => String(item?._id ?? '') === {json.dumps(TICKET_ID)} && String(item?.casServiceType?._id ?? item?.casServiceType ?? '') === {json.dumps(SERVICE_TYPE_ID)} && normalize(item?.name) === normalize({json.dumps(TICKET_NAME)}));
    return tickets.length === 1 ? {{ok:true}} : {{ok:false,error:'ticket_type_binding_mismatch'}};
  }} catch (error) {{ return {{ok:false,error:'selection_contract_error'}}; }}
}})()""")
if not binding or not binding.get('ok'):
    print('__JSON__'+json.dumps({'ok':False,'error':(binding or {}).get('error','selection_contract_error')})); raise SystemExit
if '/dashboard/orders' not in (page_info().get('url') or ''):
    new_tab('https://app.dropi.co/dashboard/orders'); wait_for_load(25); time.sleep(3)
if not js("!!document.querySelector('textarea')"):
    js("document.querySelector('button[title=\"Mostrar Filtros\"]')?.click()"); time.sleep(1)
js("document.querySelector('#radio_shipping_guide')?.click()")
js(f"""(() => {{ const field=document.querySelector('textarea'); if(!field)return false; const setter=Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value').set; setter.call(field,{json.dumps(GUIDE)}); field.dispatchEvent(new Event('input',{{bubbles:true}})); field.dispatchEvent(new Event('change',{{bubbles:true}})); return true; }})()""")
if not click_unique_exact('Ok').get('ok'):
    print('__JSON__'+json.dumps({'ok':False,'error':'selection_ambiguous','stage':'order_filter'})); raise SystemExit
time.sleep(4); wait_for_load()
opened = js(f"""(() => {{ const row=[...document.querySelectorAll('table tbody tr')].find(item=>item.innerText.includes({json.dumps(GUIDE)})); const link=row?.querySelector('a[title="Nueva consulta"]'); if(!link)return false; link.click(); return true; }})()""")
if not opened:
    print('__JSON__'+json.dumps({'ok':False,'error':'new_case_action_not_found'})); raise SystemExit
time.sleep(1); wait_for_load()
if 'Orden ya tiene un caso' in body():
    print('__JSON__'+json.dumps({'ok':True,'status':'existing_case','url':page_info().get('url') or ''}, ensure_ascii=False)); raise SystemExit
for _ in range(20):
    time.sleep(.5); wait_for_load()
    if 'Transportadora' in body(): break
else:
    print('__JSON__'+json.dumps({'ok':False,'error':'wizard_not_loaded'})); raise SystemExit
if not click_unique_exact(SERVICE_TYPE_NAME).get('ok'):
    print('__JSON__'+json.dumps({'ok':False,'error':'selection_ambiguous','stage':'service_type'})); raise SystemExit
if not click_unique_exact('Siguiente').get('ok'):
    print('__JSON__'+json.dumps({'ok':False,'error':'selection_ambiguous','stage':'service_next'})); raise SystemExit
time.sleep(2); wait_for_load()
if not click_unique_exact(TICKET_NAME).get('ok'):
    print('__JSON__'+json.dumps({'ok':False,'error':'selection_ambiguous','stage':'ticket_type'})); raise SystemExit
if not click_unique_exact('Siguiente').get('ok'):
    print('__JSON__'+json.dumps({'ok':False,'error':'selection_ambiguous','stage':'ticket_next'})); raise SystemExit
time.sleep(2); wait_for_load()
filled = js(f"""(() => {{ const field=document.querySelector('textarea'); if(!field)return false; const setter=Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value').set; setter.call(field,{json.dumps(MESSAGE)}); field.dispatchEvent(new Event('input',{{bubbles:true}})); field.dispatchEvent(new Event('change',{{bubbles:true}})); return true; }})()""")
if not filled:
    print('__JSON__'+json.dumps({'ok':False,'error':'message_field_not_found'})); raise SystemExit
upload_file('input[type="file"]', EVIDENCE); time.sleep(4); wait_for_load()
if not click_unique_exact('Iniciar conversación').get('ok'):
    print('__JSON__'+json.dumps({'ok':False,'error':'selection_ambiguous','stage':'create'})); raise SystemExit
time.sleep(4); wait_for_load()
url = page_info().get('url') or ''
match = re.search(r'cas_chat_id=([^&]+)', url)
if '/dashboard/cas/' not in url:
    print('__JSON__'+json.dumps({'ok':False,'error':'case_not_confirmed','url':url})); raise SystemExit
print('__JSON__'+json.dumps({'ok':True,'status':'created','chat_id':match.group(1) if match else None,'url':url}, ensure_ascii=False))
'''.replace("__ORDER_ID__", json.dumps(order_id)).replace("__GUIDE__", json.dumps(guide)).replace("__MESSAGE__", json.dumps(message)).replace("__EVIDENCE__", json.dumps(str(evidence_path))).replace("__SERVICE_TYPE_ID__", json.dumps(case_service_type_id)).replace("__TICKET_ID__", json.dumps(case_ticket_id)).replace("__SERVICE_TYPE_NAME__", json.dumps(service_type_name)).replace("__TICKET_NAME__", json.dumps(ticket_name))
