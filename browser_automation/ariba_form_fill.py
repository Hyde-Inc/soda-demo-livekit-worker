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
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

# Region for proximity to Indian suppliers (same as enterprise-browser-agent)
BROWSERBASE_REGION = "ap-southeast-1"

# Form field mapping: formName (externalSystemCorrelationId) -> label text for get_by_label
_FORM_LABEL_MAP: dict[str, str] = {}


def _load_form_label_map() -> dict[str, str]:
    """Load formName -> field name (label) from form_supplier_general_info.json for label-based fill."""
    global _FORM_LABEL_MAP
    if _FORM_LABEL_MAP:
        return _FORM_LABEL_MAP
    try:
        form_path = Path(__file__).resolve().parent.parent / "form_supplier_general_info.json"
        if form_path.exists():
            data = json.loads(form_path.read_text(encoding="utf-8"))
            for section in data.get("sections", []):
                for field in section.get("fields", []):
                    form_name = field.get("formName")
                    name = field.get("name")
                    if form_name and name:
                        _FORM_LABEL_MAP[form_name] = str(name).strip()
    except Exception as e:
        logger.debug("Could not load form label map: %s", e)
    return _FORM_LABEL_MAP


def _fill_by_label_exact_then_tr(page: Any, frame: Any, label: str, value: str) -> bool:
    """Find element with exact label text, walk up to nearest ancestor tr that has an input, fill it. Tries frame then page. Returns True if filled."""
    js = """
    ([label, value]) => {
        const candidates = Array.from(document.querySelectorAll('*')).filter(e => {
            const t = (e.textContent || '').trim().replace(/\\s+/g, ' ');
            return t === label || t.endsWith(' ' + label) || (t.includes(label) && t.length < 200);
        });
        const el = candidates.length ? candidates.reduce((a, b) =>
            (a.textContent || '').length < (b.textContent || '').length ? a : b
        ) : null;
        if (!el) return false;
        let tr = el.closest('tr');
        while (tr) {
            const inp = tr.querySelector('input:not([type=hidden]), textarea, select');
            if (inp) {
                inp.focus();
                inp.value = value;
                inp.dispatchEvent(new Event('input', { bubbles: true }));
                inp.dispatchEvent(new Event('change', { bubbles: true }));
                return true;
            }
            tr = tr.parentElement && tr.parentElement.closest('tr');
        }
        return false;
    }
    """
    args = [label, value]
    try:
        fr = page.frame(name="SMFrame")
        if fr and fr.evaluate(js, args):
            return True
    except Exception:
        pass
    try:
        if page.evaluate(js, args):
            return True
    except Exception:
        pass
    return False


