from __future__ import annotations

# load environment variables FIRST, before any LiveKit imports
# This ensures LIVEKIT_URL is available when the CLI framework initializes
from dotenv import load_dotenv
load_dotenv(dotenv_path=".env.local")

import asyncio
import logging
import json
import os
import time
import threading
import urllib.request
import urllib.error
from datetime import date, datetime, timedelta
from typing import Any, Optional
from uuid import UUID

# ---------------------------------------------------------------------------
# SAP Ariba Supplier Data API (EU, realm=tatachem-T).
# Token from OAuth; Application Key sent as apiKey header (ARIBA_API_KEY in env). Refresh token when expired.
# See cURL examples: vendorDataRequests, workspaces, questionnaires, qna, answers.
# ---------------------------------------------------------------------------
# OAuth token (POST); Ariba may expect grant_type=openapi_2lo and Basic auth
FORM_AUTH_API_URL = "https://api-eu.ariba.com/v2/oauth/token"

# EU runtime, Supplier Data Pagination v4 — workspace ID is fetched from workspaces endpoint
# Host is eu.openapi.ariba.com (path includes /api) — matches Postman/cURL
ARIBA_RUNTIME_URL = "https://eu.openapi.ariba.com"
ARIBA_BASE_URL = f"{ARIBA_RUNTIME_URL}/api/supplierdatapagination/v4/prod"
ARIBA_REALM = "tatachem-T"
ARIBA_VENDOR_ID = "S80228760"
ARIBA_QUESTIONNAIRE_ID = "Doc2944325329"

# Token cache for form API (refreshed when expired)
_form_api_token: Optional[str] = None
_form_api_token_expiry: float = 0.0
_form_api_token_lock = threading.Lock()

# Ariba field mapping: formName (correlationId) -> (itemId, correlationId) for submit payload
_form_field_mapping: dict[str, tuple[str, str]] = {}
# Workspace ID from GET .../vendors/{id}/workspaces (cached after first fetch)
_ariba_workspace_id: Optional[str] = None

from livekit import rtc, api
from livekit.agents import (
    AgentSession,
    Agent,
    JobContext,
    function_tool,
    RunContext,
    get_job_context,
    cli,
    WorkerOptions,
    RoomInputOptions,
)
from livekit.plugins import (
    openai,
    cartesia,
    silero,
    sarvam,
)
from livekit.plugins.turn_detector.multilingual import MultilingualModel
from pathlib import Path

def load_products_from_json() -> list[dict[str, Any]]:
    """Load products from tata_chemicals_products.json"""
    json_path = Path(__file__).parent / "tata_chemicals_products.json"
    with open(json_path, "r") as f:
        content = f.read()
    # File contains multiple JSON objects separated by commas, wrap in array
    return json.loads(f"[{content}]")

def get_product_from_json(model_name: str) -> Optional[dict[str, Any]]:
    """Get product details by model name from JSON file"""
    products = load_products_from_json()
    model_lower = model_name.lower()
    for product in products:
        if product.get("model", "").lower() == model_lower:
            return product
    return None


def load_form_from_json(path: str | Path) -> dict[str, Any]:
    """Load a form definition from a JSON file. Path can be filename (relative to script dir) or full path."""
    json_path = Path(path)
    if not json_path.is_absolute():
        json_path = Path(__file__).parent / json_path
    with open(json_path, "r", encoding="utf-8") as f:
        return json.load(f)


def _fetch_form_api_token_sync() -> tuple[str, int]:
    """
    Call Ariba OAuth token API (Client Credentials, Basic Auth).
    Matches Postman: Grant type = Client Credentials, Client Authentication = Send as Basic Auth header.
    Uses FORM_AUTH_CLIENT_ID and FORM_AUTH_CLIENT_SECRET from env.
    Returns (token, expires_in_seconds).
    """
    import base64
    client_id = os.environ.get("FORM_AUTH_CLIENT_ID", "").strip()
    client_secret = os.environ.get("FORM_AUTH_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        raise ValueError("FORM_AUTH_CLIENT_ID and FORM_AUTH_CLIENT_SECRET must be set for form API auth")
    # Basic Auth: Base64(client_id:client_secret)
    credentials = base64.b64encode(f"{client_id}:{client_secret}".encode("utf-8")).decode("ascii")
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded",
        "Authorization": f"Basic {credentials}",
    }
    body = "grant_type=client_credentials".encode("utf-8")
    req = urllib.request.Request(FORM_AUTH_API_URL, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    token = data.get("access_token") or data.get("token")
    if not token:
        raise ValueError("Auth API response missing access_token/token")
    expires_in = int(data.get("expires_in", 3600))
    if expires_in <= 0:
        expires_in = 3600
    return token, expires_in


def get_form_api_token() -> str:
    """
    Return a valid Bearer token for form API calls. Refreshes from auth API when expired.
    Uses a 60-second buffer before expiry to refresh early.
    """
    global _form_api_token, _form_api_token_expiry
    with _form_api_token_lock:
        now = time.time()
        if _form_api_token and _form_api_token_expiry > now + 60:
            return _form_api_token
        token, expires_in = _fetch_form_api_token_sync()
        _form_api_token = token
        _form_api_token_expiry = now + expires_in - 60  # refresh 60s before expiry
        return token


def _clear_form_api_token() -> None:
    """Clear cached token so next call will refresh."""
    global _form_api_token, _form_api_token_expiry
    with _form_api_token_lock:
        _form_api_token = None
        _form_api_token_expiry = 0.0


def _ariba_headers() -> dict[str, str]:
    """Headers for Ariba Supplier Data API: Bearer token + apiKey (Application Key from Developer Portal)."""
    h: dict[str, str] = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {get_form_api_token()}",
    }
    # Application Key from Ariba Developer Portal — sent as apiKey header (required for Supplier Data API)
    application_key = os.environ.get("ARIBA_API_KEY", "").strip()
    if application_key:
        h["apiKey"] = application_key
    return h


def _ariba_workspaces_url(vendor_id: str) -> str:
    """GET workspace ID: {{runtime_URL}}/supplierdatapagination/v4/prod/vendors/{SM Vendor ID}/workspaces?realm=..."""
    return f"{ARIBA_BASE_URL}/vendors/{vendor_id}/workspaces?realm={ARIBA_REALM}"


