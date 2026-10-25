import os
import logging
from datetime import datetime, timedelta
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
import httpx

logger = logging.getLogger(__name__)

LOGIN_URL = "https://www.buildinglink.com/V2/Tenant/Login.aspx"
BOOKING_URL = "https://www.buildinglink.com/v2/tenant/amenities/newreservation.aspx?amenityId=29916&from=0&selectedDate="
MYRES_URL = "https://www.buildinglink.com/V2/Tenant/Amenities/MyReservations.aspx?from=0"

BL_USERNAME = os.environ.get("BL_USERNAME", "")
BL_PASSWORD = os.environ.get("BL_PASSWORD", "")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")


def send_screenshot(chat_id, path, caption=""):
    if not TELEGRAM_TOKEN or not chat_id:
        return
    try:
        with open(path, "rb") as f:
            httpx.post(
                "https://api.telegram.org/bot" + TELEGRAM_TOKEN + "/sendPhoto",
                data={"chat_id": chat_id, "caption": caption},
                files={"photo": f},
                timeout=15
            )
    except Exception as e:
        logger.error("Screenshot send failed: " + str(e))


def to_12h(hour, minute):
    period = "AM" if hour < 12 else "PM"
    h = hour % 12
    if h == 0:
        h = 12
    return str(h) + ":" + str(minute).zfill(2) + " " + period


def to_bl_datetime(date_str, hour, minute):
    # BuildingLink format: "2026-06-11-13-00-00"
    return date_str + "-" + str(hour).zfill(2) + "-" + str(minute).zfill(2) + "-00"


MAX_FALLBACK_SLOTS = 4   # after the requested time, try up to this many later hourly slots
WINDOW_END_HOUR = 21     # court available until 9 PM (last start 8 PM)


def book_tennis_court(date_str, hour, minute, chat_id=None):
    logger.info("Starting booking: " + date_str + " " + str(hour).zfill(2) + ":" + str(minute).zfill(2))
    steps = []

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
        )
        context = browser.new_context(viewport={"width": 1280, "height": 900})
        page = context.new_page()

        try:
            # Login once - session/cookies reused for all attempts
            steps.append("1. Loading login page")
            page.goto(LOGIN_URL, timeout=60000)
            page.wait_for_load_state("domcontentloaded", timeout=60000)

            steps.append("2. Filling credentials")
            page.fill("input[name='Username'], input[id*='Username']", BL_USERNAME)
            page.fill("input[name='Password'], input[id*='Password']", BL_PASSWORD)

            steps.append("3. Submitting login")
            page.press("input[name='Password'], input[id*='Password']", "Enter")
            page.wait_for_load_state("domcontentloaded", timeout=30000)
            page.wait_for_timeout(3000)
            steps.append("3b. Post-login URL: " + page.url)

            # Build list of times to try: requested, then subsequent hourly slots
            candidate_hours = [hour]
            h = hour + 1
            while len(candidate_hours) <= MAX_FALLBACK_SLOTS and h < WINDOW_END_HOUR:
                candidate_hours.append(h)
                h += 1

            attempts_log = []
            for idx, try_hour in enumerate(candidate_hours):
                is_primary = (idx == 0)
                label = "requested" if is_primary else "fallback"
                steps.append("Attempt " + str(idx + 1) + " (" + label + "): " + to_12h(try_hour, minute))
                result = _attempt_slot(page, context, date_str, try_hour, minute, steps, chat_id)

                if result.get("success") and result.get("verified") is True:
                    # Secured this slot
                    result["booked_hour"] = try_hour
                    result["booked_minute"] = minute
                    result["was_fallback"] = not is_primary
                    result["requested_hour"] = hour
                    return result

                # Record why this attempt did not stick, then try next slot
                reason = result.get("error") or result.get("note") or "unavailable"
                attempts_log.append(to_12h(try_hour, minute) + ": " + reason[:80])

                # If verification GET itself failed (ambiguous), stop - do not spam attempts
                if result.get("success") and result.get("verified") is None:
                    result["attempts_log"] = attempts_log
                    return result

            # Nothing worked across all candidates
            return {
                "success": False,
                "error": "Could not secure any slot. Tried: " + " | ".join(attempts_log)
            }

        except PlaywrightTimeout as e:
            last_step = steps[-1] if steps else "unknown"
            try:
                os.makedirs("/app/data", exist_ok=True)
                page.screenshot(path="/app/data/failure.png", full_page=True)
                send_screenshot(chat_id, "/app/data/failure.png", "Timeout at: " + last_step)
            except Exception:
                pass
            return {"success": False, "error": "Timeout at: " + last_step + "\n" + str(e)[:200]}
        except Exception as e:
            last_step = steps[-1] if steps else "unknown"
            try:
                os.makedirs("/app/data", exist_ok=True)
                page.screenshot(path="/app/data/failure.png", full_page=True)
                send_screenshot(chat_id, "/app/data/failure.png", "Error at: " + last_step)
            except Exception:
                pass
            return {"success": False, "error": "Error at: " + last_step + "\n" + str(e)[:200]}
        finally:
            browser.close()


