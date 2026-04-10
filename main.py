from __future__ import annotations

# load environment variables FIRST, before any LiveKit imports
# This ensures LIVEKIT_URL is available when the CLI framework initializes
from dotenv import load_dotenv
load_dotenv()  # .env
load_dotenv(dotenv_path=".env.local")  # optional local overrides

import asyncio
import logging
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from typing import Any, Optional
from uuid import UUID

# ---------------------------------------------------------------------------
# SAP Ariba Supplier Data API (EU, realm=tatachem-T).
# Token from OAuth; Application Key sent as apiKey header (ARIBA_API_KEY in env). Refresh token when expired.
# ---------------------------------------------------------------------------
FORM_AUTH_API_URL = "https://api-eu.ariba.com/v2/oauth/token"
ARIBA_RUNTIME_URL = "https://eu.openapi.ariba.com"
ARIBA_BASE_URL = f"{ARIBA_RUNTIME_URL}/api/supplierdatapagination/v4/prod"
ARIBA_REALM = "tatachem-T"

# KUSUM
ARIBA_VENDOR_ID = "S80292540"
ARIBA_QUESTIONNAIRE_ID = "Doc2955540815"

# # RAJ SALES
# ARIBA_VENDOR_ID = "S80300249"
# ARIBA_QUESTIONNAIRE_ID = "Doc2957718454"


# # FRANSTEK
# ARIBA_VENDOR_ID = "S80292331"
# ARIBA_QUESTIONNAIRE_ID = "Doc2955284488"

# Token cache for form API (refreshed when expired)
_form_api_token: Optional[str] = None
_form_api_token_expiry: float = 0.0
_form_api_token_lock = threading.Lock()

# Ariba field mapping: formName (correlationId) -> (itemId, correlationId) for submit payload
_form_field_mapping: dict[str, tuple[str, str]] = {}
_ariba_workspace_id: Optional[str] = None
# QnA and answers URLs from first questionnaire link in workspaces response (set by _fetch_ariba_qna_url_from_workspaces)
_ariba_qna_url_cached: Optional[str] = None
_ariba_answers_url_cached: Optional[str] = None

# Supabase: persist transcript/lifecycle/tool events to durable_agent_run_event
SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip().rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()

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
from call_activity import CallActivityRecorder

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