def _ariba_qna_url(vendor_id: str, workspace_id: str) -> str:
    
    """GET form Q&A: .../vendors/{id}/workspaces/{workspaceId}/questionnaires/{questionnaireId}/qna?realm=..."""
    return f"{ARIBA_BASE_URL}/vendors/S80228760/workspaces/WS2942837265/questionnaires/Doc2942837275/qna?realm={ARIBA_REALM}"
    return f"{ARIBA_BASE_URL}/vendors/{vendor_id}/workspaces/{workspace_id}/questionnaires/{ARIBA_QUESTIONNAIRE_ID}/qna?realm={ARIBA_REALM}"


def _ariba_answers_url_from_qna(vendor_id: str, workspace_id: str) -> str:
    """POST form answers: same path as qna but /answers instead of /qna (derived from _ariba_qna_url)."""
    return _ariba_qna_url(vendor_id, workspace_id).replace("/qna?", "/answers?")


def get_ariba_workspace_id(vendor_id: str) -> str:
    """
    GET workspace ID from Ariba: .../vendors/{vendor_id}/workspaces?realm=...
    Returns workspaceId (prefer SupplierRegistration type, else first in list).
    Requires: Bearer token (from OAuth) and apiKey header = Application Key (ARIBA_API_KEY in env).
    """
    url = _ariba_workspaces_url(vendor_id)
    headers = _ariba_headers()
    try:
        req = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        if e.code == 401:
            logger.warning("Ariba workspaces 401 Unauthorized: %s. Refreshing token and retrying.", body or e.reason)
            _clear_form_api_token()
            headers = _ariba_headers()
            req = urllib.request.Request(url, headers=headers, method="GET")
            try:
                with urllib.request.urlopen(req, timeout=30) as retry_resp:
                    data = json.loads(retry_resp.read().decode("utf-8"))
            except urllib.error.HTTPError as retry_e:
                retry_body = ""
                try:
                    retry_body = retry_e.read().decode("utf-8", errors="replace")
                except Exception:
                    pass
                logger.error(
                    "Ariba workspaces 401 after token refresh: %s. "
                    "Check ARIBA_API_KEY in .env.local and that your OAuth token has access to this API.",
                    retry_body or retry_e.reason,
                )
                raise ValueError(
                    f"Ariba API 401 Unauthorized: {retry_body or retry_e.reason}. "
                    "Ensure ARIBA_API_KEY (Application Key from Ariba Developer Portal) is set and the token has access to Supplier Data API."
                ) from retry_e
        else:
            logger.error("Ariba workspaces HTTP %s: %s", e.code, body or e.reason)
            raise
    logger.info(f"Ariba workspaces response: {data}")
    workspaces = data if isinstance(data, list) else data.get("workspaces", data.get("data", []))
    if not workspaces:
        raise ValueError("Ariba workspaces response empty or missing workspaces list")
    logger.info(f"Ariba workspaces: {workspaces}")
    for w in workspaces:
        wid = w.get("workspaceId") or w.get("workspaceID") or w.get("id")
        if wid and (w.get("workspaceType") == "SupplierRegistration" or w.get("type") == "SupplierRegistration"):
            return str(wid)
    wid = workspaces[0].get("workspaceId") or workspaces[0].get("workspaceID") or workspaces[0].get("id")
    if not wid:
        raise ValueError("Ariba workspaces response missing workspaceId")
    return str(wid)


def _normalize_ariba_qna_to_form(data: dict[str, Any]) -> dict[str, Any]:
    """
    Normalize Ariba Q&A response to our form schema (title, sections with fields).
    Ariba returns: { "_embedded": { "questionAnswerList": [ { "questionAnswer": { itemId, externalSystemCorrelationId, questionLabel, answer, answerType, ... } } ] } }.
    Populates _form_field_mapping (formName -> (itemId, correlationId)) for submit.
    """
    global _form_field_mapping
    _form_field_mapping = {}
    logger.info("Ariba Q&A response keys: %s", list(data.keys()))
    # Ariba Supplier Data API: _embedded.questionAnswerList[].questionAnswer
    raw_list = data.get("_embedded", {}).get("questionAnswerList", [])
    items: list[dict[str, Any]] = []
    for entry in raw_list:
        qa = entry.get("questionAnswer") if isinstance(entry, dict) else None
        if qa:
            items.append(qa)
    # Fallback for other response shapes
    if not items:
        items = (
            data.get("items")
            or data.get("questionnaireItems")
            or data.get("qna")
            or data.get("questions")
            or (data["data"] if isinstance(data.get("data"), list) else [])
        )
    fields: list[dict[str, Any]] = []
    for i, it in enumerate(items):
        item_id = str(it.get("itemId", it.get("itemID", "")))
        corr_id = str(it.get("externalSystemCorrelationId", it.get("correlationId", it.get("correlationID", ""))))
        if not corr_id:
            corr_id = item_id
        question_text = it.get("questionLabel", it.get("questionText", it.get("question", it.get("label", f"Question {i+1}"))))
        answer_type = it.get("answerType", it.get("type", "ShortText"))
        # answer is a single string in Ariba response; multiValued items may have comma-separated or list elsewhere
        raw_answer = it.get("answer", it.get("answers", it.get("currentAnswers")))
        if isinstance(raw_answer, list):
            current = [str(x) for x in raw_answer]
        elif raw_answer is not None and str(raw_answer).strip():
            current = [str(raw_answer)]
        else:
            current = []
        user_answer = current[0] if current else None
        form_name = corr_id
        _form_field_mapping[form_name] = (item_id, corr_id)
        fields.append({
            "number": str(i + 1),
            "type": "Question",
            "name": question_text,
            "description": None,
            "answerType": answer_type,
            "acceptableValues": "Any Value",
            "allowedValues": it.get("allowedValues"),
            "formName": form_name,
            "required": it.get("required", True),
            "user_answer": user_answer,
            "itemId": item_id,
            "correlationId": corr_id,
        })
    title = data.get("title") or (items[0].get("questionnaireLabel") if items else None) or "Supplier Registration"
    return {
        "title": title,
        "sections": [{"id": "ariba-qna", "number": "1", "name": title, "fields": fields}],
    }


def fetch_form_from_api() -> dict[str, Any]:
    """
    Fetch supplier registration form (Q&A) from Ariba. Resolves workspace ID via GET .../vendors/{id}/workspaces,
    then GET qna. Returns normalized form schema for prompt; populates _form_field_mapping and _ariba_workspace_id.
    """
    global _ariba_workspace_id
    # workspace_id = _ariba_workspace_id or get_ariba_workspace_id(ARIBA_VENDOR_ID)
    # _ariba_workspace_id = workspace_id
    workspace_id = ""
    url = _ariba_qna_url(ARIBA_VENDOR_ID, workspace_id)
    headers = _ariba_headers()
    try:
        req = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 401:
            _clear_form_api_token()
            headers = _ariba_headers()
            req = urllib.request.Request(url, headers=headers, method="GET")
            with urllib.request.urlopen(req, timeout=30) as retry_resp:
                data = json.loads(retry_resp.read().decode("utf-8"))
        else:
            raise
    print(f"Ariba Q&A response: {data}")
    if data.get("sections") and isinstance(data.get("sections"), list):
        return data
    return _normalize_ariba_qna_to_form(data)