def _attempt_slot(page, context, date_str, hour, minute, steps, chat_id):
    """Attempt to book a single time slot. Reloads the form for fresh tokens,
    POSTs, then verifies via My Reservations. Returns a result dict."""
    try:
        # Load booking form fresh to get new tokens (tokens are single-use)
        booking_url = BOOKING_URL + date_str
        page.goto(booking_url, timeout=60000)
        page.wait_for_load_state("domcontentloaded", timeout=60000)

        if "login" in page.url.lower() or "auth.buildinglink" in page.url.lower():
            return {"success": False, "error": "Session lost - back on login page."}

        def get_val(selector):
            try:
                el = page.query_selector(selector)
                if el:
                    return el.get_attribute("value") or ""
                return ""
            except Exception:
                return ""

        viewstate = get_val("input[name='__VIEWSTATE']")
        eventvalidation = get_val("input[name='__EVENTVALIDATION']")
        pagestateid = get_val("input[name='__PAGESTATEID']")
        scriptmanager_tsm = get_val("input[id='ctl00_ScriptManager1_TSM']")
        stylesheet_tsm = get_val("input[id='ctl00_RadStyleSheetManager1_TSSM']")

        if not viewstate and not pagestateid:
            return {"success": False, "error": "Could not extract form tokens."}

        target_date = datetime.strptime(date_str, "%Y-%m-%d")
        year = target_date.year
        month = target_date.month
        day = target_date.day
        prev_date = target_date - timedelta(days=1)
        prev_year = prev_date.year
        prev_month = prev_date.month
        prev_day = prev_date.day

        start_bl = to_bl_datetime(date_str, hour, minute)
        end_hour = hour + 1
        end_bl = to_bl_datetime(date_str, end_hour, minute)
        start_12h = to_12h(hour, minute)
        end_12h = to_12h(end_hour, minute)

        cookies = context.cookies()
        cookie_header = "; ".join([c["name"] + "=" + c["value"] for c in cookies])

        payload = {
            "ctl00$ScriptManager1": "ctl00$ContentPlaceHolder1$ctl00$ContentPlaceHolder1$ReservationRequestWrapperPanel|ctl00$ContentPlaceHolder1$FooterSaveButton",
            "ctl00_ScriptManager1_TSM": scriptmanager_tsm,
            "ctl00_RadStyleSheetManager1_TSSM": stylesheet_tsm,
            "__PAGESTATEID": pagestateid,
            "ctl00_ddlCulture_ddlCulture_ClientState": "",
            "ctl00_ddlCultureMobile_ddlCulture_ClientState": "",
            "ctl00_ContentPlaceHolder1_RadButtondDocumentDownload_ClientState": '{"text":"","value":"","checked":false,"target":"","navigateUrl":"","commandName":"","commandArgument":"","autoPostBack":true,"selectedToggleStateIndex":0,"validationGroup":null,"readOnly":false,"primary":false,"enabled":true}',
            "ctl00_ContentPlaceHolder1_StartDatePicker_SD": "[[" + str(year) + "," + str(month) + "," + str(day) + "]]",
            "ctl00_ContentPlaceHolder1_StartDatePicker_AD": "[[" + str(prev_year) + "," + str(prev_month) + "," + str(prev_day) + "],[" + str(year) + "," + str(month) + "," + str(day) + "],[" + str(prev_year) + "," + str(prev_month) + "," + str(prev_day) + "]]",
            "ctl00$ContentPlaceHolder1$StartTimePicker": start_bl,
            "ctl00$ContentPlaceHolder1$StartTimePicker$dateInput": start_12h,
            "ctl00_ContentPlaceHolder1_StartTimePicker_dateInput_ClientState": '{"enabled":true,"emptyMessage":"","validationText":"' + start_bl + '","valueAsString":"' + start_bl + '","minDateStr":"1980-01-01-00-00-00","maxDateStr":"2099-12-31-00-00-00","lastSetTextBoxValue":"' + start_12h + '"}',
            "ctl00_ContentPlaceHolder1_StartTimePicker_timeView_ClientState": "",
            "ctl00_ContentPlaceHolder1_StartTimePicker_ClientState": "",
            "ctl00$ContentPlaceHolder1$EndTimePicker": end_bl,
            "ctl00$ContentPlaceHolder1$EndTimePicker$dateInput": end_12h,
            "ctl00_ContentPlaceHolder1_EndTimePicker_dateInput_ClientState": '{"enabled":true,"emptyMessage":"","validationText":"' + end_bl + '","valueAsString":"' + end_bl + '","minDateStr":"1980-01-01-00-00-00","maxDateStr":"2099-12-31-00-00-00","lastSetTextBoxValue":"' + end_12h + '"}',
            "ctl00_ContentPlaceHolder1_EndTimePicker_timeView_ClientState": "",
            "ctl00_ContentPlaceHolder1_EndTimePicker_ClientState": "",
            "ctl00$ContentPlaceHolder1$ContactName": "Renata Shaykheeva",
            "ctl00$ContentPlaceHolder1$ContactPhone": "(H) 289-689-5174",
            "ctl00$ContentPlaceHolder1$ContactEmailAddress": "renatashayheeva@gmail.com",
            "ctl00$ContentPlaceHolder1$ReservationDescription": "",
            "ctl00$ContentPlaceHolder1$liabilityWaiverAgreeCheckbox": "on",
            "ctl00_ContentPlaceHolder1_winNavAlert_ClientState": "",
            "ctl00_TtConnectCustomerCareCenterToolTip_ClientState": "",
            "ctl00_ControlWindowMaster_ClientState": "",
            "ctl00_RadWindowManagerMasterPage_ClientState": "",
            "__EVENTTARGET": "ctl00$ContentPlaceHolder1$FooterSaveButton",
            "__EVENTARGUMENT": "",
            "__VIEWSTATE": viewstate,
            "__EVENTVALIDATION": eventvalidation,
            "__ASYNCPOST": "true",
            "RadAJAXControlID": "ctl00_ContentPlaceHolder1_RadAjaxManager1",
        }

        response = httpx.post(
            booking_url,
            data=payload,
            headers={
                "Cookie": cookie_header,
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "X-MicrosoftAjax": "Delta=true",
                "X-Requested-With": "XMLHttpRequest",
                "Referer": booking_url,
                "Origin": "https://www.buildinglink.com",
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            },
            timeout=30,
            follow_redirects=True
        )

        response_text = response.text.lower()

        error_phrases = ["already been reserved", "reservation limit", "not available", "conflict", "unable to complete", "maximum number", "session may have timed out"]
        for phrase in error_phrases:
            if phrase in response_text:
                return {"success": False, "error": "BuildingLink: " + phrase}

        # Verify via My Reservations (authoritative)
        return _verify_reservation(page, context, date_str, hour, minute, steps)

    except PlaywrightTimeout as e:
        return {"success": False, "error": "Timeout: " + str(e)[:120]}
    except Exception as e:
        return {"success": False, "error": "Error: " + str(e)[:120]}

