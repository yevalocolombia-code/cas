from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class CaseValidationResult:
    status: str
    chat_id: str | None = None
    raw: dict[str, Any] | None = None


class BrowserRunner(Protocol):
    def execute_json(self, code: str, timeout_seconds: int) -> dict[str, Any]:
        ...


class DropiCaseValidator:
    """Read-only remote CAS validation. Browser tokens never leave the tab."""

    def __init__(self, runner: BrowserRunner, *, case_service_type_id: str = ""):
        self.runner = runner
        self.case_service_type_id = case_service_type_id

    def validate(self, order_id: str, carrier: str, *, allow_external_read: bool) -> CaseValidationResult:
        if not allow_external_read:
            raise PermissionError("External reads are disabled. Explicit read authorization is required to validate a remote case.")
        data = self.runner.execute_json(self._script(order_id, carrier), timeout_seconds=120)
        status = str(data.get("status") or "validation_error")
        raw_checks = data.get("checks")
        checks: dict[str, Any] = raw_checks if isinstance(raw_checks, dict) else {}
        complete_checks = all(
            checks.get(name) is True
            for name in ("validation_ok", "search_ok", "search_schema_ok", "identity_ok")
        )
        if status == "eligible" and not complete_checks:
            status = "validation_error"
        if status == "existing_case" and (not complete_checks or not data.get("chat_id")):
            status = "validation_error"
        allowed = {
            "eligible",
            "existing_case",
            "not_eligible",
            "session_missing",
            "service_type_missing",
            "validation_http_error",
            "validation_schema_error",
            "case_search_http_error",
            "case_search_schema_error",
            "ambiguous_case_search_response",
            "invalid_order_id",
            "network_error",
            "validation_error",
        }
        if status not in allowed:
            status = "validation_error"
        return CaseValidationResult(status, data.get("chat_id") if status == "existing_case" else None, data)

    def _script(self, order_id: str, carrier: str) -> str:
        return r'''
import json
ORDER_ID = __ORDER_ID__
CARRIER = __CARRIER__
SERVICE_TYPE = __SERVICE_TYPE__
script = f"""(async () => {{
  const ORDER_ID = {json.dumps(ORDER_ID)};
  const CARRIER = {json.dumps(CARRIER)};
  const SERVICE_TYPE = {json.dumps(SERVICE_TYPE)};
  const checks = {{validation_ok:false, search_ok:false, search_schema_ok:false, identity_ok:false}};
  try {{
    if (!/^\d+$/.test(ORDER_ID)) return {{status:'invalid_order_id', checks}};
    const numericOrderId = Number(ORDER_ID);
    if (!Number.isSafeInteger(numericOrderId) || numericOrderId <= 0) return {{status:'invalid_order_id', checks}};
    const ordersToken = JSON.parse(localStorage.getItem('DROPI_token') || 'null');
    const casToken = JSON.parse(localStorage.getItem('casToken') || 'null');
    if (!ordersToken || !casToken) return {{status:'session_missing', checks}};
    if (!SERVICE_TYPE) return {{status:'service_type_missing', checks}};
    const ordersHeaders = {{Authorization:'Bearer ' + ordersToken, 'X-Authorization':'Bearer ' + ordersToken}};
    const validationResponse = await fetch('https://api.dropi.co/api/orders/validate-any-case-carriers?order_id=' + encodeURIComponent(ORDER_ID), {{headers:ordersHeaders}});
    if (!validationResponse.ok) return {{status:'validation_http_error', checks}};
    const validationPayload = await validationResponse.json();
    if (!validationPayload || typeof validationPayload !== 'object' || !validationPayload.objects || typeof validationPayload.objects.ORDER_WITHOUT_MOVEMENT !== 'boolean') {{
      return {{status:'validation_schema_error', checks}};
    }}
    checks.validation_ok = true;
    if (!validationPayload.objects.ORDER_WITHOUT_MOVEMENT) return {{status:'not_eligible', checks}};
    const headers = {{Authorization:'Bearer ' + casToken, 'Content-Type':'application/json'}};
    const query = {{enterpriseNames:[CARRIER], serviceTypeId:SERVICE_TYPE, referenceObjects:[{{id:numericOrderId, type:'ORDER'}}], status_chat:['active','queues','postponed','to_reopen','close','closed','finalized']}};
    const searchResponse = await fetch('https://api-v2.dropi.co/cas/api/v1/chats/search', {{method:'POST',headers,body:JSON.stringify(query)}});
    if (!searchResponse.ok) return {{status:'case_search_http_error', checks}};
    checks.search_ok = true;
    const searchPayload = await searchResponse.json();
    if (!searchPayload || typeof searchPayload !== 'object' || !Array.isArray(searchPayload.data)) {{
      return {{status:'case_search_schema_error', checks}};
    }}
    checks.search_schema_ok = true;
    if (searchPayload.data.length === 0) {{
      checks.identity_ok = true;
      return {{status:'eligible', checks}};
    }}
    const normalize = value => String(value ?? '').normalize('NFD').replace(/[\u0300-\u036f]/g, '').trim().toUpperCase();
    const expectedOrderId = String(numericOrderId);
    const expectedServiceType = String(SERVICE_TYPE);
    const expectedCarrier = normalize(CARRIER);
    const inspect = item => {{
      const chat = item?.casChat && typeof item.casChat === 'object' ? item.casChat : item;
      const references = Array.isArray(chat?.referenceObjects) ? chat.referenceObjects :
        (Array.isArray(item?.referenceObjects) ? item.referenceObjects : []);
      const orderMatches = references.some(reference =>
        String(reference?.id ?? '') === expectedOrderId && normalize(reference?.type) === 'ORDER'
      );
      const serviceType = chat?.serviceTypeId ?? chat?.serviceType?._id ?? chat?.serviceType?.id ??
        item?.serviceTypeId ?? item?.serviceType?._id ?? item?.serviceType?.id ?? '';
      const carrierValues = [
        chat?.enterpriseName, chat?.enterprise?.name, item?.enterpriseName, item?.enterprise?.name,
        ...(Array.isArray(chat?.enterpriseNames) ? chat.enterpriseNames : []),
        ...(Array.isArray(item?.enterpriseNames) ? item.enterpriseNames : []),
      ];
      const carrierMatches = carrierValues.some(value => normalize(value) === expectedCarrier);
      const identityMatches = orderMatches && String(serviceType) === expectedServiceType && carrierMatches;
      const status = normalize(chat?.status).toLowerCase();
      const chatId = String(chat?._id ?? chat?.id ?? item?.chatId ?? '');
      return {{identityMatches, status, chatId}};
    }};
    const inspected = searchPayload.data.map(inspect);
    if (!inspected.every(result => result.identityMatches)) {{
      return {{status:'ambiguous_case_search_response', checks}};
    }}
    checks.identity_ok = true;
    const active = inspected.find(result => !['close','closed','finalized'].includes(result.status));
    if (active && !active.chatId) return {{status:'ambiguous_case_search_response', checks}};
    return active ? {{status:'existing_case', chat_id:active.chatId, checks}} : {{status:'eligible', checks}};
  }} catch (error) {{
    return {{status:'network_error', checks}};
  }}
}})()"""
print('__JSON__' + json.dumps(js(script), ensure_ascii=False))
'''.replace("__ORDER_ID__", json.dumps(order_id)).replace("__CARRIER__", json.dumps(carrier)).replace("__SERVICE_TYPE__", json.dumps(self.case_service_type_id))
