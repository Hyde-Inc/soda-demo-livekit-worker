"""
Browserbase + Playwright script: create a session in ap-southeast-1, open Ariba login,
authenticate with env credentials, and optionally fill form fields.

Designed to be called by the voice agent (e.g. via trigger_browserbase_session tool).
Returns a JSON-serializable dict with session_id, live_url, success, and message.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

BROWSERBASE_REGION = "ap-southeast-1"

_FORM_LABEL_MAP: dict[str, str] = {}
_FORM_NUMBER_MAP: dict[str, str] = {}
_ADDRESS_SUBFIELD_NAMES: set[str] = set()
_REQUIRED_FORM_NAMES: set[str] = set()

# LOGS_DIR = Path(__file__).resolve().parent.parent / "logs"  # disabled: file logging off, console only


def _load_form_maps() -> tuple[dict[str, str], dict[str, str]]:
    """Load formName -> label AND formName -> question number from the JSON spec."""
    global _FORM_LABEL_MAP, _FORM_NUMBER_MAP, _ADDRESS_SUBFIELD_NAMES, _REQUIRED_FORM_NAMES
    if _FORM_LABEL_MAP:
        return _FORM_LABEL_MAP, _FORM_NUMBER_MAP
    try:
        form_path = Path(__file__).resolve().parent.parent / "form_supplier_general_info.json"
        if not form_path.exists():
            return _FORM_LABEL_MAP, _FORM_NUMBER_MAP
        data = json.loads(form_path.read_text(encoding="utf-8"))

        def _extract(fields: list, parent_has_subfields: bool = False) -> None:
            for field in fields:
                fn = field.get("formName")
                name = field.get("name")
                number = field.get("number")
                answer_type = field.get("answerType", "")
                if fn and name:
                    _FORM_LABEL_MAP[fn] = str(name).strip()
                if fn and number:
                    _FORM_NUMBER_MAP[fn] = str(number).strip()
                if fn and field.get("required") and answer_type != "File":
                    _REQUIRED_FORM_NAMES.add(fn)
                if parent_has_subfields and fn:
                    _ADDRESS_SUBFIELD_NAMES.add(fn)
                for sub in field.get("subfields", []):
                    sfn = sub.get("formName")
                    sname = sub.get("name")
                    if sfn and sname:
                        _FORM_LABEL_MAP[sfn] = str(sname).strip()
                        _ADDRESS_SUBFIELD_NAMES.add(sfn)
                    if sfn and sub.get("required"):
                        _REQUIRED_FORM_NAMES.add(sfn)

        for section in data.get("sections", []):
            _extract(section.get("fields", []))
            for subsection in section.get("subsections", []):
                _extract(subsection.get("fields", []))
    except Exception as e:
        logger.debug("Could not load form maps: %s", e)
    return _FORM_LABEL_MAP, _FORM_NUMBER_MAP


def _dump_page_html(page: Any, label: str) -> str | None:
    """Disabled: was saving HTML to logs/. Re-enable block below for disk dumps."""
    return None
    # try:
    #     LOGS_DIR.mkdir(parents=True, exist_ok=True)
    #     ts = int(time.time())
    #     main_html = page.content()
    #     frame_html = ""
    #     try:
    #         sm_frame = page.frame(name="SMFrame")
    #         if sm_frame:
    #             frame_html = sm_frame.content()
    #     except Exception:
    #         pass
    #
    #     combined = f"<!-- PAGE URL: {page.url} -->\n<!-- LABEL: {label} -->\n"
    #     combined += "<!-- === MAIN PAGE HTML === -->\n" + main_html
    #     if frame_html:
    #         combined += "\n\n<!-- === SMFRAME HTML === -->\n" + frame_html
    #
    #     dump_path = LOGS_DIR / f"ariba_{label}_{ts}.html"
    #     dump_path.write_text(combined, encoding="utf-8")
    #     logger.info("HTML dump saved: %s (url: %s)", dump_path.name, page.url)
    #     return str(dump_path)
    # except Exception as e:
    #     logger.warning("Could not dump HTML (%s): %s", label, e)
    #     return None


def _fill_text_by_number(page: Any, qnum: str, label: str, value: str) -> bool:
    """Use Playwright locators to fill a text input in the stItemRow matching qnum."""
    import re as _re
    qnum_escaped = _re.escape(qnum)
    pattern = _re.compile(rf"(^|\s){qnum_escaped}(\s|$)")

    targets = [page]
    try:
        sm = page.frame(name="SMFrame")
        if sm:
            targets.insert(0, sm)
    except Exception:
        pass

    for ctx in targets:
        try:
            rows = ctx.locator("tr.stItemRow")
            count = rows.count()
            for i in range(count):
                row = rows.nth(i)
                cell_text = row.text_content(timeout=3000) or ""
                cell_text = " ".join(cell_text.split())
                if not pattern.search(cell_text):
                    continue
                inp = row.locator("td.columnBreak input.w-txt, td.columnBreak textarea").first
                if inp.count() == 0:
                    inp = row.locator("input.w-txt:not([type=hidden]), textarea").first
                if inp.count() == 0:
                    continue
                inp.scroll_into_view_if_needed()
                inp.click(timeout=3000)
                inp.fill(value, timeout=3000)
                return True
        except Exception as exc:
            logger.debug("_fill_text_by_number ctx failed: %s", exc)
            continue
    return False


def _select_dropdown_by_number(page: Any, qnum: str, value: str) -> str | bool:
    """Use Playwright locators to open an Ariba w-dropdown and select an option by text."""
    import re as _re
    qnum_escaped = _re.escape(qnum)
    pattern = _re.compile(rf"(^|\s){qnum_escaped}(\s|$)")
    value_lower = value.lower().strip()

    targets = [page]
    try:
        sm = page.frame(name="SMFrame")
        if sm:
            targets.insert(0, sm)
    except Exception:
        pass

    for ctx in targets:
        try:
            rows = ctx.locator("tr.stItemRow")
            count = rows.count()
            for i in range(count):
                row = rows.nth(i)
                cell_text = row.text_content(timeout=3000) or ""
                cell_text = " ".join(cell_text.split())
                if not pattern.search(cell_text):
                    continue
                dd = row.locator('div.w-dropdown[role="combobox"]').first
                if dd.count() == 0:
                    continue
                dd.scroll_into_view_if_needed()
                dd.click(timeout=3000)
                page.wait_for_timeout(300)
                items = row.locator('div.w-dropdown-item[role="option"]')
                item_count = items.count()
                for j in range(item_count):
                    item = items.nth(j)
                    item_text = (item.text_content(timeout=2000) or "").strip()
                    if item_text.lower() == value_lower or value_lower in item_text.lower():
                        item.click(timeout=3000)
                        page.wait_for_timeout(500)
                        return f"selected: {item_text}"
                dd.click(timeout=2000)
                return f"no matching option for: {value}"
        except Exception as exc:
            logger.debug("_select_dropdown_by_number ctx failed: %s", exc)
            continue
    return False


def _fill_labeled_subfield(page: Any, label_text: str, value: str) -> bool:
    """Use Playwright locators to fill input associated with a <label> element."""
    clean = label_text.rstrip(":").strip()

    targets = [page]
    try:
        sm = page.frame(name="SMFrame")
        if sm:
            targets.insert(0, sm)
    except Exception:
        pass

    for ctx in targets:
        try:
            inp = ctx.get_by_label(clean, exact=False).first
            if inp.count() > 0:
                inp.scroll_into_view_if_needed()
                inp.click(timeout=3000)
                inp.fill(value, timeout=3000)
                return True
        except Exception:
            pass
        try:
            labels = ctx.locator("label")
            for i in range(labels.count()):
                lbl = labels.nth(i)
                lt = (lbl.text_content(timeout=2000) or "").replace("\u00a0", " ").replace(":", "").strip()
                if lt.lower() != clean.lower():
                    continue
                for_id = lbl.get_attribute("for")
                if for_id:
                    inp = ctx.locator(f"#{for_id}")
                    if inp.count() > 0:
                        inp.scroll_into_view_if_needed()
                        inp.click(timeout=3000)
                        inp.fill(value, timeout=3000)
                        return True
        except Exception:
            continue
    return False

def _select_labeled_dropdown(page: Any, label_text: str, value: str) -> str | bool:
    """Use Playwright locators to select a dropdown option near a <label> element."""
    clean = label_text.rstrip(":").strip().lower()
    value_lower = value.lower().strip()

    targets = [page]
    try:
        sm = page.frame(name="SMFrame")
        if sm:
            targets.insert(0, sm)
    except Exception:
        pass

    for ctx in targets:
        try:
            labels = ctx.locator("label")
            for i in range(labels.count()):
                lbl = labels.nth(i)
                lt = (lbl.text_content(timeout=2000) or "").replace("\u00a0", " ").replace(":", "").strip()
                if lt.lower() != clean:
                    continue
                tr = lbl.locator("xpath=ancestor::tr[1]")
                if tr.count() == 0:
                    continue
                dd = tr.locator('div.w-dropdown[role="combobox"]').first
                if dd.count() == 0:
                    continue
                dd.scroll_into_view_if_needed()
                dd.click(timeout=3000)
                page.wait_for_timeout(300)
                items = tr.locator('div.w-dropdown-item[role="option"]')
                item_count = items.count()
                for j in range(item_count):
                    item = items.nth(j)
                    item_text = (item.text_content(timeout=2000) or "").strip()
                    if item_text.lower() == value_lower or value_lower in item_text.lower():
                        item.click(timeout=3000)
                        page.wait_for_timeout(500)
                        return f"selected: {item_text}"
                dd.click(timeout=2000)
                return f"no matching option for: {value}"
        except Exception as exc:
            logger.debug("_select_labeled_dropdown ctx failed: %s", exc)
            continue
    return False


def create_browserbase_session() -> dict[str, Any]:
    """Create a Browserbase session only (no Playwright). Returns session_id, connect_url, live_url."""
    try:
        from browserbase import Browserbase
    except ImportError:
        return {
            "success": False,
            "session_id": None,
            "connect_url": None,
            "live_url": None,
            "message": "browserbase not installed",
            "error": "pip install browserbase",
        }
    api_key = os.environ.get("BROWSERBASE_API_KEY", "").strip()
    project_id = os.environ.get("BROWSERBASE_PROJECT_ID", "").strip()
    if not api_key or not project_id:
        return {
            "success": False,
            "session_id": None,
            "connect_url": None,
            "live_url": None,
            "message": "Browserbase not configured",
            "error": "Missing BROWSERBASE_API_KEY or BROWSERBASE_PROJECT_ID",
        }
    try:
        bb = Browserbase(api_key=api_key)
        session = bb.sessions.create(
            project_id=project_id,
            region=BROWSERBASE_REGION,
        )
        session_id = session.id
        connect_url = session.connect_url or ""
        if not connect_url:
            return {
                "success": False,
                "session_id": session_id,
                "connect_url": None,
                "live_url": None,
                "message": "Session created but no connect URL",
                "error": "Session object missing connect_url",
            }
        try:
            links = bb.sessions.debug(session_id)
            live_url = links.debugger_fullscreen_url or links.debugger_url or f"https://www.browserbase.com/sessions/{session_id}"
        except Exception as e:
            logger.warning("Could not get debugger URL: %s", e)
            live_url = f"https://www.browserbase.com/sessions/{session_id}"
        return {
            "success": True,
            "session_id": session_id,
            "connect_url": connect_url,
            "live_url": live_url,
            "message": "Session created",
        }
    except Exception as e:
        logger.exception("create_browserbase_session failed")
        return {
            "success": False,
            "session_id": None,
            "connect_url": None,
            "live_url": None,
            "message": str(e),
            "error": str(e),
        }


def run_ariba_form_fill(
    form_answers_json: str | None = None,
    navigation_timeout_ms: int = 60_000,
    on_session_ready: Callable[[str, str | None], None] | None = None,
    existing_session_id: str | None = None,
    existing_connect_url: str | None = None,
) -> dict[str, Any]:
    """
    Create a Browserbase session, connect via Playwright CDP,
    navigate to Ariba login URL, log in, then perform post-login steps.

    Returns:
        Dict with keys: success (bool), session_id (str | None), live_url (str | None),
        message (str), error (str | None).
    """
    try:
        from browserbase import Browserbase
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        missing = "playwright" if "playwright" in str(e).lower() else "browserbase"
        return {
            "success": False,
            "session_id": None,
            "live_url": None,
            "message": f"{missing} not installed in this environment",
            "error": f"Install with: pip install playwright browserbase. Original: {e}",
        }

    api_key = os.environ.get("BROWSERBASE_API_KEY", "").strip()
    project_id = os.environ.get("BROWSERBASE_PROJECT_ID", "").strip()
    ariba_url = os.environ.get("ARIBA_WEB_URL", "").strip()
    ariba_email = os.environ.get("ARIBA_WEB_EMAIL", "").strip()
    ariba_password = os.environ.get("ARIBA_WEB_PASSWORD", "").strip()

    if not api_key or not project_id:
        return {
            "success": False,
            "session_id": None,
            "live_url": None,
            "message": "Browserbase not configured",
            "error": "Missing BROWSERBASE_API_KEY or BROWSERBASE_PROJECT_ID",
        }
    if not ariba_url:
        return {
            "success": False,
            "session_id": None,
            "live_url": None,
            "message": "Ariba URL not configured",
            "error": "Missing ARIBA_WEB_URL",
        }

    session_id: str | None = None
    live_url: str | None = None
    connect_url: str | None = None
    playwright = None
    browser = None

    try:
        # ── Session setup ──
        if existing_session_id and existing_connect_url:
            session_id = existing_session_id
            connect_url = existing_connect_url
            live_url = f"https://www.browserbase.com/sessions/{session_id}"
        else:
            bb = Browserbase(api_key=api_key)
            session = bb.sessions.create(
                project_id=project_id,
                region=BROWSERBASE_REGION,
            )
            session_id = session.id
            connect_url = session.connect_url
            if not connect_url:
                return {
                    "success": False,
                    "session_id": session_id,
                    "live_url": None,
                    "message": "Session created but no connect URL",
                    "error": "Session object missing connect_url",
                }
            try:
                links = bb.sessions.debug(session_id)
                live_url = links.debugger_fullscreen_url or links.debugger_url
            except Exception as e:
                logger.warning("Could not get debugger URL: %s", e)
                live_url = f"https://www.browserbase.com/sessions/{session_id}"
            if not live_url and session_id:
                live_url = f"https://www.browserbase.com/sessions/{session_id}"

            if on_session_ready and session_id:
                try:
                    on_session_ready(session_id, live_url)
                except Exception as e:
                    logger.warning("on_session_ready callback failed: %s", e)

        # ── Connect Playwright ──
        playwright = sync_playwright().start()
        browser = playwright.chromium.connect_over_cdp(connect_url)
        context = browser.contexts[0] if browser.contexts else None
        page = context.pages[0] if context and context.pages else None
        if not page:
            page = context.new_page() if context else None
        if not page:
            return {
                "success": False,
                "session_id": session_id,
                "live_url": live_url,
                "message": "Could not get browser page",
                "error": "No page in context",
            }

        page.set_default_timeout(navigation_timeout_ms)
        page.set_default_navigation_timeout(navigation_timeout_ms)

        # ── Navigate to Ariba login ──
        page.goto(ariba_url, wait_until="domcontentloaded", timeout=navigation_timeout_ms)

        # ── Login ──
        if ariba_email and ariba_password:
            try:
                # Ariba uses <input name="UserName"> (no type, no placeholder, no label element)
                user_input = page.locator('input[name="UserName"]').first
                if user_input.count() == 0:
                    user_input = (
                        page.get_by_placeholder("User Name")
                        .or_(page.get_by_placeholder("Username"))
                        .first
                    )
                if user_input.count() > 0:
                    user_input.fill(ariba_email)
                    logger.info("Filled username")
                else:
                    logger.warning("Username field not found")

                # Ariba has a hidden decoy <input type="password" class="displayNone">
                # before the real one; target by id="Password" or name="Password"
                pwd_input = page.locator('input#Password, input[name="Password"]:not(.displayNone)').first
                if pwd_input.count() == 0:
                    pwd_input = page.locator('input[type="password"]:visible').first
                if pwd_input.count() > 0:
                    pwd_input.fill(ariba_password)
                    logger.info("Filled password")
                else:
                    logger.warning("Password field not found")

                # Submit is <input type="submit" value="Login"> not a <button>
                submit = page.locator(
                    'input[type="submit"][value="Login"], input[type="submit"], '
                    'button[type="submit"], [type="submit"]'
                ).first
                if submit.count() > 0:
                    submit.click()
                    page.wait_for_load_state("domcontentloaded", timeout=15000)
                    page.wait_for_load_state("load", timeout=15000)
                    try:
                        page.wait_for_load_state("networkidle", timeout=20000)
                    except Exception:
                        pass
                    logger.info("Login submitted. URL: %s", page.url)
                else:
                    logger.warning("Submit button not found")
            except Exception as e:
                logger.warning("Login step failed: %s", e)

        # ── Post-login: dismiss "additional info" banner if present ──
        try:
            dont_show_btn = page.locator('button:has-text("Don\'t show this to me again")').first
            dont_show_btn.wait_for(state="visible", timeout=8000)
            dont_show_btn.click(timeout=5000)
            page.wait_for_load_state("load", timeout=15000)
            logger.info("Dismissed 'Don't show this to me again' banner")
        except Exception:
            logger.info("No 'additional info' banner found, continuing")

        # ── Dump current page for inspection (disabled: _dump_page_html is no-op) ──
        # _dump_page_html(page, "after_login")

        # ── Fill form fields ──
        filled_count = 0
        failed_fields: list[str] = []

        if form_answers_json:
            try:
                answers = json.loads(form_answers_json) if isinstance(form_answers_json, str) else form_answers_json
            except json.JSONDecodeError:
                answers = []
                logger.warning("Could not parse form_answers_json")

            if isinstance(answers, list) and answers:
                label_map, number_map = _load_form_maps()
                logger.info(
                    "Filling %d form answers (label_map=%d, number_map=%d)",
                    len(answers), len(label_map), len(number_map),
                )

                for entry in answers:
                    form_name = entry.get("externalSystemCorrelationId") or entry.get("formName", "")
                    value = str(entry.get("answer", "")).strip()
                    if not form_name or not value:
                        continue

                    q_number = number_map.get(form_name)
                    label = label_map.get(form_name, "")
                    is_address_sub = form_name in _ADDRESS_SUBFIELD_NAMES

                    logger.info(
                        "Filling field %s (number=%s, label=%s, address=%s) = %s",
                        form_name, q_number, label, is_address_sub, value,
                    )

                    success = False

                    if is_address_sub and label:
                        if _fill_labeled_subfield(page, label, value):
                            success = True
                            logger.info("  Filled address subfield '%s' via label", label)
                        else:
                            result = _select_labeled_dropdown(page, label, value)
                            if result and isinstance(result, str) and result.startswith("selected:"):
                                success = True
                                logger.info("  Selected address dropdown '%s': %s", label, result)
                            else:
                                logger.warning("  Address subfield '%s' not filled: %s", label, result)
                    elif q_number:
                        if _fill_text_by_number(page, q_number, label, value):
                            success = True
                            logger.info("  Filled text field %s", q_number)
                        else:
                            result = _select_dropdown_by_number(page, q_number, value)
                            if result and isinstance(result, str) and result.startswith("selected:"):
                                success = True
                                logger.info("  Selected dropdown %s: %s", q_number, result)
                            else:
                                logger.warning("  Field %s (%s) not filled: %s", q_number, form_name, result)
                    else:
                        if label:
                            if _fill_labeled_subfield(page, label, value):
                                success = True
                                logger.info("  Filled field '%s' via label fallback", label)
                            else:
                                result = _select_labeled_dropdown(page, label, value)
                                if result and isinstance(result, str) and result.startswith("selected:"):
                                    success = True
                                    logger.info("  Selected dropdown '%s' via label fallback: %s", label, result)

                    if success:
                        filled_count += 1
                    else:
                        failed_fields.append(form_name)
                        logger.warning("  FAILED to fill: %s (number=%s, label=%s)", form_name, q_number, label)

                logger.info("Form fill complete: %d/%d fields filled", filled_count, len(answers))
                if failed_fields:
                    logger.warning("Failed fields: %s", failed_fields)

        # ── Determine whether all required fields are covered ──
        submitted = False
        saved_draft = False
        provided_form_names: set[str] = set()
        if form_answers_json:
            try:
                answers_check = json.loads(form_answers_json) if isinstance(form_answers_json, str) else form_answers_json
            except Exception:
                answers_check = []
            if isinstance(answers_check, list):
                for entry in answers_check:
                    fn = entry.get("externalSystemCorrelationId") or entry.get("formName", "")
                    val = str(entry.get("answer", "")).strip()
                    if fn and val:
                        provided_form_names.add(fn)

        missing_required = _REQUIRED_FORM_NAMES - provided_form_names
        all_required_filled = len(missing_required) == 0 and filled_count > 0

        if all_required_filled:
            logger.info("All %d required fields provided — submitting entire response", len(_REQUIRED_FORM_NAMES))
        else:
            if missing_required:
                logger.info(
                    "Missing %d required fields — will save draft instead: %s",
                    len(missing_required),
                    sorted(missing_required),
                )
            elif filled_count == 0:
                logger.info("No fields were filled — will save draft")

        if filled_count > 0:
            if all_required_filled:
                # ── Submit Entire Response ──
                try:
                    submit_btn = page.locator('button[title="Submit Entire Response"]').first
                    submit_btn.scroll_into_view_if_needed()
                    submit_btn.click(timeout=10000)
                    logger.info("Clicked 'Submit Entire Response'")

                    try:
                        ok_btn = page.locator(
                            '[role="dialog"]:has-text("Submit this response") button:has-text("OK"), '
                            '.w-dlg-buttons button:has-text("OK")'
                        ).first
                        ok_btn.wait_for(state="visible", timeout=10000)
                        ok_btn.click(timeout=5000)
                        logger.info("Clicked OK in submit confirmation dialog")
                        page.wait_for_timeout(3000)
                        submitted = True
                    except Exception as e:
                        logger.warning("Could not click OK in confirmation dialog: %s", e)
                except Exception as e:
                    logger.warning("Could not click Submit Entire Response: %s", e)
            else:
                # ── Save Draft ──
                try:
                    draft_btn = page.locator(
                        'button[title="Save your response; it will not be submitted to the owner"], '
                        'button:has-text("Save draft")'
                    ).first
                    draft_btn.scroll_into_view_if_needed()
                    draft_btn.click(timeout=10000)
                    logger.info("Clicked 'Save draft' (not all required fields filled)")
                    page.wait_for_timeout(3000)
                    saved_draft = True
                except Exception as e:
                    logger.warning("Could not click Save draft: %s", e)

        # _dump_page_html(page, "after_fill")

        action = "Submitted" if submitted else ("Saved draft" if saved_draft else "No action")
        logger.info(
            "Session done. View replay: https://www.browserbase.com/sessions/%s",
            session_id,
        )
        return {
            "success": True,
            "session_id": session_id,
            "live_url": live_url,
            "message": (
                f"Filled {filled_count} fields. {action}. "
                f"Failed: {failed_fields}. Missing required: {sorted(missing_required)}"
            ),
            "error": None,
        }
    except Exception as e:
        logger.exception("ariba_form_fill failed")
        return {
            "success": False,
            "session_id": session_id,
            "live_url": live_url,
            "message": str(e),
            "error": str(e),
        }
    finally:
        if browser:
            try:
                browser.close()
            except Exception:
                pass
        if playwright:
            try:
                playwright.stop()
            except Exception:
                pass


if __name__ == "__main__":
    """CLI entrypoint: read stdin JSON, run form fill, print JSON result."""
    import sys
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stderr,
    )
    raw = sys.stdin.read().strip() if not sys.stdin.isatty() else ""
    form_answers_json = None
    existing_session_id = None
    existing_connect_url = None
    if raw:
        try:
            payload = json.loads(raw)
            if isinstance(payload, dict):
                form_answers_json = payload.get("form_answers_json")
                if isinstance(form_answers_json, list):
                    form_answers_json = json.dumps(form_answers_json)
                existing_session_id = payload.get("session_id")
                existing_connect_url = payload.get("connect_url")
            else:
                form_answers_json = raw
        except json.JSONDecodeError:
            form_answers_json = raw
    logger.info(
        "Stdin: form_answers_json length=%s, existing_session=%s",
        len(form_answers_json) if form_answers_json else 0,
        bool(existing_session_id and existing_connect_url),
    )
    result = run_ariba_form_fill(
        form_answers_json=form_answers_json,
        existing_session_id=existing_session_id or None,
        existing_connect_url=existing_connect_url or None,
    )
    print(json.dumps(result), flush=True)