def load_supplier_general_info_form_from_json() -> dict[str, Any]:
    """Load form definition from form_supplier_general_info.json (Supplier General Information and subsections)."""
    json_path = Path(__file__).parent / "form_supplier_general_info.json"
    with open(json_path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_form_from_json(path: str | Path) -> dict[str, Any]:
    """Load a form definition from a JSON file. Path can be filename (relative to script dir) or full path."""
    json_path = Path(path)
    if not json_path.is_absolute():
        json_path = Path(__file__).parent / json_path
    with open(json_path, "r", encoding="utf-8") as f:
        return json.load(f)


def get_all_form_questions_from_supplier_form(form: dict[str, Any]) -> list[dict[str, Any]]:
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
                    if "user_answer" in sub and sub["name"] == "Supplier Name 1":
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


def _fetch_form_api_token_sync() -> tuple[str, int]:
    """
    Call Ariba OAuth token API (Client Credentials, Basic Auth).
    Uses FORM_AUTH_CLIENT_ID and FORM_AUTH_CLIENT_SECRET from env.
    Returns (token, expires_in_seconds).
    """
    import base64
    client_id = os.environ.get("FORM_AUTH_CLIENT_ID", "").strip()
    client_secret = os.environ.get("FORM_AUTH_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        raise ValueError("FORM_AUTH_CLIENT_ID and FORM_AUTH_CLIENT_SECRET must be set for form API auth")
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
    """Return a valid Bearer token for form API calls. Refreshes from auth API when expired."""
    global _form_api_token, _form_api_token_expiry
    with _form_api_token_lock:
        now = time.time()
        if _form_api_token and _form_api_token_expiry > now + 60:
            return _form_api_token
        token, expires_in = _fetch_form_api_token_sync()
        _form_api_token = token
        _form_api_token_expiry = now + expires_in - 60
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
    application_key = os.environ.get("ARIBA_API_KEY", "").strip()
    if application_key:
        h["apiKey"] = application_key
    return h


def _ariba_workspaces_url(vendor_id: str) -> str:
    return f"{ARIBA_BASE_URL}/vendors/{vendor_id}/workspaces?realm={ARIBA_REALM}"


def _get_direct_qna_url() -> str:
    """Build qna URL directly from ARIBA_VENDOR_ID and ARIBA_QUESTIONNAIRE_ID (no workspace ID in path)."""
    return f"{ARIBA_BASE_URL}/vendors/{ARIBA_VENDOR_ID}/workspaces/questionnaires/{ARIBA_QUESTIONNAIRE_ID}/qna?realm={ARIBA_REALM}"


def _ariba_answers_url_from_qna_url(qna_url: str) -> str:
    """
    Derive answers URL from qna URL by:
      (1) Replacing '/qna' with '/answers'
      (2) Removing the workspace id after 'workspaces'
    """
    # Split at '/workspaces'
    if '/workspaces/' in qna_url:
        prefix, suffix = qna_url.split('/workspaces/', 1)
        # Remove the workspace id segment after /workspaces/
        parts = suffix.split('/', 1)
        # parts[0] is the workspace id; parts[1] is the rest
        if len(parts) == 2:
            rest = parts[1]
        else:
            rest = ''
        # Replace '/qna' or '/qna?' with '/answers' or '/answers?'
        if '/qna?' in rest:
            rest = rest.replace('/qna?', '/answers?')
        else:
            rest = rest.replace('/qna', '/answers')
        return f"{prefix}/workspaces/{rest}"
    else:
        # fallback, just replace
        if "/qna?" in qna_url:
            return qna_url.replace("/qna?", "/answers?")
        return qna_url.replace("/qna", "/answers")


def _parse_first_qna_url_from_workspaces_response(data: dict[str, Any]) -> tuple[str, str]:
    """
    Parse workspaces API response: data.workspaces is a dict of category -> list of workspaces.
    Returns (workspace_id, qna_url) using the first workspace and its first questionnaire's QuestionAnswer link.
    """
    global _ariba_workspace_id, _ariba_qna_url_cached, _ariba_answers_url_cached
    ws_obj = data.get("workspaces", {})
    if not isinstance(ws_obj, dict):
        ws_obj = data.get("data", {}) if isinstance(data.get("data"), dict) else {}
    # ws_obj is e.g. {"SupplierRequest": [...], "Registration": [...]}
    first_workspace: dict[str, Any] | None = None
    key = "Registration"
    lst = ws_obj.get(key) if isinstance(ws_obj.get(key), list) else []
    if lst:
        first_workspace = lst[0]
    if not first_workspace:
        # Fallback: any first list value
        for v in ws_obj.values() if isinstance(ws_obj, dict) else []:
            if isinstance(v, list) and v:
                first_workspace = v[0]
                break
    if not first_workspace:
        raise ValueError("Ariba workspaces response empty or missing workspaces list")
    wid = first_workspace.get("workspaceId") or first_workspace.get("workspaceID") or first_workspace.get("id")
    if not wid:
        raise ValueError("Ariba workspaces response missing workspaceId")
    questionnaires = first_workspace.get("questionnaires") or []
    if not questionnaires:
        raise ValueError("Ariba workspace has no questionnaires")
    first_q = questionnaires[0]
    links = first_q.get("links") or []
    qna_href = None
    for link in links:
        if isinstance(link, dict) and (link.get("rel") or link.get("type")) == "QuestionAnswer":
            qna_href = link.get("href")
            break
    if not qna_href or not isinstance(qna_href, str):
        raise ValueError("First questionnaire has no QuestionAnswer link")
    # Use host from ARIBA_BASE_URL, path and query from workspace QnA URL
    base_parts = urllib.parse.urlparse(ARIBA_BASE_URL)
    qna_parts = urllib.parse.urlparse(qna_href)
    qna_url = urllib.parse.urlunparse((
        base_parts.scheme,
        base_parts.netloc,
        qna_parts.path or "",
        "",
        qna_parts.query or "",
        "",
    ))
    if "realm=" not in qna_url:
        qna_url = f"{qna_url}?realm={ARIBA_REALM}" if "?" not in qna_url else f"{qna_url}&realm={ARIBA_REALM}"
    _ariba_workspace_id = str(wid)
    _ariba_qna_url_cached = qna_url
    _ariba_answers_url_cached = _ariba_answers_url_from_qna_url(qna_url)
    return str(wid), qna_url


def get_ariba_workspace_id(vendor_id: str) -> str:
    """GET workspace ID and first questionnaire QnA URL from Ariba; parses response and sets _ariba_qna_url_cached, _ariba_answers_url_cached."""
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
    logger.info("Ariba workspaces response: %s", data)
    wid, _ = _parse_first_qna_url_from_workspaces_response(data)
    return wid


def _normalize_ariba_qna_to_form(data: dict[str, Any]) -> dict[str, Any]:
    """
    Normalize Ariba Q&A response to our form schema (title, sections with fields).
    Populates _form_field_mapping (formName -> (itemId, correlationId)) for submit.
    """
    global _form_field_mapping
    _form_field_mapping = {}
    logger.info("Ariba Q&A response keys: %s", list(data.keys()))
    raw_list = data.get("_embedded", {}).get("questionAnswerList", [])
    items: list[dict[str, Any]] = []
    for entry in raw_list:
        qa = entry.get("questionAnswer") if isinstance(entry, dict) else None
        if qa:
            items.append(qa)
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
    Fetch supplier registration form (Q&A) from Ariba. Uses direct qna URL built from
    ARIBA_VENDOR_ID and ARIBA_QUESTIONNAIRE_ID. Returns normalized form schema for prompt;
    populates _form_field_mapping, _ariba_qna_url_cached, _ariba_answers_url_cached.
    """
    global _ariba_qna_url_cached, _ariba_answers_url_cached
    if not _ariba_qna_url_cached:
        _ariba_qna_url_cached = _get_direct_qna_url()
        _ariba_answers_url_cached = (
            _ariba_qna_url_cached.replace("/qna?", "/answers?")
            if "/qna?" in _ariba_qna_url_cached
            else _ariba_qna_url_cached.replace("/qna", "/answers")
        )
    url = _ariba_qna_url_cached
    if not url:
        raise ValueError("Ariba QnA URL not set")
    headers = _ariba_headers()
    try:
        req = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        logger.info("Ariba Q&A url: %s", url)
        logger.info(f"Ariba Q&A response: {data}")
    except urllib.error.HTTPError as e:
        if e.code == 401:
            _clear_form_api_token()
            headers = _ariba_headers()
            req = urllib.request.Request(url, headers=headers, method="GET")
            with urllib.request.urlopen(req, timeout=30) as retry_resp:
                data = json.loads(retry_resp.read().decode("utf-8"))
        else:
            raise
    if data.get("sections") and isinstance(data.get("sections"), list):
        return data
    return _normalize_ariba_qna_to_form(data)


def _submit_form_to_fill_api_sync(answers: list[dict[str, Any]]) -> tuple[bool, str]:
    """POST form answers to Ariba answers endpoint. Uses _ariba_answers_url_cached (set when form was fetched)."""
    global _ariba_qna_url_cached, _ariba_answers_url_cached
    if not _ariba_answers_url_cached:
        _ariba_qna_url_cached = _get_direct_qna_url()
        _ariba_answers_url_cached = (
            _ariba_qna_url_cached.replace("/qna?", "/answers?")
            if "/qna?" in _ariba_qna_url_cached
            else _ariba_qna_url_cached.replace("/qna", "/answers")
        )
    fill_url = _ariba_answers_url_cached
    logger.info(f"Ariba answers URL: {fill_url}")
    if not fill_url:
        return False, "Ariba answers URL not set; fetch form from API first"
    ariba_answers: list[dict[str, Any]] = []
    for a in answers:
        corr_id = a.get("externalSystemCorrelationId") or a.get("correlationId", "")
        answer_val = a.get("answer", a.get("value", ""))
        ariba_answers.append({
            "externalSystemCorrelationId": str(corr_id),
            "answer": answer_val,
        })
    if not ariba_answers:
        return True, "No answers to submit"
    payload: dict[str, Any] = {"answers": ariba_answers, "triggerApprove": True}
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


def _flush_collected_form_answers_sync(
    collected: list[dict[str, Any]],
    run_browserbase: bool = True,
    run_browserbase_in_subprocess: bool = False,
) -> tuple[bool, str]:
    """
    Flush collected form answers: dedupe by externalSystemCorrelationId (last wins),
    then submit to Ariba form fill API and optionally run Browserbase form fill.

    When run_browserbase_in_subprocess=True (e.g. on_session_close), Browserbase runs in a
    detached subprocess so it can complete after the worker exits; the worker returns immediately.
    When run_browserbase_in_subprocess=False (e.g. end_call), Browserbase runs in-process and we wait.
    """
    if not collected:
        return True, "No form answers to submit"
    # Dedupe by externalSystemCorrelationId — keep last occurrence
    by_corr: dict[str, dict[str, Any]] = {}
    for a in collected:
        corr = (
            a.get("externalSystemCorrelationId")
            or a.get("correlationId")
            or a.get("formName")
            or ""
        )
        if corr:
            by_corr[corr] = {
                "externalSystemCorrelationId": corr,
                "answer": a.get("answer", a.get("value", "")),
            }
    answers_for_ariba = list(by_corr.values())
    if not answers_for_ariba:
        return True, "No valid form answers to submit"
    fill_success, fill_message = _submit_form_to_fill_api_sync(answers_for_ariba)
    if run_browserbase:
        form_answers_json = json.dumps(answers_for_ariba)
        logger.info(
            "Form answers JSON passed to Browserbase (%d items): %s",
            len(answers_for_ariba),
            form_answers_json,
        )
        if run_browserbase_in_subprocess:
            try:
                import subprocess
                import sys
                # logs_dir = Path(__file__).parent / "logs"
                # logs_dir.mkdir(exist_ok=True)
                # log_path = logs_dir / f"browserbase_ariba_{int(time.time())}_{os.getpid()}.log"
                # log_file = open(log_path, "w", encoding="utf-8")
                proc = subprocess.Popen(
                    [sys.executable, "-m", "browser_automation.ariba_form_fill"],
                    stdin=subprocess.PIPE,
                    # stdout=log_file,
                    # stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
                proc.stdin.write(form_answers_json.encode("utf-8"))
                proc.stdin.close()
                logger.info(
                    "Browserbase form fill started in background subprocess (pid=%s)",
                    proc.pid,
                )
            except Exception as e:
                logger.warning("Failed to start Browserbase subprocess: %s", e)
        else:
            try:
                from browser_automation.ariba_form_fill import run_ariba_form_fill
                bb_result = run_ariba_form_fill(form_answers_json=form_answers_json)
                if bb_result.get("success"):
                    logger.info(
                        "Browserbase form fill completed: session_id=%s live_url=%s",
                        bb_result.get("session_id"),
                        bb_result.get("live_url"),
                    )
                else:
                    logger.warning(
                        "Browserbase form fill failed: %s",
                        bb_result.get("error") or bb_result.get("message"),
                    )
            except Exception as e:
                logger.warning("Browserbase form fill failed: %s", e)
    else:
        logger.info(
            "Flush on session close: Ariba submit done; Browserbase skipped (use end_call or trigger_browserbase_session for browser fill)"
        )
    return fill_success, fill_message


def _flush_ariba_and_create_browserbase_session_sync(
    collected: list[dict[str, Any]],
) -> tuple[bool, str, str | None, dict[str, Any] | None]:
    """
    Dedupe form answers, submit to Ariba API, then create a Browserbase session (no form fill).
    Returns (fill_success, fill_message, form_answers_json_for_subprocess, session_result_dict or None).
    Used on session close so we can publish the session to the room before spawning the form-fill subprocess.
    """
    if not collected:
        return True, "No form answers to submit", None, None
    by_corr: dict[str, dict[str, Any]] = {}
    for a in collected:
        corr = (
            a.get("externalSystemCorrelationId")
            or a.get("correlationId")
            or a.get("formName")
            or ""
        )
        if corr:
            by_corr[corr] = {
                "externalSystemCorrelationId": corr,
                "answer": a.get("answer", a.get("value", "")),
            }
    answers_for_ariba = list(by_corr.values())
    if not answers_for_ariba:
        return True, "No valid form answers to submit", None, None
    fill_success, fill_message = _submit_form_to_fill_api_sync(answers_for_ariba)
    form_answers_json = json.dumps(answers_for_ariba)
    logger.info(
        "Form answers JSON for Browserbase (%d items): %s",
        len(answers_for_ariba),
        form_answers_json,
    )
    from browser_automation.ariba_form_fill import create_browserbase_session
    session_result = create_browserbase_session()
    return fill_success, fill_message, form_answers_json, session_result


async def _flush_publish_browserbase_then_subprocess(
    collected: list[dict[str, Any]],
    room: Any,
    recorder: CallActivityRecorder | None = None,
) -> None:
    """
    On session close: flush to Ariba, create Browserbase session, publish session_id to room
    (so participants get the link before disconnect), then spawn subprocess to do form fill in that session.
    """
    fill_success, fill_message, form_answers_json, session_result = await asyncio.to_thread(
        _flush_ariba_and_create_browserbase_session_sync,
        collected,
    )
    if session_result and session_result.get("success") and room:
        session_id = session_result.get("session_id")
        if session_id:
            await _publish_browserbase_session_to_room(room, session_id, recorder=recorder)
            logger.info(
                "Published browserbase_session to room before subprocess (session_id=%s); starting form-fill subprocess",
                session_id,
            )
    # Spawn subprocess: with existing session so it connects and fills; without session it would create its own
    if form_answers_json and session_result and session_result.get("success"):
        subprocess_payload = {
            "form_answers_json": form_answers_json,
            "session_id": session_result.get("session_id"),
            "connect_url": session_result.get("connect_url"),
        }
        stdin_data = json.dumps(subprocess_payload).encode("utf-8")
    elif form_answers_json:
        # Fallback: no session created (e.g. API error); subprocess will create its own (room already closed)
        stdin_data = form_answers_json.encode("utf-8")
    else:
        return
    try:
        import subprocess
        import sys
        # logs_dir = Path(__file__).parent / "logs"
        # logs_dir.mkdir(exist_ok=True)
        # log_path = logs_dir / f"browserbase_ariba_{int(time.time())}_{os.getpid()}.log"
        # log_file = open(log_path, "w", encoding="utf-8")
        proc = subprocess.Popen(
            [sys.executable, "-m", "browser_automation.ariba_form_fill"],
            stdin=subprocess.PIPE,
            # stdout=log_file,
            # stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        proc.stdin.write(stdin_data)
        proc.stdin.close()
        logger.info(
            "Browserbase form fill started in background subprocess (pid=%s)",
            proc.pid,
        )
    except Exception as e:
        logger.warning("Failed to start Browserbase subprocess: %s", e)


def load_form_for_prompt(default_json_path: str = "form_supplier_general_info.json") -> dict[str, Any]:
    """
    Load form for the agent prompt: from Ariba API if auth credentials are set, otherwise from JSON file.
    If the API request fails, falls back to the JSON file.
    """
    logger.info("Loading form for prompt from Ariba API")
    if os.environ.get("FORM_AUTH_CLIENT_ID", "").strip() and os.environ.get("FORM_AUTH_CLIENT_SECRET", "").strip():
        try:
            return fetch_form_from_api()
        except Exception as e:
            logger.error("Form API fetch failed: %s", e)
            logger.warning("Form API fetch failed, falling back to JSON file %s: %s", default_json_path, e)
    return load_form_from_json(default_json_path)


logger = logging.getLogger("outbound-caller")
logger.setLevel(logging.INFO)

# Topic for transcript data messages on LiveKit (subscribers can filter by this)
TRANSCRIPT_TOPIC = "transcript"
# Topic for browserbase session data (sessionId for viewing in room)
BROWSERBASE_SESSION_TOPIC = "browserbase_session"


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


async def _publish_browserbase_session_to_room(
    room: Any,
    session_id: str,
    recorder: CallActivityRecorder | None = None,
) -> None:
    """Publish browserbase session for viewing in the room (event + sessionId + timestamp)."""
    if room is None:
        return
    try:
        local = getattr(room, "local_participant", None)
        if local is None:
            return
        payload = {
            "event": "browserbase_session",
            "sessionId": session_id,
            "timestamp": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        }
        logger.info(
            "Publishing browserbase_session to room (topic=%s): %s",
            BROWSERBASE_SESSION_TOPIC,
            payload,
        )
        if recorder:
            await recorder.publish_data_message(
                topic=BROWSERBASE_SESSION_TOPIC,
                payload=payload,
                lifecycle_event="browserbase_session_published",
            )
        else:
            data = json.dumps(payload).encode("utf-8")
            await local.publish_data(data, topic=BROWSERBASE_SESSION_TOPIC)
        logger.info(
            "Published browserbase_session to room: event=%s sessionId=%s timestamp=%s",
            payload["event"],
            payload["sessionId"],
            payload["timestamp"],
        )
    except Exception as e:
        logger.warning("Failed to publish browserbase session to room: %s", e)


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
        
        # Load form for prompt: from Ariba API if provided/pre-loaded, else from API or JSON fallback
        if form is None:
            form = load_form_for_prompt("form_supplier_general_info.json")
        logger.info(f"Form: {form}")
        all_questions = get_all_form_questions_from_supplier_form(form)
        questions_block_lines = []
        for q in all_questions:
            name = q.get("name", "")
            name_lower = (name or "").lower()
            # Do not ask PAN, Bank, or GST details — skip these questions entirely
            if "pan" in name_lower or "bank" in name_lower or "gst" in name_lower or "gstin" in name_lower:
                continue
            # Use externalSystemCorrelationId (formName/correlationId) as the number before the question
            num = q.get("formName") or q.get("correlationId") or q.get("externalSystemCorrelationId") or q.get("number", "")
            if name == "Supplier Name 1":
                user_answer = q.get("user_answer") if q.get("user_answer") else "Not answered yet"
            else:   
                user_answer = "Not answered yet"
            desc = q.get("description")
            allowed = q.get("allowedValues")
            req = " (required)" if q.get("required") else ""
            # user_answer = q.get("user_answer") if q.get("user_answer") else "Not answered yet"
            line = f"- [{num}] {name}{req} — User answer: {user_answer}"
            if desc:
                line += f" — {desc}"
            if allowed:
                line += f" — Options: {', '.join(str(v) for v in allowed)}"
            questions_block_lines.append(line)
        questions_block = "\n                ".join(questions_block_lines) if questions_block_lines else "(No questions loaded)"
        # Supplier name from form: JSON form uses formName "supplierName1"; Ariba form uses question name "Full Company Name (Supplier Name)" or similar
        supplier_name = ""
        for q in all_questions:
            ans = (q.get("user_answer") or "").strip() if q.get("user_answer") else ""
            if not ans:
                continue
            if q.get("name") == "Supplier Name 1":
                supplier_name = ans
                break
            name_lower = (q.get("name") or "").lower()
            if "full company name" in name_lower or ("supplier name" in name_lower and "company" in name_lower):
                supplier_name = ans
                break
        if not supplier_name:
            supplier_name = "the registered company"
        supplier_name = supplier_name.lower()
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
                
                ## TONE & MANNER
                Be **polite and calm in every situation**. Give every response in a very polite way. Use courteous language (e.g. kripya, dhanyavaad, maafi chahti hoon). Never sound rushed, impatient, or curt. If the user is confused, repeats, or corrects — stay calm and polite. This applies to greetings, questions, corrections, refusals, and goodbyes.
                
                
                ## OPENING (MANDATORY — complete ALL steps in order before ANY form question)
                **Step 1** — Greet: "Namaste, main Tata Chemicals ki AI agent hoon. Mai yaha aapke supplier onboarding form ko bharne mein sahayta karne ke liye hu."
                **Step 2** — Confirm company (HARD GATE — you MUST get an explicit yes/no before doing ANYTHING else):
                Ask: "Kya main {supplier_name} ki taraf se kisi representative se baat kar rahi hoon?"
                Then STOP and WAIT for the user's response. Do NOT proceed to any form question, section listing, or any other topic until the user explicitly confirms "yes/haan/ji" or denies "no/nahi".
                  - If **yes/haan/ji/sahi hai**: Company confirmed. Only now proceed to Step 3.
                  - If **no/nahi** or "I don't work here" / "main is org se nahi hu" / "nahi mai iss industry se nahi hu" / "we are not suppliers" / "wrong number" / "galat number": you MUST **speak** a polite response OUT LOUD first — e.g. "Kripya maafi chahti hoon, aapko disturb kiya. Aapka time dene ke liye dhanyavaad. Aapka din accha ho!" — so the user **hears** the goodbye. Only AFTER saying this out loud, call end_call(sorry=True). **Never** cut the call without speaking the polite goodbye first.
                  - If the user gives an **ambiguous/unclear** response (e.g. "what?", "kya?", "kaun?", doesn't answer the question), politely ask again: "Main bas confirm karna chahti hoon — kya aap {supplier_name} company se bol rahe hain?" Do NOT move forward until you get a clear yes or no.
                **Step 3** — After company is confirmed, say: "Mai ye dekh paa rahi hu ki kuch sawalon ke jawab fill ho chuke hai, kya aap bache hue sawalo ko complete karne mein meri madat kar sakte ho?"
                Then proceed to the CALL FLOW below.
                
                ## FORM QUESTIONS & ANSWERS
                {questions_block}
                
                ## CALL FLOW
                - Complete the OPENING steps above first (greet → confirm company → get explicit yes → then proceed).
                - Only after the user confirms they are from {supplier_name}, move to below steps.
                - Mention the section names which are left to be answered and ask them which one they want to answer first.
                - If they choose a section, ask the questions in that section one by one.
                - If they say kuch bhi/koi bhi chalega, begin with the first question in that section which is not answered yet.
                - If they have answered some questions, ask the ones that are not answered yet.
                - One question at a time. After you get the complete information for a field (and for critical fields, after confirmation — see below), call submit_form_answers only for the new field, then acknowledge briefly and ask the next question. Do NOT wait until the entire form is filled; update after every field.
                - **Long inputs in parts (mobile number, account number, IFSC code, etc.)**: If the user gives only part of the answer (e.g. first few digits), repeat back what you heard and ask for the rest. Say: "[Repeat the part you heard] — iske aage bataye" / "Ye suna: [repeat]. Baaki bataiye." Do not move to the next question until the full value is complete. Once you have the complete information, then confirm (for critical fields) and call submit_form_answers.
                - If a question has allowed values (Options): when the list is long, mention only 3–4 options when asking; if the user specifically asks for all options (e.g. "sab batao", "poori list"), then mention all. For short lists, you may mention all. They can answer in their own words if it matches.
                - For optional (non-required) questions, they can say "skip" or "nahi chahiye"; then move to the next.
                - Keep responses crisp (max 30 words when possible).
                - When all questions are done, say: "Form complete ho gaya. Dhanyavaad aapke time ke liye. Aapka din accha ho!" then call end_call. (You will have already called submit_form_answers after each field.)
                
                ## CONFIRMATION OF CRITICAL FIELDS (MANDATORY BEFORE SUBMIT FOR THAT FIELD)
                For **important fields where accuracy is critical** (account number, mobile number, primary contact mobile, email, bank account number, IFSC code, or other bank/financial details): after you get the answer, repeat it back and get verbal confirmation (e.g. "Aapka account number [repeat digits] hai, sahi hai?" / "Mobile number [repeat] confirm karein?") and wait for yes/sahi hai/galat. If they correct it, update the answer and confirm again if needed. Then call submit_form_answers with **only** that field's question-answer (the one you just got). Do not skip this for bank details, account numbers, or phone numbers.
                
                ## SPOKEN NUMBER CONVERSION (CRITICAL — apply everywhere)
                Users often speak numbers as words in Hindi or English instead of digits. You MUST silently convert these to digits before validating or submitting. NEVER ask the user to "say it in numbers" — just convert and confirm the digit version.
                Hindi: sunya/shunya=0, eek/ek=1, do=2, teen=3, chaar=4, paanch=5, chhah/chhe=6, saat=7, aath=8, nau=9, das=10, gyarah=11, baarah=12, terah=13, chaudah=14, pandrah=15, solah=16, satrah=17, athaarah=18, unees=19, bees=20, tees=30, chaalees=40, pachaas=50, saath=60, sattar=70, assi=80, nabbe=90, sau=100, hazaar/hazar=1000, laakh/lakh=100000, crore/karod=10000000.
                English: zero=0, one=1, two=2, three=3, four=4, five=5, six=6, seven=7, eight=8, nine=9, ten=10, hundred=100, thousand=1000, lakh=100000, million=1000000.
                Examples: "nau eight saat six paanch four teen two eek zero" → 9876543210. "ek lakh bees hazaar" → 120000. "triple seven" → 777.
                Apply this to ALL numeric fields: mobile numbers, account numbers, IFSC codes, postal codes, GST numbers, etc. Always convert first, then validate the digit form.

                ## LOGICAL VALIDATION OF RESPONSES
                After converting spoken numbers to digits (see above), validate every answer before submitting. If validation fails, politely tell the user the expected format and ask them to provide it again. Do not use any external tool — use only your knowledge.

                ### Mobile numbers (primaryContactMobile, supplierContactMobile, contact3Mobile)
                - Must be exactly 10 digits (for Indian numbers).
                - Must start with 6, 7, 8, or 9.
                - If user gives country code prefix (like +91 or 91), strip it — store only the 10-digit number.
                - If fewer or more than 10 digits, say: "Mobile number mein das digit hone chahiye, aapne [count] digits diye. Kripya poora number bataiye."

                ### Email addresses (primaryContactEmail, supplierContactEmail, contact3Email)
                - Must contain exactly one "@" and at least one "." after the "@".
                - Common domains: gmail.com, yahoo.com, outlook.com, hotmail.com, company domains. If the user says "at the rate" or "at" → "@". If they say "dot" → ".".
                - If format looks wrong (no @, no dot in domain, spaces), say: "Ye email format sahi nahi lag raha. Kripya email phir se bataiye, jaise name at gmail dot com."

                ### Postal / PIN codes (supplierAddressGst.postalCode, bankPostalCode)
                - Indian PIN code: exactly 6 digits, first digit is 1–9 (never 0).
                - If not 6 digits or starts with 0, say: "Indian PIN code mein chhah digit hone chahiye aur pehla digit zero nahi ho sakta. Kripya sahi PIN code bataiye."

                ### IFSC Code (bankKeyIfsc)
                - Exactly 11 characters.
                - First 4 characters: letters (A–Z) — this is the bank code.
                - 5th character: always "0" (zero).
                - Last 6 characters: digits (0–9) — this is the branch code.
                - Example: SBIN0001234, HDFC0000123.
                - If format is wrong, say: "IFSC code mein gyaarah characters hote hain — pehle chaar letters, phir zero, phir chhah digits. Jaise SBIN zero zero zero ek do teen chaar. Kripya sahi IFSC code bataiye."
                - Use your knowledge to cross-check: if the user gave a bank name earlier, the IFSC should start with that bank's code (e.g. HDFC bank → HDFC, SBI → SBIN, ICICI → ICIC, Axis → UTIB, PNB → PUNB, Bank of Baroda → BARB, Kotak → KKBK, Yes Bank → YESB, IndusInd → INDB, Union Bank → UBIN, Canara → CNRB, Bank of India → BKID, Indian Bank → IDIB, Central Bank → CBIN, UCO Bank → UCBA, IOB → IOBA). If it does not match, politely ask the user to confirm.

                ### Bank Account Number (bankAccountNumber)
                - Typically 9 to 18 digits for Indian banks.
                - Must be all digits (no letters or special characters).
                - If fewer than 9 or more than 18 digits, say: "Bank account number usually nau se athaarah digits ka hota hai. Aapne [count] digits diye. Kripya confirm karein ya dubara bataiye."

                ### IBAN / SWIFT Code (ibanSwiftCode)
                - SWIFT/BIC code: 8 or 11 alphanumeric characters (e.g. HDFCINBB, SBININBB123).
                - IBAN: varies by country, typically 15–34 alphanumeric characters starting with 2-letter country code.
                - If it does not look like either format, politely ask: "Ye SWIFT ya IBAN format mein nahi lag raha. Kripya check karke bataiye."

                ### GST Number (gstNo)
                - Exactly 15 alphanumeric characters.
                - Format: first 2 digits = state code (01–37), next 10 characters = PAN, 13th = entity number (1–9 or Z), 14th = "Z", 15th = checksum (digit or letter).
                - Example: 27AABCU9603R1ZM (27=Maharashtra, AABCU9603R=PAN, 1=entity, Z=fixed, M=checksum).
                - If not 15 characters or format looks wrong, say: "GST number mein pandrah characters hone chahiye. Kripya apna GST number dubara check karke bataiye."
                - Cross-check: if user already gave state (e.g. Maharashtra=27, Gujarat=24, Delhi=07, Karnataka=29, Tamil Nadu=33, Rajasthan=08, UP=09, West Bengal=19, Telangana=36, Haryana=06), the first 2 digits of GST should match. If mismatch, politely ask user to confirm.

                ### Date of Incorporation (dateOfIncorporation)
                - Today's date is {date.today().strftime("%d %B %Y")}. Must be a valid date and must not be after today.
                - If user says only year (e.g. "2015"), ask for full date (day, month, year).
                - Accept common formats: DD/MM/YYYY, DD-MM-YYYY, "15 January 2015", etc. Convert to standard format.

                ### Country / City / State consistency
                - If country is India, city should be an Indian city, state should be an Indian state, PIN code should be 6 digits.
                - If country is not India, adapt validation accordingly using your knowledge.
                - If city does not match the country or state, politely ask: "Aapne [city] bataya — ye [state/country] mein hai? Kripya confirm karein."

                ### Bank branch name (bankBranchName)
                - Use your knowledge to validate or suggest the likely official branch name. Politely ask the user to confirm before submitting.

                ### General rules
                - Name fields (supplier name, contact first/last name, account holder name): should contain only letters and spaces (no digits or special characters). If user gives digits in a name field, politely clarify.
                - For any field, if the answer seems nonsensical or placeholder-like (e.g. "abc", "123", "test"), politely ask the user to confirm that is their real answer.
                
                ## RESTRICTIONS
                - No CRM/system references
                - One question at a time
                - Do not collect phone/email beyond what is in the form (e.g. Primary Contact Mobile, Email are in the list)
                - **Do NOT ask for or collect PAN details, Bank details, or GST/GSTIN details.** Skip those topics entirely. If the user volunteers such information, do not record or submit it.
                
                ## END CALL SEQUENCE
                For **every** call end (including when user says no / wrong org / "iss industry se nahi hu" / not interested): you MUST speak a polite goodbye OUT LOUD first so the user hears it — e.g. "Kripya maafi chahti hoon. Dhanyavaad, aapka din accha ho!" — then call end_call. When ending because user said they are not from this industry / wrong org / not interested / wrong number, call end_call(sorry=True). For normal form-complete endings, call end_call(sorry=False).
                1. SAY the goodbye out loud (user must hear it)
                2. Call end_call(sorry=True if user refused/not from industry/wrong org, else sorry=False)
                
                ## ANSWERING MACHINE DETECTION
                Signs: automated greetings, "leave a message", beep tones
                Action: Call detected_answering_machine (outcome is recorded automatically).
                
                ## TOOL CALL BEHAVIOR
                Do NOT say any waiting phrase before end_call or submit_form_answers - call them silently.
                When calling submit_form_answers send the user answers in English, if they are not in English, convert them to English. Pass **only** the question-answer(s) for the field you just collected — do NOT include previously submitted fields. One field per call. Use this format for each object: fieldName (from the form question name), externalSystemCorrelationId (from the form, e.g. formName/correlationId in the question list), answer (from the conversation).
                Use trigger_browserbase_session when the user asks to open the form in a browser, or to fill the form on the Ariba web page. You may pass form_answers_json (same format as submit_form_answers) to pre-fill fields. After calling, you can tell the user the session is open and share the live_url if they want to view it.
                
                ## KEY RULES
                - Be **polite and calm in every situation** — respond in a very polite way at all times.
                - Be professional, conversational and helpful, not robotic
                - Focus on filling the full Supplier General Information form; use the question list above as the single source of questions (all sections and subsections in order)
                - If user gives long inputs (mobile, account number) in parts: repeat what you heard and say "iske aage bataye" / "baaki bataiye" until the full value is complete; only then confirm and submit.
                - Call submit_form_answers **after every field** with **only** the question-answer for the field you just got — do not pass all fields. One field per submit. Do not wait until the whole form is done.
                - For critical fields (account number, mobile, bank details, IFSC, etc.): repeat the value to the user and get confirmation, then call submit_form_answers with only that field's Q&A.
                - **Spoken numbers**: ALWAYS silently convert spoken Hindi/English number words to digits (e.g. "eek" → 1, "paanch" → 5, "triple nine" → 999). NEVER tell the user "please say it in numbers" — just convert and confirm the digit form.
                - **Validate before submitting**: For every field, apply the format rules in LOGICAL VALIDATION OF RESPONSES. If validation fails, tell the user the expected format and ask again. Only call submit_form_answers after the answer passes validation.
                - When country/city/region might not match, use your knowledge to validate; then politely ask the user to confirm or correct.
                - For bank branch name: use your knowledge to validate; confirm with the user before submitting.
                - When user says no / wrong org / "iss industry se nahi hu" / not interested: always SPEAK the polite goodbye out loud first (so they hear it), then end_call(sorry=True). Never hang up without responding.
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
        # Collected form answers during conversation; flushed to form fill on session end
        self.collected_form_answers: list[dict[str, Any]] = []
        self._form_answers_flushed: bool = False
        # Room reference for publishing transcript data messages (set in entrypoint)
        self.room: Any = None
        self.recorder: CallActivityRecorder | None = None
        # Supabase durable_agent_run_event: run_id/workflow_id set in entrypoint from metadata
        self.run_id: str | None = None
        self.workflow_id: str = ""
        self.participant_identity: str = ""

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
    async def end_call(self, ctx: RunContext, sorry: bool = False):
        """End the call. STRICT REQUIREMENTS:
        
        1. You MUST have already SPOKEN the closing greeting OUT LOUD (e.g. "Dhanyavaad aapke time ke liye. Aapka din accha ho!" or "Sorry to disturb. Thank you for your time.")
        
        Args:
            sorry: Set to True when the user said they are not from this industry / wrong org / not interested / wrong number — so we ended politely with a sorry/thank-you. False for normal form-complete endings.
        
        DO NOT call this tool without speaking the closing greeting first."""
        participant_identity = self.participant.identity if self.participant else "unknown"
        logger.info(f"ending the call for {participant_identity}, sorry={sorry}")
        
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
        
        if sorry:
            speech_handle = ctx.session.say("Kripya maafi chahti hoon. Dhanyavaad, aapka din accha ho!", allow_interruptions=False)
            await speech_handle.wait_for_playout()
        else:
            speech_handle = ctx.session.say("Dhanyavaad aapke time ke liye. Aapka din accha ho!", allow_interruptions=False)
            await speech_handle.wait_for_playout()
        
        # Flush to Ariba, create Browserbase session (recorded to Supabase),
        # then spawn subprocess for form fill (survives process exit).
        if not self._form_answers_flushed and self.collected_form_answers:
            self._form_answers_flushed = True
            to_flush = list(self.collected_form_answers)
            await _flush_publish_browserbase_then_subprocess(to_flush, self.room, self.recorder)
        
        await self.hangup()

    @function_tool()
    async def submit_form_answers(self, ctx: RunContext, form_answers_json: str) -> dict:
        """
        MANDATORY: Call this after each field (or batch) to record the user's answer. Answers are
        stored in a list and published to the room. When the call ends, all collected answers are
        submitted to the form fill (Ariba API and optional Browserbase session) in one go.

        Args:
            form_answers_json: A JSON string: array of objects, each with:
                - fieldName: question label from form (e.g. "Bank Name")
                - externalSystemCorrelationId: field ID from form (e.g. "KI_17088017")
                - answer: the vendor's answer from the conversation (English string)

                Example: [{"fieldName": "Bank Name", "externalSystemCorrelationId": "KI_17088017", "answer": "HDFC Bank"}]

        Returns:
            Status dict with success and message.
        """
        try:
            answers = json.loads(form_answers_json)
            if not isinstance(answers, list):
                answers = [answers]
            # Normalize and append to collected list (same field updated = last wins when we flush)
            for a in answers:
                self.collected_form_answers.append({
                    "fieldName": a.get("fieldName", ""),
                    "externalSystemCorrelationId": (
                        a.get("externalSystemCorrelationId")
                        or a.get("correlationId")
                        or a.get("formName")
                        or ""
                    ),
                    "answer": a.get("answer", a.get("value", "")),
                })
            payload = {
                "event": "form_answers",
                "role": "agent",
                "form_answers": answers,
                "timestamp": datetime.utcnow().isoformat(),
            }
            if self.recorder:
                await self.recorder.publish_data_message(
                    topic=TRANSCRIPT_TOPIC,
                    payload=payload,
                    lifecycle_event="form_answers_published",
                )
            elif self.room:
                await _publish_transcript_to_room(self.room, payload)
            logger.info(
                "Collected form_answers (total %d): added %d Q&A pairs",
                len(self.collected_form_answers),
                len(answers),
            )
            return {
                "success": True,
                "message": f"Recorded {len(answers)} answer(s); total collected: {len(self.collected_form_answers)}. Will submit all when call ends.",
                "count": len(answers),
                "total_collected": len(self.collected_form_answers),
            }
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

    @function_tool()
    async def trigger_browserbase_session(
        self, ctx: RunContext, form_answers_json: str = ""
    ) -> dict[str, Any]:
        """Start a Browserbase browser session, open the Ariba login page, log in with configured credentials,
        and optionally fill the form with the given answers. Use this when the user or workflow requires
        filling the supplier form in the actual Ariba web UI (e.g. for verification or manual follow-up).
        Session runs in region ap-southeast-1. Returns session_id and live_url for viewing the browser.

        Args:
            form_answers_json: Optional JSON string - array of objects with externalSystemCorrelationId and answer
                (same format as submit_form_answers). If provided, the script will attempt to fill these in the form.

        Returns:
            JSON with success, session_id, live_url, message, and optional error.
        """
        from browser_automation.ariba_form_fill import run_ariba_form_fill

        room = self.room
        recorder = self.recorder
        if room:
            loop = asyncio.get_running_loop()
            def on_session_ready(session_id: str, _live_url: str | None) -> None:
                loop.call_soon_threadsafe(
                    lambda: asyncio.create_task(
                        _publish_browserbase_session_to_room(room, session_id, recorder=recorder)
                    )
                )
        else:
            on_session_ready = None

        result = await asyncio.to_thread(
            run_ariba_form_fill,
            form_answers_json=form_answers_json or None,
            on_session_ready=on_session_ready,
        )
        logger.info(
            "trigger_browserbase_session result: success=%s session_id=%s",
            result.get("success"),
            result.get("session_id"),
        )
        return result


async def entrypoint(ctx: JobContext):
    logger.info(f"connecting to room {ctx.room.name}")
    await ctx.connect()
    
    # Initialize batch dialer fields
    lead_id = None
    batch_name = None
    room_id = ctx.room.name
    
    # Initialize trunk_id - will be read from metadata
    outbound_trunk_id = None
    
    metadata: dict[str, Any] = {}

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

    # Load form in a thread so Ariba API fetch does not block the event loop
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
    agent.run_id = metadata.get("run_id") or metadata.get("runId") or None
    agent.workflow_id = metadata.get("workflow_id") or ""
    agent.participant_identity = participant_identity

    recorder = CallActivityRecorder(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)
    recorder.configure(
        run_id=agent.run_id,
        workflow_id=agent.workflow_id,
        room_id=room_id,
        room=ctx.room,
        participant_identity=participant_identity,
    )
    asyncio.create_task(
        recorder.update_run(
            room_id=room_id,
            phone_number=phone_number,
            participant_name=participant_identity,
        )
    )
    agent.recorder = recorder

    # the following uses GPT-4o, Deepgram and Cartesia
    # VAD is required for responsive barge-in: it detects "user started speaking" from audio
    # so the framework can stop the agent (allow_interruptions=True). Without VAD, interruption
    # handling would rely only on STT and be slower.
    session = AgentSession(
        # turn_detection=MultilingualModel(),  # Temporarily disabled - requires model download
        turn_detection="vad",  # VAD-only so min_endpointing_delay is applied (session-level wait before agent responds)
        vad=silero.VAD.load(
            activation_threshold=0.9,      # Lower = more sensitive (default: 0.5)
            min_speech_duration=1,         # Min speech duration to trigger (default: 0.05s)
            min_silence_duration=3.0,      # Silence (sec) after speech before "user finished" — listen until 5s silence, then take as input (default: 0.55s).
        ),
        stt=sarvam.STT(model = 'saarika:v2.5', language='hi-IN'),
        # stt=deepgram.STT(language='en'),
        # you can also use OpenAI's TTS with openai.TTS()
        # Cartesia speed: 0.6–1.5 (multiplier; <1 = slower). See https://docs.cartesia.ai (Volume, Speed, Emotion).
        tts=cartesia.TTS(model='sonic-3', voice='95d51f79-c397-46f9-b49a-23763d3eaa2d', language='hi', speed=0.95),
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
    recorder.attach_to_session(session)
    agent.transcript = recorder.transcript

    # Register cleanup handler for when session ends (handles unexpected disconnects)
    @session.on("close")
    def on_session_close():
        logger.info(f"Session closed for room {room_id}")
        end_ts = datetime.utcnow().isoformat()
        asyncio.create_task(recorder.record_lifecycle("session_closed", room_id=room_id))
        asyncio.create_task(recorder.update_run(run_status="Completed", end_timestamp=end_ts))
        # Flush to Ariba, create Browserbase session, publish to room (so link is visible before disconnect), then subprocess form fill
        if not agent._form_answers_flushed and agent.collected_form_answers:
            agent._form_answers_flushed = True
            to_flush = list(agent.collected_form_answers)
            asyncio.create_task(_flush_publish_browserbase_then_subprocess(to_flush, agent.room, agent.recorder))
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

    # Start the session first before dialing so the agent does not miss anything the user says.
    # Pass participant_identity so RoomIO explicitly waits for and links to the SIP participant's
    # audio track; otherwise we rely on "first participant" and can miss linking if timing is off.
    session_started = asyncio.create_task(
        session.start(
            agent=agent,
            room=ctx.room,
            room_input_options=RoomInputOptions(
                participant_identity=participant_identity,
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
    asyncio.create_task(
        recorder.record_lifecycle(
            "call_initiated",
            phone_number=phone_number,
            trunk_id=outbound_trunk_id,
        )
    )
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
        asyncio.create_task(
            recorder.record_lifecycle(
                "participant_joined",
                participant_identity=participant.identity,
            )
        )

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
        outcome = "busy" if sip_code == "486" else "no_answer" if sip_code == "480" else "error"
        end_ts = datetime.utcnow().isoformat()
        asyncio.create_task(recorder.record_sip_error(sip_status, sip_code=sip_code))
        asyncio.create_task(
            recorder.update_run(
                outcome=outcome,
                run_status="Failed",
                end_timestamp=end_ts,
            )
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
    _agent_name = (
        os.getenv("LIVEKIT_AGENT_NAME", "tatachemicals-voice-agent").strip()
        or "tatachemicals-voice-agent"
    )
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            agent_name=_agent_name,
        )
    )