def _verify_reservation(page, context, date_str, hour, minute, steps):
    """
    Authoritative verification: GET My Reservations and confirm a Tennis Court
    reservation exists for our exact date + start time with an Approved status.
    Returns a result dict with success + verified fields.
    """
    from datetime import datetime as _dt

    target = _dt.strptime(date_str, "%Y-%m-%d")
    # BuildingLink duration format e.g.: "Tuesday 7/14/26 10:00 AM to 11:00 AM"
    # Build the date fragment: M/D/YY (no leading zeros)
    date_frag = str(target.month) + "/" + str(target.day) + "/" + str(target.year)[2:]
    start_12h = to_12h(hour, minute)          # e.g. "10:00 AM"
    end_12h = to_12h(hour + 1, minute)        # e.g. "11:00 AM"
    # The exact duration string BuildingLink renders
    duration_needle = date_frag + " " + start_12h + " to " + end_12h

    steps.append("7a. Looking for: " + duration_needle)

    try:
        page.goto(MYRES_URL, timeout=30000)
        page.wait_for_load_state("domcontentloaded", timeout=30000)
        page.wait_for_timeout(1500)
    except Exception as e:
        return {"success": True, "verified": False,
                "note": "Booking submitted but couldn't load My Reservations to verify: " + str(e)[:100]}

    # Grab each reservation row and inspect it individually so we can pair
    # the duration with its status (Approved vs Canceled).
    try:
        rows = page.query_selector_all("tr.rgRow, tr.rgAltRow")
        steps.append("7b. Found " + str(len(rows)) + " reservation rows")

        match_approved = False
        match_canceled = False

        for row in rows:
            row_text = row.inner_text()
            # Normalize whitespace for reliable matching
            norm = " ".join(row_text.split())
            if "Tennis Court" not in norm:
                continue
            if duration_needle not in norm:
                continue
            # This row is our date/time + Tennis Court. Check status.
            status_lower = norm.lower()
            if "canceled" in status_lower or "cancelled" in status_lower or "declined" in status_lower:
                match_canceled = True
            elif "approved" in status_lower or "reserved" in status_lower or "requested" in status_lower or "pending" in status_lower:
                match_approved = True

        if match_approved:
            steps.append("7c. CONFIRMED - approved reservation found")
            return {"success": True, "verified": True}

        if match_canceled and not match_approved:
            steps.append("7c. Found matching reservation but it is CANCELED")
            return {"success": False,
                    "error": "Reservation shows as Canceled for " + duration_needle + " - slot may have been lost."}

        # No matching row at all
        steps.append("7c. No matching reservation found in My Reservations")
        return {"success": False,
                "error": "Booking did not appear in My Reservations for " + duration_needle + ". It was not secured."}

    except Exception as e:
        return {"success": True, "verified": False,
                "note": "Booking submitted but verification parsing failed: " + str(e)[:100]}
