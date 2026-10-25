import os
import logging
import asyncio
from datetime import datetime, timedelta
import pytz
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

from booker import book_tennis_court
import storage

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

TORONTO_TZ = pytz.timezone("America/Toronto")
ALLOWED_USER_ID = int(os.environ.get("ALLOWED_USER_ID", "0"))


def is_authorized(update):
    return ALLOWED_USER_ID == 0 or update.effective_user.id == ALLOWED_USER_ID


def parse_date(date_str):
    now = datetime.now(TORONTO_TZ)
    today = now.date()
    tomorrow = today + timedelta(days=1)
    s = date_str.strip().lower()
    if s == "today":
        return today
    if s == "tomorrow":
        return tomorrow
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        pass
    for fmt in ["%b %d", "%B %d", "%b %d %Y", "%B %d %Y"]:
        try:
            p = datetime.strptime(s, fmt)
            if fmt in ["%b %d", "%B %d"]:
                p = p.replace(year=today.year)
            return p.date()
        except ValueError:
            continue
    return None


def parse_time(time_str):
    s = time_str.strip().lower().replace(" ", "")
    for fmt in ["%I%p", "%I:%M%p", "%H:%M", "%H"]:
        try:
            t = datetime.strptime(s, fmt)
            return t.hour, t.minute
        except ValueError:
            continue
    return None


async def start(update, context):
    if not is_authorized(update):
        return
    await update.message.reply_text(
        "🎾 *Tennis Court Booking Bot*\n\n"
        "Commands:\n"
        "`/book <date> <time>` — Book or schedule\n"
        "`/pending` — Show scheduled bookings\n"
        "`/cancel` — Cancel all pending bookings\n"
        "`/help` — Examples\n\n"
        "💡 If the window isn't open yet, I'll book automatically at 12:00 AM.",
        parse_mode="Markdown"
    )


async def help_command(update, context):
    if not is_authorized(update):
        return
    await update.message.reply_text(
        "🎾 *Examples:*\n\n"
        "`/book today 8am`\n"
        "`/book tomorrow 7am`\n"
        "`/book June 14 9am` — schedules for midnight\n\n"
        "Available: 7AM–9PM, 1 hour slots.",
        parse_mode="Markdown"
    )


async def pending(update, context):
    if not is_authorized(update):
        return
    try:
        bookings = storage.load_bookings()
    except Exception as e:
        await update.message.reply_text(f"❌ Database error: {str(e)}")
        return
    if not bookings:
        await update.message.reply_text("📭 No pending bookings.")
        return
    lines = ["⏰ *Pending bookings:*\n"]
    for b in bookings:
        lines.append(f"📅 {b['date_display']} at {b['time_display']}\n   Books at: 12:00 AM on {b['trigger_date']}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cancel_command(update, context):
    if not is_authorized(update):
        return
    try:
        count = storage.clear_all_bookings()
    except Exception as e:
        await update.message.reply_text(f"❌ Database error: {str(e)}")
        return
    if count == 0:
        await update.message.reply_text("📭 No pending bookings to cancel.")
    else:
        await update.message.reply_text(f"🗑️ Cancelled {count} pending booking(s).")


async def book(update, context):
    if not is_authorized(update):
        await update.message.reply_text("⛔ Unauthorized.")
        return

    args = context.args
    if len(args) < 2:
        await update.message.reply_text("Usage: `/book <date> <time>`\nExample: `/book tomorrow 8am`", parse_mode="Markdown")
        return

    time_str = args[-1]
    date_str = " ".join(args[:-1])

    booking_date = parse_date(date_str)
    if booking_date is None:
        await update.message.reply_text(f"❌ Can't parse date: `{date_str}`\nTry: `today`, `tomorrow`, `June 14`", parse_mode="Markdown")
        return

    parsed_time = parse_time(time_str)
    if parsed_time is None:
        await update.message.reply_text(f"❌ Can't parse time: `{time_str}`\nTry: `7am`, `8:00am`, `19:00`", parse_mode="Markdown")
        return

    now = datetime.now(TORONTO_TZ)
    today = now.date()
    tomorrow = today + timedelta(days=1)
    day_after = today + timedelta(days=2)

    if booking_date < today:
        await update.message.reply_text("❌ Can't book in the past.")
        return
    if booking_date > day_after:
        await update.message.reply_text(f"❌ Can only book up to 2 days ahead. Latest: {day_after}")
        return

    hour, minute = parsed_time
    if hour < 7 or hour >= 21:
        await update.message.reply_text(f"❌ Court available 7AM–9PM only. You requested: {hour:02d}:{minute:02d}")
        return

    date_display = booking_date.strftime("%A, %B %d")
    time_display = f"{hour:02d}:{minute:02d}"
    end_hour = hour + 1
    date_str_iso = booking_date.strftime("%Y-%m-%d")

    window_open = booking_date <= tomorrow

    if window_open:
        await update.message.reply_text(f"⏳ Booking now...\n📅 {date_display}\n🕐 {time_display} – {end_hour:02d}:{minute:02d}")
        try:
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(None, book_tennis_court, date_str_iso, hour, minute, update.effective_chat.id)
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
                        msg = f"{status_line}\n🎾 Tennis Court\n📅 {date_display}\n🕐 {booked_time}"
                    else:
                        status_line = "✅ *Booked & Verified!*\n☑️ Confirmed in My Reservations"
                        msg = f"{status_line}\n🎾 Tennis Court\n📅 {date_display}\n🕐 {booked_time}"
                else:
                    status_line = "⚠️ *Submitted but NOT confirmed*\nCould not verify in My Reservations — please check manually"
                    msg = f"{status_line}\n🎾 Tennis Court\n📅 {date_display}\n🕐 {time_display} – {end_hour:02d}:{minute:02d}"
                if result.get("note"):
                    msg += f"\n\nℹ️ {result['note']}"
                await update.message.reply_text(msg, parse_mode="Markdown")
            else:
                await update.message.reply_text(f"❌ *Booking failed:*\n{result['error']}", parse_mode="Markdown")
        except Exception as e:
            await update.message.reply_text(f"❌ Error: {str(e)}")
    else:
        trigger_date = booking_date - timedelta(days=1)
        trigger_date_str = trigger_date.strftime("%Y-%m-%d")

        booking_entry = {
            "chat_id": update.effective_chat.id,
            "date_str": date_str_iso,
            "hour": hour,
            "minute": minute,
            "date_display": date_display,
            "time_display": time_display,
            "trigger_date": trigger_date_str,
        }

        try:
            storage.add_booking(booking_entry)
        except Exception as e:
            await update.message.reply_text(f"❌ Database error: {str(e)}")
            return

        await update.message.reply_text(
            f"⏰ *Scheduled!*\n"
            f"🎾 Tennis Court\n"
            f"📅 {date_display}\n"
            f"🕐 {time_display} – {end_hour:02d}:{minute:02d}\n\n"
            f"I'll book at *12:00 AM on {trigger_date.strftime('%A, %B %d')}* when the window opens.\n"
            f"Use /pending to check or /cancel to remove.",
            parse_mode="Markdown"
        )


def main():
    # Initialize database table on startup
    try:
        storage.init_db()
    except Exception as e:
        logger.error("Failed to init DB: " + str(e))

    token = os.environ["TELEGRAM_BOT_TOKEN"]
    app = ApplicationBuilder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("book", book))
    app.add_handler(CommandHandler("pending", pending))
    app.add_handler(CommandHandler("cancel", cancel_command))
    logger.info("Bot started.")
    app.run_polling()


if __name__ == "__main__":
    main()