def _submit_form_to_fill_api_sync(answers: list[dict[str, Any]]) -> tuple[bool, str]:
    """
    POST form answers to Ariba answers endpoint. Matches cURL:
      POST ${RUNTIME_URL}/supplierdatapagination/v4/prod/vendors/{id}/workspaces/{wid}/questionnaires/{qid}/answers?realm=...
      Headers: Content-Type: application/json, apiKey, Authorization: Bearer ${ACCESS_TOKEN}
      Body: {"answers": [{"itemId": "...", "externalSystemCorrelationId": "...", "multiValueAnswer": ["..."]}]}
    Uses workspace ID from cache or GET .../vendors/{id}/workspaces. itemId/correlationId from QnA or _form_field_mapping.
    """
    global _ariba_workspace_id
    # workspace_id = _ariba_workspace_id or get_ariba_workspace_id(ARIBA_VENDOR_ID)
    # _ariba_workspace_id = workspace_id
    workspace_id = ""
    fill_url = _ariba_answers_url_from_qna(ARIBA_VENDOR_ID, workspace_id)

    ariba_answers: list[dict[str, Any]] = []
    for a in answers:
        form_name = a.get("formName") or a.get("fieldId", "")
        answer_val = a.get("answer", a.get("value", ""))
        if isinstance(answer_val, list):
            multi = [str(x) for x in answer_val]
        else:
            multi = [str(answer_val)] if answer_val is not None and str(answer_val).strip() else []
        item_id = a.get("itemId")
        corr_id = a.get("externalSystemCorrelationId") or a.get("correlationId") or form_name
        if item_id is None and form_name in _form_field_mapping:
            item_id, corr_id = _form_field_mapping[form_name]
        if item_id is None:
            item_id = corr_id or form_name
        ariba_answers.append({
            "itemId": str(item_id),
            "externalSystemCorrelationId": str(corr_id),
            "multiValueAnswer": multi,
        })
    if not ariba_answers:
        return True, "No answers to submit"
    # Match cURL payload: {"answers": [{"itemId", "externalSystemCorrelationId", "multiValueAnswer": [...]}]}
    payload: dict[str, Any] = {"answers": ariba_answers}
    body = json.dumps(payload).encode("utf-8")

    def _do_post() -> None:
        req = urllib.request.Request(fill_url, data=body, headers=_ariba_headers(), method="POST")
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp.read()

    try:
        _do_post()
        return True, "Submitted to Ariba form fill API"
    except urllib.error.HTTPError as e:
        if e.code == 401:
            _clear_form_api_token()
            try:
                _do_post()
                return True, "Submitted to Ariba form fill API"
            except Exception as retry_e:
                return False, str(retry_e)
        return False, str(e)
    except Exception as e:
        return False, str(e)


def load_form_for_prompt(default_json_path: str = "form_supplier_general_info.json") -> dict[str, Any]:
    """
    Load form for the agent prompt: from form API (hardcoded URL) if auth credentials are set, otherwise from JSON file.
    If the API request fails, falls back to the JSON file.
    """
    logger.info("Loading form for prompt from Ariba API")
    if os.environ.get("FORM_AUTH_CLIENT_ID", "").strip() and os.environ.get("FORM_AUTH_CLIENT_SECRET", "").strip():
        try:
            return fetch_form_from_api()
        except Exception as e:
            logger.warning("Form API fetch failed, falling back to JSON file %s: %s", default_json_path, e)
    # return load_form_from_json(default_json_path)