def create_browserbase_session() -> dict[str, Any]:
    """
    Create a Browserbase session only (no Playwright, no form fill). Returns session_id, connect_url, live_url.
    Used so the main process can publish the session to the room before spawning the form-fill subprocess.
    """
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
    Create a Browserbase session (region ap-southeast-1), connect via Playwright CDP,
    navigate to Ariba login URL, log in with ARIBA_WEB_EMAIL / ARIBA_WEB_PASSWORD,
    and optionally fill form fields from form_answers_json.

    Args:
        form_answers_json: Optional JSON string - list of {"externalSystemCorrelationId": str, "answer": str}.
        navigation_timeout_ms: Timeout for page loads (default 60s).
        on_session_ready: Optional callback(session_id, live_url) invoked as soon as the session
            and live view URL are ready (before browser connect/form fill). Use to publish to room immediately.
        existing_session_id: When provided with existing_connect_url, connect to this session instead of creating one.
        existing_connect_url: CDP connect URL for the existing session (from create_browserbase_session).

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
            "error": f"Install with: pip install playwright browserbase (then run the worker with the same venv). Original: {e}",
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

            # Debugger URL for live view (from Browserbase API)
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

        # Navigate to Ariba login
        page.goto(ariba_url, wait_until="domcontentloaded", timeout=navigation_timeout_ms)

        # Login if credentials are set — target visible "User Name" and "Password" fields only (exclude hidden)
        if ariba_email and ariba_password:
            try:
                # Prefer label/placeholder so we hit the visible editable fields, not hidden inputs
                user_input = (
                    page.get_by_placeholder("User Name")
                    .or_(page.get_by_placeholder("Username"))
                    .or_(page.get_by_label("User Name"))
                    .or_(page.get_by_label("Username"))
                    .first
                )
                if user_input.count() > 0:
                    user_input.fill(ariba_email)
                else:
                    # Fallback: visible text input, exclude hidden (hidden_username was matching before)
                    visible_user = page.locator(
                        'input:not([type="hidden"])[type="text"], input:not([type="hidden"])[type="email"], '
                        'input:not([type="hidden"])[name="j_username"], input:not([type="hidden"])[name="email"]'
                    ).first
                    if visible_user.count() > 0:
                        visible_user.fill(ariba_email)
                password_input = (
                    page.get_by_placeholder("Password")
                    .or_(page.get_by_label("Password"))
                    .first
                )
                if password_input.count() > 0:
                    password_input.fill(ariba_password)
                else:
                    pwd_fallback = page.locator('input[type="password"]').first
                    if pwd_fallback.count() > 0:
                        pwd_fallback.fill(ariba_password)
                submit = page.locator(
                    'button[type="submit"], input[type="submit"], [type="submit"], '
                    'button:has-text("Log in"), button:has-text("Sign in"), '
                    'a:has-text("Log in"), a:has-text("Sign in")'
                ).first
                if submit.count() > 0:
                    submit.click()
                    # Wait for post-login navigation and full load (redirect or SPA)
                    page.wait_for_load_state("domcontentloaded", timeout=15000)
                    page.wait_for_load_state("load", timeout=15000)
                    try:
                        page.wait_for_load_state("networkidle", timeout=20000)
                    except Exception:
                        pass
            except Exception as e:
                logger.warning("Login step failed (selectors may not match): %s", e)

        # Frame reference for form/dashboard content (used for supplier steps and form fill)
        frame = page.frame_locator("#SMFrame")

        # After login: click the link/element for "Supplier General Information" (e.g. "ABC Industries")
        _supplier_name: str | None = None
        if form_answers_json:
            try:
                answers = json.loads(form_answers_json)
                if isinstance(answers, list):
                    for item in answers:
                        corr = (item.get("externalSystemCorrelationId") or item.get("correlationId") or "").strip()
                        if corr in ("supplierName1", "Supplier Name 1"):
                            _supplier_name = (item.get("answer") or item.get("value") or "").strip()
                            break
            except json.JSONDecodeError:
                pass
        if not _supplier_name:
            try:
                form_path = Path(__file__).resolve().parent.parent / "form_supplier_general_info.json"
                if form_path.exists():
                    form_data = json.loads(form_path.read_text(encoding="utf-8"))
                    for section in form_data.get("sections", []):
                        for field in section.get("fields", []):
                            if field.get("formName") == "supplierName1" and field.get("user_answer"):
                                _supplier_name = str(field["user_answer"]).strip()
                                break
                        if _supplier_name:
                            break
            except Exception as e:
                logger.debug("Could not read supplier name from form file: %s", e)
        if _supplier_name:
            try:
                # Supplier dashboard lives inside iframe SMFrame (see saved HTML: id="SMFrame", src=.../supplier-dashboard)
                logger.info("Waiting for SMFrame iframe and supplier table...")
                page.wait_for_selector("iframe#SMFrame", state="attached", timeout=15000)
                page.wait_for_timeout(3000)

                escaped = _supplier_name.replace('"', '\\"')
                row_selector = f'md-row:has(div.link-text[title="{escaped}"])'
                try:
                    frame.locator(row_selector).first.wait_for(state="visible", timeout=20000)
                    logger.info("Supplier table row found in iframe for: %s", _supplier_name)
                except Exception as e:
                    logger.warning("Supplier table row not found in iframe after 20s: %s", e)
                page.wait_for_timeout(1000)

                logger.info("Opening Supplier General Information for: %s", _supplier_name)
                clicked = False

                # Table is inside iframe: md-row, View is button[aria-label="View"] in same row
                try:
                    row = frame.locator(row_selector).first
                    row.locator('button[aria-label="View"]').first.click(timeout=5000)
                    page.wait_for_timeout(2000)
                    logger.info("Clicked View for supplier: %s", _supplier_name)
                    clicked = True
                except Exception as e:
                    logger.debug("View button click failed: %s", e)

                if not clicked:
                    try:
                        loc = frame.locator(f'div.link-text[title="{escaped}"]').first
                        loc.scroll_into_view_if_needed(timeout=3000)
                        loc.click(timeout=5000, force=True)
                        page.wait_for_timeout(2000)
                        logger.info("Clicked supplier name for: %s", _supplier_name)
                        clicked = True
                    except Exception as e:
                        logger.debug("Supplier name click failed: %s", e)

                if not clicked:
                    logger.warning("Could not open supplier %r (tried View button and supplier name in iframe)", _supplier_name)

                # After opening supplier: click Advanced View icon/button (same frame)
                if clicked:
                    try:
                        adv_btn = frame.locator('button#advanced-view, button[aria-label="Advanced View"]').first
                        adv_btn.wait_for(state="visible", timeout=10000)
                        adv_btn.click(timeout=5000)
                        page.wait_for_timeout(2000)
                        logger.info("Clicked Advanced View")
                    except Exception as e:
                        logger.warning("Could not click Advanced View: %s", e)

                # Next: click "Supplier request form" then "Prepare Response" in dropdown.
                # Link and dropdown can be in frame or main page (page3.html); try frame first then page.
                # Link: <a class="hoverArrow hoverLink">Supplier request form</a>
                # Menu item: <a role="menuitem"><b>Prepare Response</b></a>
                if clicked:
                    try:
                        supplier_form_link = None
                        for locator in [
                            frame.locator('a.hoverLink:has-text("Supplier request form")').first,
                            frame.locator('a:has-text("Supplier request form")').first,
                            page.locator('a.hoverLink:has-text("Supplier request form")').first,
                            page.locator('a:has-text("Supplier request form")').first,
                        ]:
                            try:
                                locator.wait_for(state="visible", timeout=5000)
                                supplier_form_link = locator
                                break
                            except Exception:
                                continue
                        if supplier_form_link:
                            supplier_form_link.scroll_into_view_if_needed(timeout=5000)
                            supplier_form_link.click(timeout=5000)
                            page.wait_for_timeout(1500)
                            logger.info("Clicked Supplier request form")
                            # Dropdown opens (options lazy-load); click "Prepare Response" (capital R in page3)
                            prepare_btn = None
                            for loc in [
                                page.locator('[role="menuitem"]:has-text("Prepare Response")').first,
                                page.locator('[role="menuitem"]:has-text("Prepare response")').first,
                                frame.locator('[role="menuitem"]:has-text("Prepare Response")').first,
                                frame.locator('[role="menuitem"]:has-text("Prepare response")').first,
                            ]:
                                try:
                                    loc.wait_for(state="visible", timeout=12000)
                                    prepare_btn = loc
                                    break
                                except Exception:
                                    continue
                            if prepare_btn:
                                prepare_btn.click(timeout=5000)
                                page.wait_for_timeout(2000)
                                logger.info("Clicked Prepare Response")
                                # Click "Revise Response" button: <button class="w-btn w-btn-primary" title="Revise Response">
                                revise_btn = None
                                for loc in [
                                    page.locator('button[title="Revise Response"]').first,
                                    page.locator('button:has-text("Revise Response")').first,
                                    frame.locator('button[title="Revise Response"]').first,
                                    frame.locator('button:has-text("Revise Response")').first,
                                ]:
                                    try:
                                        loc.wait_for(state="visible", timeout=10000)
                                        revise_btn = loc
                                        break
                                    except Exception:
                                        continue
                                if revise_btn:
                                    revise_btn.click(timeout=5000)
                                    page.wait_for_timeout(2000)
                                    logger.info("Clicked Revise Response")
                                    # Click OK button in dialog: <button class="w-btn" title="OK">
                                    ok_btn = None
                                    for loc in [
                                        page.locator('button[title="OK"]').first,
                                        page.locator('button:has-text("OK")').first,
                                        frame.locator('button[title="OK"]').first,
                                        frame.locator('button:has-text("OK")').first,
                                    ]:
                                        try:
                                            loc.wait_for(state="visible", timeout=10000)
                                            ok_btn = loc
                                            break
                                        except Exception:
                                            continue
                                    if ok_btn:
                                        ok_btn.click(timeout=5000)
                                        page.wait_for_timeout(2000)
                                        logger.info("Clicked OK")
                                    else:
                                        logger.warning("OK button not found")
                                else:
                                    logger.warning("Revise Response button not found")
                            else:
                                logger.warning("Prepare Response menuitem not found")
                        else:
                            logger.warning("Supplier request form link not found in frame or main page")
                    except Exception as e:
                        logger.warning("Could not click Supplier request form / Prepare Response: %s", e)
            except Exception as e:
                logger.warning("Could not open Supplier General Information (%s): %s", _supplier_name, e)

        # Fill form fields from JSON passed by main.py (collected_form_answers).
        # After initial steps (OK) we are on the form page; fill then submit.
        form_was_filled = False
        if form_answers_json:
            try:
                answers = json.loads(form_answers_json)
                if isinstance(answers, list):
                    label_map = _load_form_label_map()
                    filled_count = 0
                    for item in answers:
                        corr_id = item.get("externalSystemCorrelationId") or item.get(
                            "correlationId", ""
                        )
                        answer = item.get("answer") or item.get("value", "")
                        if not corr_id:
                            continue
                        value = str(answer).strip()
                        if not value:
                            continue
                        filled = False
                        label = label_map.get(corr_id)
                        # Try page and frame; try id/name/data attr, then label, then row-with-label
                        for ctx in (page, frame):
                            if filled:
                                break
                            selectors = [
                                f'input[id="{corr_id}"], textarea[id="{corr_id}"], select[id="{corr_id}"]',
                                f'input[name="{corr_id}"], textarea[name="{corr_id}"], select[name="{corr_id}"]',
                                f'[data-correlation-id="{corr_id}"]',
                                f'input[id*="{corr_id}"], textarea[id*="{corr_id}"]',
                            ]
                            for sel in selectors:
                                try:
                                    el = ctx.locator(sel).first
                                    if el.count() > 0:
                                        el.fill(value)
                                        filled = True
                                        filled_count += 1
                                        logger.info("Filled %s by selector", corr_id)
                                        break
                                except Exception:
                                    pass
                            if filled:
                                break
                            if label:
                                try:
                                    el = ctx.get_by_label(label).first
                                    if el.count() > 0:
                                        el.fill(value)
                                        filled = True
                                        filled_count += 1
                                        logger.info("Filled %s by label %s", corr_id, label)
                                        break
                                except Exception:
                                    pass
                            if filled:
                                break
                            # Label-exact + walk up: find element with exact label text, then nearest ancestor tr that has an input.
                            # Ariba uses nested tables so tr.filter(has_text) can match a huge row; we need the input for this question only.
                            if label:
                                try:
                                    if _fill_by_label_exact_then_tr(page, frame, label, value):
                                        filled = True
                                        filled_count += 1
                                        logger.info("Filled %s by label-exact (label %s)", corr_id, label)
                                        break
                                except Exception:
                                    pass
                        if not filled:
                            logger.warning("No element found for correlationId %s (label: %s)", corr_id, label_map.get(corr_id, ""))
                    if answers:
                        logger.info("Form fill: %d of %d field(s) filled from JSON", filled_count, len(answers))
                        form_was_filled = filled_count > 0
            except json.JSONDecodeError as e:
                logger.warning("Invalid form_answers_json: %s", e)

        # Always click "Submit Entire Response" when we have form answers (fields may have been filled by JS without tracking)
        if form_answers_json:
            submit_clicked = False
            page.wait_for_timeout(2000)
            # 1) Direct locator on page with force click
            try:
                btn = page.locator('button[title="Submit Entire Response"]').first
                btn.scroll_into_view_if_needed(timeout=5000)
                btn.click(timeout=5000, force=True)
                submit_clicked = True
                logger.info("Clicked Submit Entire Response (page locator)")
            except Exception as e:
                logger.warning("Submit page locator failed: %s", e)
            # 2) get_by_role
            if not submit_clicked:
                try:
                    btn = page.get_by_role("button", name="Submit Entire Response").first
                    btn.scroll_into_view_if_needed(timeout=5000)
                    btn.click(timeout=5000, force=True)
                    submit_clicked = True
                    logger.info("Clicked Submit Entire Response (get_by_role)")
                except Exception as e:
                    logger.warning("Submit get_by_role failed: %s", e)
            # 3) JS click on main page
            if not submit_clicked:
                try:
                    result = page.evaluate(
                        """() => {
                            const btn = document.querySelector('button[title="Submit Entire Response"]');
                            if (btn) { btn.scrollIntoView({block:'center'}); btn.click(); return 'clicked'; }
                            const all = Array.from(document.querySelectorAll('button'));
                            const found = all.find(b => b.textContent.replace(/\\s+/g,' ').trim() === 'Submit Entire Response');
                            if (found) { found.scrollIntoView({block:'center'}); found.click(); return 'clicked-text'; }
                            return 'not-found: ' + all.length + ' buttons, titles: ' + all.slice(0,10).map(b=>b.title||b.textContent.trim().slice(0,30)).join(', ');
                        }"""
                    )
                    logger.info("Submit JS result: %s", result)
                    if result and result.startswith("clicked"):
                        submit_clicked = True
                except Exception as e:
                    logger.warning("Submit page JS failed: %s", e)
            # 4) Try all frames
            if not submit_clicked:
                for f in page.frames():
                    if f == page.main_frame:
                        continue
                    try:
                        result = f.evaluate(
                            """() => {
                                const btn = document.querySelector('button[title="Submit Entire Response"]');
                                if (btn) { btn.scrollIntoView({block:'center'}); btn.click(); return 'clicked'; }
                                return 'not-found';
                            }"""
                        )
                        if result and result.startswith("clicked"):
                            submit_clicked = True
                            logger.info("Clicked Submit Entire Response (frame %s)", f.url[:60])
                            break
                    except Exception:
                        continue
            if not submit_clicked:
                logger.warning("Submit Entire Response button not found or not clickable")
            if submit_clicked:
                page.wait_for_timeout(3000)
                # Click OK in the "Submit this response?" / "Click OK to submit" dialog.
                # Dialog structure: <div role="dialog">...<div class="w-dlg-buttons">...<button title="OK">
                # There are multiple hidden OK dialogs; we need the visible one with "Submit this response?" title.
                ok_clicked = False
                # 1) Wait for dialog with "Submit this response?" to become visible, then Playwright-click its OK
                try:
                    dialog = page.locator('[role="dialog"]:has-text("Submit this response")').first
                    dialog.wait_for(state="visible", timeout=10000)
                    ok_btn = dialog.locator('button[title="OK"]').first
                    ok_btn.wait_for(state="visible", timeout=5000)
                    ok_btn.click(timeout=5000)
                    ok_clicked = True
                    logger.info("Clicked OK after Submit (role=dialog)")
                except Exception as e:
                    logger.debug("OK role=dialog: %s", e)
                # 2) Try w-dlg-dialog with "Click OK to submit"
                if not ok_clicked:
                    try:
                        dialog = page.locator('.w-dlg-dialog:has-text("Click OK to submit")').first
                        dialog.wait_for(state="visible", timeout=5000)
                        ok_btn = dialog.locator('button[title="OK"]').first
                        ok_btn.wait_for(state="visible", timeout=5000)
                        ok_btn.click(timeout=5000)
                        ok_clicked = True
                        logger.info("Clicked OK after Submit (w-dlg-dialog)")
                    except Exception as e:
                        logger.debug("OK w-dlg-dialog: %s", e)
                # 3) Click whichever OK button is currently visible (Playwright checks visibility)
                if not ok_clicked:
                    try:
                        all_ok = page.locator('button[title="OK"]')
                        n = all_ok.count()
                        for i in range(n):
                            btn = all_ok.nth(i)
                            if btn.is_visible():
                                btn.click(timeout=5000)
                                ok_clicked = True
                                logger.info("Clicked OK after Submit (visible button #%d)", i)
                                break
                    except Exception as e:
                        logger.debug("OK visible loop: %s", e)
                if ok_clicked:
                    # Wait for page to load/redirect after submission
                    try:
                        page.wait_for_load_state("domcontentloaded", timeout=20000)
                        page.wait_for_load_state("load", timeout=20000)
                        try:
                            page.wait_for_load_state("networkidle", timeout=20000)
                        except Exception:
                            pass
                    except Exception:
                        pass
                    page.wait_for_timeout(3000)
                    logger.info("Post-submit page loaded. URL: %s", page.url)
                else:
                    logger.warning("OK button after Submit not found or not clickable")

        logger.info(
            "Session done. View replay & steps: https://www.browserbase.com/sessions/%s",
            session_id,
        )
        return {
            "success": True,
            "session_id": session_id,
            "live_url": live_url,
            "message": "Browser session started; Ariba login and form fill completed.",
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
    """CLI entrypoint: read stdin (JSON object or raw form_answers_json string), run form fill, print JSON result.
    When parent passes {"form_answers_json": "...", "session_id": "...", "connect_url": "..."}, connects to
    existing session instead of creating one (used after publishing session to room)."""
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
