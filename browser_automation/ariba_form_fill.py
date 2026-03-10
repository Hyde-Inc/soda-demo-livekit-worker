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
from typing import Any

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


def run_ariba_form_fill(
    form_answers_json: str | None = None,
    navigation_timeout_ms: int = 60_000,
) -> dict[str, Any]:
    """
    Create a Browserbase session (region ap-southeast-1), connect via Playwright CDP,
    navigate to Ariba login URL, log in with ARIBA_WEB_EMAIL / ARIBA_WEB_PASSWORD,
    and optionally fill form fields from form_answers_json.

    Args:
        form_answers_json: Optional JSON string - list of {"externalSystemCorrelationId": str, "answer": str}.
        navigation_timeout_ms: Timeout for page loads (default 60s).

    Returns:
        Dict with keys: success (bool), session_id (str | None), live_url (str | None),
        message (str), error (str | None), last_page_url (str | None), last_page_html (str | None),
        last_frame_html (str | None). Last-page fields hold the final URL and HTML (main page and
        SMFrame iframe) so the caller can inspect and guide next common steps.
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
    playwright = None
    browser = None

    try:
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

        # Debug: dump URL and page HTML after login for manual inspection
        try:
            url_after_login = page.url
            html_after_login = page.content()
            logger.info("After login — URL: %s", url_after_login)
            snippet_len = 1500
            logger.info("After login — HTML snippet (first %d chars):\n%s", snippet_len, html_after_login[:snippet_len] if html_after_login else "(empty)")
            logs_dir = Path(__file__).resolve().parent.parent / "logs"
            logs_dir.mkdir(parents=True, exist_ok=True)
            dump_name = f"ariba_after_login_{int(time.time())}.html"
            dump_path = logs_dir / dump_name
            dump_path.write_text(html_after_login, encoding="utf-8")
            logger.info("After login — full HTML saved to: %s", dump_path)
        except Exception as e:
            logger.warning("Could not dump post-login HTML: %s", e)

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

        # Click "Submit Entire Response" after filling (page4: button in specialButtonRow, title="Submit Entire Response")
        if form_was_filled:
            try:
                submit_btn = None
                for loc in [
                    frame.locator('button[title="Submit Entire Response"]').first,
                    frame.locator('button.w-btn-primary:has-text("Submit Entire Response")').first,
                    frame.locator('button:has-text("Submit Entire Response")').first,
                    page.locator('button[title="Submit Entire Response"]').first,
                    page.locator('button.w-btn-primary:has-text("Submit Entire Response")').first,
                    page.locator('button:has-text("Submit Entire Response")').first,
                ]:
                    try:
                        loc.wait_for(state="visible", timeout=8000)
                        submit_btn = loc
                        break
                    except Exception:
                        continue
                if submit_btn:
                    submit_btn.scroll_into_view_if_needed(timeout=5000)
                    submit_btn.click(timeout=5000)
                    page.wait_for_timeout(2000)
                    logger.info("Clicked Submit Entire Response")
                else:
                    logger.warning("Submit Entire Response button not found")
            except Exception as e:
                logger.warning("Could not click Submit Entire Response: %s", e)

        # Capture last page URL and HTML so caller can inspect and guide next steps
        last_page_url: str | None = None
        last_page_html: str | None = None
        last_frame_html: str | None = None
        try:
            last_page_url = page.url
            last_page_html = page.content()
            sm_frame = page.frame(name="SMFrame")
            if sm_frame:
                last_frame_html = sm_frame.content()
        except Exception as e:
            logger.debug("Could not capture last page HTML: %s", e)

        logger.info(
            "Session done. View replay & steps: https://www.browserbase.com/sessions/%s",
            session_id,
        )
        return {
            "success": True,
            "session_id": session_id,
            "live_url": live_url,
            "message": "Browser session started; Ariba login and form fill attempted.",
            "error": None,
            "last_page_url": last_page_url,
            "last_page_html": last_page_html,
            "last_frame_html": last_frame_html,
        }
    except Exception as e:
        logger.exception("ariba_form_fill failed")
        return {
            "success": False,
            "session_id": session_id,
            "live_url": live_url,
            "message": str(e),
            "error": str(e),
            "last_page_url": None,
            "last_page_html": None,
            "last_frame_html": None,
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
    """CLI entrypoint: read form_answers_json from stdin, run form fill, print JSON result. Used for subprocess so Browserbase can complete after worker exits. Logs go to stderr (parent may redirect to a file)."""
    import sys
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stderr,
    )
    raw = sys.stdin.read().strip() if not sys.stdin.isatty() else ""
    form_answers_json = raw if raw else None
    logger.info("Received form_answers_json from stdin (length=%s): %s", len(raw) if raw else 0, form_answers_json)
    result = run_ariba_form_fill(form_answers_json=form_answers_json)
    print(json.dumps(result), flush=True)