def get_all_form_questions_from_form(form: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten all questions from Supplier General Information form: section fields (including subfields for address),
    then each subsection's fields, in order."""
    questions: list[dict[str, Any]] = []
    for section in form.get("sections", []):
        parent_number = section.get("number", "")
        for field in section.get("fields", []):
            if field.get("subfields"):
                # Extended address or other composite: emit one question per subfield
                parent_name = field.get("name", "")
                parent_form = field.get("formName", "")
                for sub in field["subfields"]:
                    q = {
                        "number": parent_number,
                        "name": f"{parent_name} — {sub.get('name', '')}",
                        "description": sub.get("description"),
                        "required": sub.get("required", False),
                        "allowedValues": sub.get("allowedValues"),
                        "formName": f"{parent_form}.{sub.get('formName', '')}" if parent_form else sub.get("formName", ""),
                        "answerType": sub.get("answerType"),
                    }
                    if "user_answer" in sub:
                        q["user_answer"] = sub["user_answer"]
                    questions.append(q)
            else:
                questions.append(dict(field))
        for subsection in section.get("subsections", []):
            for field in subsection.get("fields", []):
                questions.append(dict(field))
    return questions


def get_form_section_status(form: dict[str, Any], all_questions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Compute per-section status (left / partially filled / complete) from form structure and flat questions list."""
    sections_status: list[dict[str, Any]] = []
    idx = 0
    for section in form.get("sections", []):
        main_name = section.get("name", "Supplier General Information")
        count_main = 0
        for field in section.get("fields", []):
            if field.get("subfields"):
                count_main += len(field["subfields"])
            else:
                count_main += 1
        answered_main = sum(
            1 for i in range(idx, min(idx + count_main, len(all_questions)))
            if all_questions[i].get("user_answer")
        )
        idx += count_main
        if count_main == 0:
            status = "left"
        elif answered_main == 0:
            status = "left"
        elif answered_main < count_main:
            status = "partially filled"
        else:
            status = "complete"
        sections_status.append({
            "name": main_name,
            "status": status,
            "answered": answered_main,
            "total": count_main,
        })
        for subsection in section.get("subsections", []):
            sub_name = subsection.get("title", subsection.get("number", "Section"))
            count_sub = len(subsection.get("fields", []))
            answered_sub = sum(
                1 for i in range(idx, min(idx + count_sub, len(all_questions)))
                if all_questions[i].get("user_answer")
            )
            idx += count_sub
            if count_sub == 0:
                sub_status = "left"
            elif answered_sub == 0:
                sub_status = "left"
            elif answered_sub < count_sub:
                sub_status = "partially filled"
            else:
                sub_status = "complete"
            sections_status.append({
                "name": sub_name,
                "status": sub_status,
                "answered": answered_sub,
                "total": count_sub,
            })
    return sections_status




logger = logging.getLogger("outbound-caller")
logger.setLevel(logging.INFO)

# Topic for transcript data messages on LiveKit (subscribers can filter by this)
TRANSCRIPT_TOPIC = "transcript"


async def _publish_transcript_to_room(room: Any, payload: dict) -> None:
    """Publish a transcript chunk to the LiveKit room as a data message so subscribers can use it."""
    if room is None:
        return
    try:
        local = getattr(room, "local_participant", None)
        if local is None:
            return
        data = json.dumps(payload).encode("utf-8")
        await local.publish_data(data, topic=TRANSCRIPT_TOPIC)
    except Exception as e:
        logger.warning(f"Failed to publish transcript to room: {e}")


async def update_lead_in_db(
    lead_id: str,
    *,
    connected: bool,
    opty_created: bool = False,
    recall_requested: bool = False,
    not_interested: bool = False,
    agent_summary: str | None = None,
    room_id: str | None = None,
    transcript: list[dict] | None = None,
    model: str | None = None,
    variant: str | None = None,
    time_to_buy: str | None = None,
    dealer_code: str | None = None,
) -> dict:
    """
    Update lead status in the database after a call.
    
    Returns a dict with the update result.
    """
    try:
        from sqlalchemy import select, update
        from db import AsyncSessionLocal
        from models import Lead, CallLog
        
        async with AsyncSessionLocal() as session:
            # Get the lead
            result = await session.execute(
                select(Lead).where(Lead.id == UUID(lead_id))
            )
            lead = result.scalar_one_or_none()
            
            if not lead:
                logger.warning(f"Lead {lead_id} not found in database")
                return {"success": False, "error": "Lead not found"}
            
            # Increment attempt count
            lead.attempt_count += 1
            
            # Determine status based on outcome
            if connected:
                if opty_created:
                    lead.status = 1  # Success
                elif recall_requested:
                    lead.status = 0  # Recall
                    lead.redial_date = date.today() + timedelta(days=1)
                    lead.ready_for_call = False
                elif not_interested:
                    lead.status = -1  # Not interested
                # If connected but none of above, status stays as is (could be NULL or previous value)
            # If not connected, status remains unchanged (NULL stays NULL for retry)
            
            # Clear inflight status
            lead.inflight = False
            lead.inflight_room_id = None
            
            # Update call outcome details if provided
            if model:
                lead.model = model
            if variant:
                lead.variant = variant
            if time_to_buy:
                lead.time_to_buy = time_to_buy
            if dealer_code:
                lead.dealer_code = dealer_code
            
            # Update call log if room_id provided
            if room_id:
                logger.info(f"Looking for CallLog with room_id: {room_id}")
                call_log_result = await session.execute(
                    select(CallLog).where(CallLog.room_id == room_id)
                )
                call_log = call_log_result.scalar_one_or_none()
                if call_log:
                    logger.info(f"Found CallLog {call_log.id}, updating with transcript={len(transcript) if transcript else 0} items")
                    call_log.ended_at = datetime.utcnow()
                    call_log.outcome = (
                        "success" if opty_created else
                        "recall" if recall_requested else
                        "not_interested" if not_interested else
                        "connected" if connected else
                        "not_connected"
                    )
                    call_log.agent_summary = agent_summary
                    if transcript:
                        call_log.transcript = transcript
                else:
                    logger.warning(f"CallLog not found for room_id: {room_id}")
            
            await session.commit()
            
            logger.info(
                f"Updated lead {lead_id}: attempt={lead.attempt_count}, "
                f"status={lead.status}, connected={connected}"
            )
            
            return {
                "success": True,
                "lead_id": lead_id,
                "attempt_count": lead.attempt_count,
                "status": lead.status,
            }
            
    except Exception as e:
        logger.error(f"Failed to update lead {lead_id}: {e}")
        return {"success": False, "error": str(e)}


class OutboundCaller(Agent):
    def __init__(
        self,
        *,
        dial_info: dict[str, Any],
        user_details: dict[str, Any],
        lead_id: str | None = None,
        batch_name: str | None = None,
        room_id: str | None = None,
        form: dict[str, Any] | None = None,
    ):
        # Format vendor details for prompt context
        vendor_context = ""
        if user_details:
            vendor_context = f"""
                ## VENDOR DETAILS (from metadata)
                - Vendor Name: {user_details.get('full_name', 'N/A')}
                - Mobile Number: {user_details.get('mobile_number', 'N/A')}
                - Previous Interest History: {user_details.get('opty_history', 'N/A')}
                - Raw Material History: {user_details.get('vahan_history', 'N/A')}
                - Call Transcripts: {user_details.get('call_transcripts', 'N/A')}
                - WhatsApp Content: {user_details.get('whatsapp_content', 'N/A')}
                - Next Best Action: {user_details.get('next_best_action', 'N/A')}
            """
        
        # Add lead_id context if from batch dialer
        lead_context = ""
        if lead_id:
            lead_context = f"""
                ## CALL TRACKING
                - Lead ID: {lead_id}
                - Batch: {batch_name or 'N/A'}
            """
        
        # Use pre-loaded form if provided (avoids blocking event loop during agent init); else load from API/JSON
        if form is None:
            form = load_form_for_prompt("form_supplier_general_info.json")
        sections = form.get("sections") or []
        num_questions = sum(len(s.get("fields") or []) for s in sections)
        logger.info("Form loaded for prompt: %s questions", num_questions)
        all_questions = get_all_form_questions_from_form(form)
        questions_block_lines = []
        for q in all_questions:
            num = q.get("number", "")
            name = q.get("name", "")
            desc = q.get("description")
            allowed = q.get("allowedValues")
            req = " (required)" if q.get("required") else ""
            user_answer = q.get("user_answer") if q.get("user_answer") else "Not answered yet"
            line = f"- [{num}] {name}{req} — User answer: {user_answer}"
            if desc:
                line += f" — {desc}"
            if allowed:
                line += f" — Options: {', '.join(str(v) for v in allowed)}"
            questions_block_lines.append(line)
        questions_block = "\n                ".join(questions_block_lines) if questions_block_lines else "(No questions loaded)"
        # Supplier name from form (16.1 Supplier Name 1) for verification question
        supplier_name = ""
        for q in all_questions:
            if q.get("formName") == "supplierName1" and q.get("user_answer"):
                supplier_name = (q.get("user_answer") or "").strip()
                break
        if not supplier_name:
            supplier_name = "the registered company"
        logger.info("Form questions loaded for prompt: %d questions", len(all_questions))
        logger.info("Form questions: %s", questions_block)
        super().__init__(
            instructions=f"""
                ## IDENTITY
                You are Tata Chemicals ki **AI agent** — an artificial intelligence assistant. You must make it clear to the user that they are speaking with an AI, not a human. Voice-only; you help vendors fill the supplier onboarding form.
                **LANGUAGE**: Speak in **Hinglish** (mix of Hindi and English). Keep it natural and conversational.
                Ensure to use the devnagri script for hindi words and roman for english words.
                Even convert the output of tool calls to devnagri script.
                You must not put any symbols or numbers in the output.
                You must not put any emojis in the output.
                You must not put any special characters in the output.
                You must not put any html tags in the output.
                You must not put any markdown tags in the output.
                You must not put any xml tags in the output.
                You must not put any json tags in the output.
                You must not put any yaml tags in the output.
                You must not put any csv tags in the output.
                
                ## OBJECTIVE
                Help the vendor fill the **Supplier General Information** form (main section plus Bank Information, Tax Information, Additional Information, Supporting Documents). Go through each question one by one in the order listed, collect answers in Hinglish, and be professional and helpful.
                
                
                ## OPENING (say this first after confirming you are speaking with the right person)
                "Namaste, main Tata Chemicals ki AI agent hoon. Mai yaha aapke supplier onboarding form ko bharne mein sahayta karne ke liye hu."
                **Always ask:** "Kya main {supplier_name} ki taraf se kisi representative se baat kar rahi hoon?" / "Are you speaking from {supplier_name}?" — wait for yes/no before continuing.
                "Mai ye dekh paa rahi hu ki kuch sawalon ke jawab fill ho chuke hai, kya aap bache hue sawalo ko complete karne mein meri madat kar sakte ho?"
                If they say yes/haaan, then move to the questions below. Do not mention any section name when asking the questions.
                
                ## FORM QUESTIONS & ANSWERS
                {questions_block}
                
                ## CALL FLOW
                - Start with the opening line above.
                - If they say yes/haaan, then move to below steps.
                - Mention the section names which are left to be answered and ask them which one they want to answer first.
                - If they choose a section, ask the questions in that section one by one.
                - If they say kuch bhi/koi bhi chalega, begin with the first question in that section which is not answered yet.
                - If they have answered some questions, ask the ones that are not answered yet.
                - One question at a time. After you get an answer, acknowledge briefly and ask the next question.
                - If a question has allowed values (Options): when the list is long, mention only 3–4 options when asking; if the user specifically asks for all options (e.g. "sab batao", "poori list"), then mention all. For short lists, you may mention all. They can answer in their own words if it matches.
                - For optional (non-required) questions, they can say "skip" or "nahi chahiye"; then move to the next.
                - Keep responses crisp (max 30 words when possible).
                - When all questions are done, say: "Form complete ho gaya. Dhanyavaad aapke time ke liye. Aapka din accha ho!" then call end_call.
                
                ## RESTRICTIONS
                - No CRM/system references
                - One question at a time
                - Do not collect phone/email beyond what is in the form (e.g. Primary Contact Mobile, Email are in the list)
                
                ## END CALL SEQUENCE
                1. SAY: "Dhanyavaad aapke time ke liye. Aapka din accha ho!"
                2. Call end_call
                
                ## ANSWERING MACHINE DETECTION
                Signs: automated greetings, "leave a message", beep tones
                Action: Call detected_answering_machine (outcome is recorded automatically).
                
                ## TOOL CALL BEHAVIOR
                Do NOT say any waiting phrase before end_call or submit_form_answers - call them silently.
                When calling submit_form_answers send the user answers in English, if they are not in English, convert them to English.
                
                ## KEY RULES
                - Be professional, conversational and helpful, not robotic
                - Focus on filling the full Supplier General Information form; use the question list above as the single source of questions (all sections and subsections in order)
                - ALWAYS AND ONLY call submit_form_answers with the collected Q&A JSON before end_call (so answers are published to the room)
            """
        )
        # keep reference to the participant for transfers
        self.participant: rtc.RemoteParticipant | None = None

        self.dial_info = dial_info
        self.user_details = user_details
        
        # Batch dialer tracking
        self.lead_id = lead_id
        self.batch_name = batch_name
        self.room_id = room_id
        self.call_outcome_written = False
        
        # Transcript capture
        self.transcript: list[dict] = []
        # Room reference for publishing transcript data messages (set in entrypoint)
        self.room: Any = None

    def set_participant(self, participant: rtc.RemoteParticipant):
        self.participant = participant

    async def hangup(self):
        """Helper function to hang up the call by deleting the room"""

        job_ctx = get_job_context()
        await job_ctx.api.room.delete_room(
            api.DeleteRoomRequest(
                room=job_ctx.room.name,
            )
        )


    @function_tool()
    async def detected_answering_machine(self, ctx: RunContext):
        """CRITICAL: Call this tool IMMEDIATELY when you detect an answering machine or voicemail greeting.
        
        Detection signs:
        - Automated greeting messages
        - Phrases like "answering machine", "voicemail", "voice mail", "आंसरिंग मशीन", "वॉइस मेल"
        - Pre-recorded messages asking to leave a message
        - Beep tones
        - Any robotic/pre-recorded voice
        
        Call this tool IMMEDIATELY when ANY of these are detected, BEFORE continuing conversation.
        This will hang up the call automatically."""
        participant_identity = self.participant.identity if self.participant else "unknown"
        logger.info(f"detected answering machine for {participant_identity}")
        
        # Ensure outcome is recorded before hanging up
        if not self.call_outcome_written and self.lead_id:
            await update_lead_in_db(
                lead_id=self.lead_id,
                connected=False,
                agent_summary="Answering machine/voicemail detected",
                room_id=self.room_id,
                transcript=self.transcript if self.transcript else None,
            )
            self.call_outcome_written = True
        
        await self.hangup()
    
    @function_tool()
    async def end_call(self, ctx: RunContext):
        """End the call. STRICT REQUIREMENTS:
        
        1. You MUST have already SPOKEN "Dhanyavaad aapke time ke liye. Aapka din accha ho!" OUT LOUD
        
        DO NOT call this tool without speaking the greeting first.
        The greeting must be spoken to the user, not just thought about."""
        participant_identity = self.participant.identity if self.participant else "unknown"
        logger.info(f"ending the call for {participant_identity}")
        
        # Enforce that outcome was recorded
        if not self.call_outcome_written and self.lead_id:
            logger.warning(
                f"end_call for lead {self.lead_id}: recording outcome (none recorded yet)."
            )
            await update_lead_in_db(
                lead_id=self.lead_id,
                connected=True,  # Assume connected since we're ending normally
                agent_summary="Call ended without explicit outcome recording",
                room_id=self.room_id,
                transcript=self.transcript if self.transcript else None,
            )
            self.call_outcome_written = True
        
        await self.hangup()

    @function_tool()
    async def submit_form_answers(self, ctx: RunContext, form_answers_json: str) -> dict:
        """
        MANDATORY: Call this at the end of the call (when form is complete or when ending) to submit
        all collected question-answer pairs in JSON format. This publishes the form answers to the
        room so subscribers can consume them.

        Args:
            form_answers_json: A JSON string: array of objects, each with:
                - number: question number (e.g. "2", "16.1")
                - name: question label (e.g. "Region", "Supplier Name")
                - fieldId: form field name (e.g. "region", "supplierName")
                - answer: the vendor's answer (English string)

                Example: [{"number": "2", "name": "Region", "formName": "region", "answer": "Mithapur"}, {"number": "16.1", "name": "Supplier Name", "formName": "supplierName", "answer": "ABC Corp"}]

        Returns:
            Status dict with success and message.
        """
        try:
            answers = json.loads(form_answers_json)
            if not isinstance(answers, list):
                answers = [answers]
            payload = {
                "event": "form_answers",
                "role": "agent",
                "form_answers": answers,
                "timestamp": datetime.utcnow().isoformat(),
            }
            if self.room:
                await _publish_transcript_to_room(self.room, payload)
            logger.info("Published form_answers to room: %d Q&A pairs", len(answers))

            # Submit to Ariba form fill API (uses workspace ID from workspaces endpoint)
            fill_success, fill_message = await asyncio.to_thread(_submit_form_to_fill_api_sync, answers)
            if not fill_success:
                logger.warning("Form fill API failed: %s", fill_message)
            else:
                logger.info("Form fill API: %s", fill_message)

            msg = f"Submitted {len(answers)} form answers to room"
            if fill_success and "skipped" not in fill_message.lower():
                msg += f"; {fill_message}"
            elif not fill_success:
                msg += f"; fill API error: {fill_message}"
            return {"success": True, "message": msg, "count": len(answers)}
        except json.JSONDecodeError as e:
            logger.warning("submit_form_answers invalid JSON: %s", e)
            return {"success": False, "error": f"Invalid JSON: {e}"}
        except Exception as e:
            logger.warning("submit_form_answers failed: %s", e)
            return {"success": False, "error": str(e)}

    @function_tool
    async def get_product_details(self, model_name: str) -> Optional[dict[str, Any]]:
        """Get the raw material details and requirements from the product object
        Args:
            model_name: the name of the raw material to get details for
        Returns:
            the raw material details and requirements
        """
        product = get_product_from_json(model_name)
        return product
    
async def entrypoint(ctx: JobContext):
    logger.info(f"connecting to room {ctx.room.name}")
    await ctx.connect()
    
    # Initialize batch dialer fields
    lead_id = None
    batch_name = None
    room_id = ctx.room.name
    
    # Initialize trunk_id - will be read from metadata
    outbound_trunk_id = None
    
    if not ctx.job.metadata:
        logger.error("No metadata found in the job")
        user_details = {
            "full_name": "Tanmoy Sarkar",
            "mobile_number": "9967768395",
            "opty_history": "[{\"optyCreationDate\":\"2025-01-04\",\"carModel\":\"Punch\",\"salesStage\":\"07 Closed Lost\",\"city\":\"MUMBAI\",\"dealerName\":\"Puneet Cars, Worli - Tata Motors Service Center\",\"source\":\"Referral\",\"testDriveDate\":\"2025-01-05\"},{\"optyCreationDate\":\"2025-01-02\",\"carModel\":\"Nexon\",\"salesStage\":\"07 Closed Lost\",\"city\":\"MUMBAI\",\"dealerName\":\"Puneet Cars, Prabhadevi - Tata Motors Car Showroom\",\"source\":\"Events\"},{\"optyCreationDate\":\"2025-12-15\",\"carModel\":\"Nexon\",\"salesStage\":\"07 Closed Lost\",\"city\":\"MUMBAI\",\"dealerName\":\"Regent, Bandra - Tata Motors Car Showroom\",\"source\":\"External Leads-Web\"},{\"optyCreationDate\":\"2025-11-20\",\"carModel\":\"Tigor\",\"salesStage\":\"07 Closed Lost\",\"city\":\"MUMBAI\",\"dealerName\":\"Puneet Cars, Worli - Tata Motors Service Center\",\"source\":\"External Leads-Web\",\"testDriveDate\":\"2025-11-25\"},{\"optyCreationDate\":\"2025-10-10\",\"carModel\":\"Altroz\",\"salesStage\":\"07 Closed Lost\",\"city\":\"MUMBAI\",\"dealerName\":\"Regent, Bandra - Tata Motors Car Showroom\",\"source\":\"External Leads-Web\",\"testDriveDate\":\"2025-10-15\"}]",
            "vahan_history": "[{\"carModel\":\"ALTO K10 VXI CNG\",\"manufacturer\":\"MARUTI SUZUKI INDIA LTD\",\"registrationDate\":\"2023-09-10\",\"rtoLocation\":\"MUMBAI\",\"rtoState\":\"Maharashtra\",\"seatCapacity\":4,\"cubicCapacity\":998}]",
            "call_transcripts": "[\"Customer called to inquire about Punch Adventure model. Interested in safety features and ADAS. Budget around 10-12 lakhs.\",\"Follow-up call: Customer mentioned comparing with Hyundai Venue. Emphasized Nexon's 5-star safety rating.\"]",
            "whatsapp_content": "[\"Thank you for contacting Chalo Apni Rides! Please let us know how we can help you.\",\"Hi, I am interested in the Nexon model. Can you share the price?\",\"What are the finance options available?\"]",
            "next_best_action": "Customer Interest: Safety conscious; Modern features biased | Relevant Features: Level 2+ ADAS suite; Voice-assisted panoramic sunroof; Advanced infotainment | Recommended Models: Nexon Fearless+ PS (DCA), Harrier Accomplished Ultra | Suggestions: Highlight Nexon's 5-star safety ratings and ADAS; Emphasize Harrier's Samsung Neo QLED connectivity"
        }

        dial_info = {"phone_number": "+919806953395", "full_name": "John Doe"}
        # Fallback to env var for test/manual calls without metadata
        outbound_trunk_id = os.getenv("SIP_OUTBOUND_TRUNK_ID")
    else:
        logger.info("Metadata found in the job")
        logger.info(ctx.job.metadata)
        metadata = json.loads(ctx.job.metadata)
        
        # Get trunk_id from metadata (set by celery task)
        outbound_trunk_id = metadata.get("sip_trunk_id")
        
        # Extract batch dialer fields if present
        lead_id = metadata.get("lead_id")
        batch_name = metadata.get("batch_name")
        
        if lead_id:
            logger.info(f"Batch dialer call: lead_id={lead_id}, batch={batch_name}")
        
        # Extract user details from metadata
        # Support both direct fields and nested lead_metadata from batch dialer
        lead_metadata = metadata.get("lead_metadata", {}) or {}
        
        user_details = {
            "full_name": metadata.get("full_name") or metadata.get("name", ""),
            "mobile_number": metadata.get("mobile_number") or metadata.get("phone", ""),
            "opty_history": lead_metadata.get("opty_history", metadata.get("opty_history", "")),
            "vahan_history": lead_metadata.get("vahan_history", metadata.get("vahan_history", "")),
            "call_transcripts": lead_metadata.get("call_transcripts", metadata.get("call_transcripts", "")),
            "whatsapp_content": lead_metadata.get("whatsapp_content", metadata.get("whatsapp_content", "")),
            "next_best_action": lead_metadata.get("next_best_action", metadata.get("next_best_action", ""))
        }
        
        # Extract dial info (phone_number and full_name for dialing)
        dial_info = {
            "phone_number": metadata.get("phone") or metadata.get("mobile_number", ""),
            "full_name": metadata.get("name") or metadata.get("full_name", "")
        }

    # when dispatching the agent, we'll pass it the approriate info to dial the user
    # dial_info is a dict with the following keys:
    # - phone_number: the phone number to dial
    # - transfer_to: the phone number to transfer the call to when requested
    phone_number = (dial_info.get("phone_number") or "").strip()
    participant_identity = (dial_info.get("full_name") or "unknown").strip() or "unknown"
    full_name = participant_identity
    logger.info(f"full_name: {full_name}")
    logger.info(f"user_details: {user_details}")

    if not phone_number:
        logger.error(
            "Missing SIP callee number: metadata must include 'phone' or 'mobile_number'. "
            "When dispatching the agent, pass metadata with phone/mobile_number set."
        )
        if lead_id:
            asyncio.create_task(update_lead_in_db(
                lead_id=lead_id,
                connected=False,
                agent_summary="Agent error: missing phone number in metadata",
                room_id=room_id,
            ))
        ctx.shutdown()
        return

    # Load form in a thread so Ariba API fetch does not block the event loop; when the
    # participant joins, the loop can process participant_connected and link audio immediately.
    form = await asyncio.to_thread(load_form_for_prompt, "form_supplier_general_info.json")

    # look up the user's phone number and appointment details
    agent = OutboundCaller(
        dial_info=dial_info,
        user_details=user_details,
        lead_id=lead_id,
        batch_name=batch_name,
        room_id=room_id,
        form=form,
    )
    agent.room = ctx.room  # for publishing transcript to LiveKit

    # the following uses GPT-4o, Deepgram and Cartesia
    # VAD is required for responsive barge-in: it detects "user started speaking" from audio
    # so the framework can stop the agent (allow_interruptions=True). Without VAD, interruption
    # handling would rely only on STT and be slower.
    session = AgentSession(
        # turn_detection=MultilingualModel(),  # Temporarily disabled - requires model download
        # Use VAD-based turn detection (VAD + allow_interruptions = barge-in)
        vad=silero.VAD.load(
            activation_threshold=0.9,      # Lower = more sensitive (default: 0.5)
            min_speech_duration=0.3,       # Min speech duration to trigger (default: 0.05s). Keep low so short answers (e.g. "yes") are detected.
            min_silence_duration=2.0,      # Silence before ending speech (default: 0.55s). Higher so user can pause mid-answer (e.g. phone number) without agent cutting in.
        ),
        stt=sarvam.STT(model = 'saarika:v2.5', language='hi-IN'),
        # stt=deepgram.STT(language='en'),
        # you can also use OpenAI's TTS with openai.TTS()
        tts=cartesia.TTS(model ='sonic-2',voice='95d51f79-c397-46f9-b49a-23763d3eaa2d',language='hi'),
        # tts=cartesia.TTS(language='en'),
        # llm=aws.LLM(model="anthropic.claude-3-haiku-20240307-v1:0"),
        llm=openai.LLM(model="gpt-5.2"),
        # you can also use a speech-to-speech model like OpenAI's Realtime API
        # llm=openai.realtime.RealtimeModel()
        allow_interruptions=False,  # Agent stops when user speaks (uses VAD to detect user speech)
        # Wait time before agent starts speaking (after user stops):
        # min_endpointing_delay=0.5,   # Seconds to wait before considering user turn complete (default 0.5). Lower = agent responds sooner.
        # max_endpointing_delay=3.0,  # Max wait when turn detector thinks user might continue (default 3.0). Only used with turn_detection model.
    )

    # Register cleanup handler for when session ends (handles unexpected disconnects)
    @session.on("close")
    def on_session_close():
        logger.info(f"Session closed for room {room_id}")
        if lead_id and not agent.call_outcome_written:
            logger.warning(f"Session ended without outcome recorded for lead {lead_id}. Recording as not_connected.")
            agent.call_outcome_written = True
            asyncio.create_task(update_lead_in_db(
                lead_id=lead_id,
                connected=False,
                agent_summary="Session ended unexpectedly - no outcome recorded",
                room_id=room_id,
                transcript=agent.transcript if agent.transcript else None,
            ))

    # Capture user transcripts as they arrive (final only)
    @session.on("user_input_transcribed")
    def on_user_input_transcribed(event):
        """Capture finalized user speech and publish to LiveKit room."""
        try:
            if event.is_final and event.transcript:
                chunk = {
                    "event": "transcript",
                    "role": "user",
                    "text": event.transcript,
                    "timestamp": datetime.utcnow().isoformat(),
                    "room_id": room_id,
                }
                agent.transcript.append({
                    "role": "user",
                    "text": event.transcript,
                    "timestamp": chunk["timestamp"],
                })
                logger.info(f"Transcript [user]: {event.transcript[:100]}...")
                if agent.room:
                    asyncio.create_task(_publish_transcript_to_room(agent.room, chunk))
        except Exception as e:
            logger.warning(f"Failed to capture user transcript: {e}")

    # Capture agent responses as conversation items are added
    @session.on("conversation_item_added")
    def on_conversation_item_added(event):
        """Capture agent responses for transcript."""
        try:
            item = event.item
            role = getattr(item, "role", "unknown")
            
            # Skip user items - we capture those via user_input_transcribed
            if role == "user":
                return
            
            # Use text_content property if available, otherwise build from content list
            content = ""
            if hasattr(item, "text_content") and item.text_content:
                content = item.text_content
            elif hasattr(item, "content"):
                # Build content from content list
                parts = []
                for part in item.content:
                    if isinstance(part, str):
                        parts.append(part)
                    elif hasattr(part, "transcript") and part.transcript:
                        # AudioContent with transcript
                        parts.append(part.transcript)
                    elif hasattr(part, "text"):
                        parts.append(part.text)
                content = " ".join(parts)
            
            if content:  # Only add non-empty entries
                ts = datetime.utcnow().isoformat()
                agent.transcript.append({
                    "role": role,
                    "text": content,
                    "timestamp": ts,
                })
                logger.info(f"Transcript [{role}]: {content[:100]}...")
                chunk = {
                    "event": "transcript",
                    "role": role,
                    "text": content,
                    "timestamp": ts,
                    "room_id": room_id,
                }
                if agent.room:
                    asyncio.create_task(_publish_transcript_to_room(agent.room, chunk))
        except Exception as e:
            logger.warning(f"Failed to capture transcript item: {e}")

    # Start the session first before dialing so the agent does not miss anything the user says.
    # Pass participant_identity so RoomIO explicitly waits for and links to the SIP participant's
    # audio track; otherwise we rely on "first participant" and can miss linking if timing is off.
    session_started = asyncio.create_task(
        session.start(
            agent=agent,
            room=ctx.room,
            room_input_options=RoomInputOptions(
                participant_identity=participant_identity,
                # Give more time for pre-connect audio so we don't drop early user speech
                pre_connect_audio_timeout=10.0,
                # noise_cancellation=noise_cancellation.BVCTelephony(),
            ),
        )
    )

    # `create_sip_participant` starts dialing the user
    if not outbound_trunk_id:
        logger.error("Cannot create SIP participant: SIP_OUTBOUND_TRUNK_ID is not set")
        ctx.shutdown()
        return
    
    logger.info(f"Initiating SIP call to {phone_number} using trunk {outbound_trunk_id}")
    try:
        await ctx.api.sip.create_sip_participant(
            api.CreateSIPParticipantRequest(
                room_name=ctx.room.name,
                sip_trunk_id=outbound_trunk_id,
                sip_call_to=phone_number,
                participant_identity=participant_identity,
                # function blocks until user answers the call, or if the call fails
                wait_until_answered=True,
            )
        )

        # wait for the agent session start and participant join
        await session_started
        participant = await ctx.wait_for_participant(identity=participant_identity)
        await session.generate_reply(
            instructions=f"Greet in Hinglish: Namaste, main Tata Chemicals ki AI agent hoon. Mai yaha aapke supplier onboarding form ko bharne mein sahayta karne ke liye hu.",
            allow_interruptions=False
        )
        logger.info(f"participant joined: {participant.identity}")

        agent.set_participant(participant)
        
        # Start 3-minute call timeout
        # async def call_timeout():
        #     await asyncio.sleep(150)  # 2.5 minutes
        #     if not agent.call_outcome_written:
        #         logger.info(f"Call timeout reached (3 min) for lead {lead_id}, ending call")
        #         if lead_id:
        #             await update_lead_in_db(
        #                 lead_id=lead_id,
        #                 connected=True,
        #                 recall_requested=True,  # Schedule callback for tomorrow, prevent immediate redial
        #                 agent_summary="Call ended due to 3-minute timeout",
        #                 room_id=room_id,
        #                 transcript=agent.transcript if agent.transcript else None,
        #             )
        #         agent.call_outcome_written = True
        #         await agent.hangup()
        
        # asyncio.create_task(call_timeout())

    except api.TwirpError as e:
        sip_code = e.metadata.get("sip_status_code") or ""
        sip_status = e.metadata.get("sip_status", "unknown")
        logger.error(
            f"error creating SIP participant: {e.message}, "
            f"SIP status: {sip_code} {sip_status}"
        )
        # 486 User Busy (or similar) can arrive after the callee already answered and joined.
        # If we already have a participant in the room, continue and speak so the agent is heard.
        if sip_code == "486" or "busy" in (sip_status or "").lower():
            try:
                # await session_started
                # participant = await asyncio.wait_for(
                #     ctx.wait_for_participant(identity=participant_identity),
                #     timeout=5.0,
                # )
                # logger.info(f"Participant joined despite 486; sending greeting. participant={participant.identity}")
                # await session.generate_reply(
                #     instructions=f"Greet in Hinglish: Mai Nikita bol rahi hu Tata Chemicals se. Kya main {full_name} ji se baat kar rahi hu? Mai yaha aapke supplier onboarding form ko bharne mein sahayta karne ke liye hu — chaliye section 16 se shuru karte hain.",
                #     allow_interruptions=False,
                # )
                # agent.set_participant(participant)
                # # Start call timeout as in the success path
                # async def call_timeout():
                #     await asyncio.sleep(150)
                #     if not agent.call_outcome_written:
                #         if lead_id:
                #             await update_lead_in_db(
                #                 lead_id=lead_id,
                #                 connected=True,
                #                 recall_requested=True,
                #                 agent_summary="Call ended due to 3-minute timeout",
                #                 room_id=room_id,
                #                 transcript=agent.transcript if agent.transcript else None,
                #             )
                #         agent.call_outcome_written = True
                #         await agent.hangup()
                # asyncio.create_task(call_timeout())
                return
            except (asyncio.TimeoutError, Exception) as fallback_err:
                logger.warning(f"Could not recover after 486: {fallback_err}")
        # No participant or not 486: treat as failed SIP and shutdown
        if lead_id and not agent.call_outcome_written:
            await update_lead_in_db(
                lead_id=lead_id,
                connected=False,
                agent_summary=f"SIP call failed: {sip_status}",
                room_id=room_id,
            )
            agent.call_outcome_written = True
        ctx.shutdown()


if __name__ == "__main__":
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            agent_name="tatachem-v2v-agent-ariba",
        )
    )