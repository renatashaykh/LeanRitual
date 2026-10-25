"""
Run by Railway Cron at 12:05 AM Toronto time (4:05 AM UTC) every night.
Reads pending bookings from Postgres, executes any due today, notifies via Telegram.
"""
import os
import asyncio
import logging
from datetime import datetime
import pytz
import httpx

from booker import book_tennis_court
import storage

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

TORONTO_TZ = pytz.timezone("America/Toronto")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")


async def send_telegram(chat_id, text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    async with httpx.AsyncClient() as client:
        await client.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"})


async def main():
    now = datetime.now(TORONTO_TZ)
    today_str = now.strftime("%Y-%m-%d")
    logger.info(f"Cron running at {now.strftime('%Y-%m-%d %H:%M:%S')} Toronto time")

    try:
        storage.init_db()
    except Exception as e:
        logger.error("DB init failed: " + str(e))
        return

    try:
        bookings = storage.get_bookings_for_trigger_date(today_str)
    except Exception as e:
        logger.error("Failed to load bookings: " + str(e))
        return

    if not bookings:
        logger.info(f"No bookings with trigger_date = {today_str}")
        return

    logger.info(f"Found {len(bookings)} booking(s) to execute")

    for booking in bookings:
        chat_id = booking["chat_id"]
        date_str = booking["date_str"]
        hour = booking["hour"]
        minute = booking["minute"]
        date_display = booking["date_display"]
        time_display = booking["time_display"]
        end_hour = hour + 1

        logger.info(f"Executing booking: {date_str} {hour:02d}:{minute:02d}")
        await send_telegram(chat_id, f"⏳ 12:00 AM — booking window is open! Booking now...\n📅 {date_display}\n🕐 {time_display} – {end_hour:02d}:{minute:02d}")

        try:
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(None, book_tennis_court, date_str, hour, minute, chat_id)
            if result["success"]:
                verified = result.get("verified", None)
                if verified is True:
                    booked_hour = result.get("booked_hour", hour)
                    booked_min = result.get("booked_minute", minute)
                    b_end = booked_hour + 1
                    booked_time = f"{booked_hour:02d}:{booked_min:02d} – {b_end:02d}:{booked_min:02d}"
                    if result.get("was_fallback"):
                        req_h = result.get("requested_hour", hour)
                        status_line = f"⚠️ *{req_h:02d}:00 was taken.*\n✅ *Booked next slot & Verified!*\n☑️ Confirmed in My Reservations"
                    else:
                        status_line = "✅ *Booked & Verified!*\n☑️ Confirmed in My Reservations"
                    msg = f"{status_line}\n🎾 Tennis Court\n📅 {date_display}\n🕐 {booked_time}"
                else:
                    status_line = "⚠️ *Submitted but NOT confirmed*\nPlease check My Reservations manually"
                    msg = f"{status_line}\n🎾 Tennis Court\n📅 {date_display}\n🕐 {time_display} – {end_hour:02d}:{minute:02d}"
                if result.get("note"):
                    msg += f"\n\nℹ️ {result['note']}"
                await send_telegram(chat_id, msg)
                logger.info("Booking successful!")
                # Remove from DB after successful booking
                storage.delete_booking(booking["id"])
            else:
                await send_telegram(chat_id, f"❌ Booking failed:\n{result['error']}")
                logger.error(f"Booking failed: {result['error']}")
                # Remove failed booking too so it doesn't retry forever
                storage.delete_booking(booking["id"])
        except Exception as e:
            logger.error(f"Error: {e}")
            await send_telegram(chat_id, f"❌ Unexpected error: {str(e)}")

    logger.info("Cron run complete.")


if __name__ == "__main__":
    asyncio.run(main())
