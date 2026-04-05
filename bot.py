"""
NutriBot — Telegram nutrition tracking bot powered by Claude Vision.
Handles meal photos and text, tracks daily macros, personalised to Renata's targets.
"""

import os
import logging
import base64
import json
from datetime import date, datetime
from zoneinfo import ZoneInfo

import anthropic
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

from storage import DailyLog, get_today_log, save_log, reset_log
from targets import get_targets, DAY_TYPE_TRAINING, DAY_TYPE_REST
from prompts import build_analysis_prompt, build_summary_prompt

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────
ANTHROPIC_MODEL = "claude-opus-4-5"
USER_TIMEZONE = ZoneInfo(os.getenv("USER_TIMEZONE", "Europe/London"))

# Allowed Telegram user IDs (comma-separated in env). Empty = allow all.
ALLOWED_USERS_RAW = os.getenv("ALLOWED_USER_IDS", "")
ALLOWED_USERS: set[int] = (
    {int(uid.strip()) for uid in ALLOWED_USERS_RAW.split(",") if uid.strip()}
    if ALLOWED_USERS_RAW
    else set()
)

anthropic_client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])


# ── Auth guard ────────────────────────────────────────────────────────────────
def is_allowed(user_id: int) -> bool:
    return not ALLOWED_USERS or user_id in ALLOWED_USERS


# ── Keyboard helper ───────────────────────────────────────────────────────────
def main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("📊 Daily Summary"), KeyboardButton("🏋️ Training Day")],
            [KeyboardButton("😴 Rest Day"), KeyboardButton("🔄 Reset Today")],
        ],
        resize_keyboard=True,
    )


# ── Claude vision call ────────────────────────────────────────────────────────
def analyse_meal(
    image_b64: str | None,
    text_description: str | None,
    day_log: DailyLog,
    day_type: str,
) -> str:
    """Call Claude to analyse a meal and return structured nutrition feedback."""
    targets = get_targets(day_type)
    prompt = build_analysis_prompt(
        text_description=text_description,
        current_log=day_log,
        targets=targets,
    )

    content: list = []
    if image_b64:
        content.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/jpeg",
                    "data": image_b64,
                },
            }
        )
    content.append({"type": "text", "text": prompt})

    response = anthropic_client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=1024,
        messages=[{"role": "user", "content": content}],
    )
    return response.content[0].text


def get_daily_summary(day_log: DailyLog, day_type: str) -> str:
    targets = get_targets(day_type)
    prompt = build_summary_prompt(day_log, targets)
    response = anthropic_client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text


# ── Parse macros from Claude response ─────────────────────────────────────────
def extract_macros_from_response(response_text: str) -> dict | None:
    """
    Claude is instructed to embed a JSON block like:
        <macros>{"kcal": 450, "protein": 35, "carbs": 40, "fat": 12}</macros>
    This extracts and parses it.
    """
    import re

    match = re.search(r"<macros>(.*?)</macros>", response_text, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(1).strip())
    except json.JSONDecodeError:
        return None


# ── Command handlers ──────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update.effective_user.id):
        return
    await update.message.reply_text(
        "👋 Hi Renata! I'm NutriBot — your personal nutrition tracker.\n\n"
        "📸 *Send me a meal photo* (with an optional caption)\n"
        "✍️ *Or describe your meal in text* and I'll estimate the macros.\n\n"
        "Use the buttons below to check your daily summary or switch between "
        "training and rest day targets.",
        parse_mode="Markdown",
        reply_markup=main_keyboard(),
    )


async def cmd_summary(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update.effective_user.id):
        return
    user_id = update.effective_user.id
    day_log = get_today_log(user_id)
    day_type = context.user_data.get("day_type", DAY_TYPE_TRAINING)

    if day_log.meal_count == 0:
        await update.message.reply_text(
            "No meals logged yet today. Send me a photo or description of your food! 🥗"
        )
        return

    await update.message.reply_text("⏳ Generating your summary…")
    summary = get_daily_summary(day_log, day_type)
    await update.message.reply_text(summary, parse_mode="Markdown")


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update.effective_user.id):
        return
    reset_log(update.effective_user.id)
    await update.message.reply_text(
        "✅ Daily log reset! Ready to track from scratch. 🌅",
        reply_markup=main_keyboard(),
    )


# ── Message handler (photo or text) ──────────────────────────────────────────
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update.effective_user.id):
        return

    user_id = update.effective_user.id
    text = update.message.text or update.message.caption or ""

    # Keyboard button shortcuts
    if text == "📊 Daily Summary":
        await cmd_summary(update, context)
        return
    if text == "🏋️ Training Day":
        context.user_data["day_type"] = DAY_TYPE_TRAINING
        targets = get_targets(DAY_TYPE_TRAINING)
        await update.message.reply_text(
            f"🏋️ *Training day* targets set!\n"
            f"• Calories: {targets['kcal_min']}–{targets['kcal_max']} kcal\n"
            f"• Protein: {targets['protein_min']}–{targets['protein_max']}g\n"
            f"• Carbs: {targets['carbs_min']}–{targets['carbs_max']}g\n"
            f"• Fat: {targets['fat_min']}–{targets['fat_max']}g",
            parse_mode="Markdown",
            reply_markup=main_keyboard(),
        )
        return
    if text == "😴 Rest Day":
        context.user_data["day_type"] = DAY_TYPE_REST
        targets = get_targets(DAY_TYPE_REST)
        await update.message.reply_text(
            f"😴 *Rest day* targets set!\n"
            f"• Calories: {targets['kcal_min']}–{targets['kcal_max']} kcal\n"
            f"• Protein: {targets['protein_min']}–{targets['protein_max']}g\n"
            f"• Carbs: {targets['carbs_min']}–{targets['carbs_max']}g\n"
            f"• Fat: {targets['fat_min']}–{targets['fat_max']}g",
            parse_mode="Markdown",
            reply_markup=main_keyboard(),
        )
        return
    if text == "🔄 Reset Today":
        await cmd_reset(update, context)
        return

    # Determine if this is a photo or text meal log
    image_b64 = None
    if update.message.photo:
        photo = update.message.photo[-1]  # highest resolution
        file = await context.bot.get_file(photo.file_id)
        file_bytes = await file.download_as_bytearray()
        image_b64 = base64.b64encode(file_bytes).decode("utf-8")
    elif not text:
        await update.message.reply_text(
            "Please send a meal photo or describe what you ate. 🍽️"
        )
        return

    day_type = context.user_data.get("day_type", DAY_TYPE_TRAINING)
    day_log = get_today_log(user_id)

    status_msg = await update.message.reply_text("🔍 Analysing your meal…")

    try:
        response_text = analyse_meal(image_b64, text, day_log, day_type)
    except Exception as e:
        logger.error("Claude API error: %s", e)
        await status_msg.edit_text(
            "⚠️ Sorry, I couldn't analyse that meal. Please try again."
        )
        return

    # Extract and store macros
    macros = extract_macros_from_response(response_text)
    if macros:
        day_log.add_meal(
            kcal=macros.get("kcal", 0),
            protein=macros.get("protein", 0),
            carbs=macros.get("carbs", 0),
            fat=macros.get("fat", 0),
        )
        save_log(user_id, day_log)

    # Clean response — remove the hidden macros tag before sending
    import re
    clean_response = re.sub(r"<macros>.*?</macros>", "", response_text, flags=re.DOTALL).strip()

    await status_msg.edit_text(clean_response, parse_mode="Markdown")


# ── Entry point ───────────────────────────────────────────────────────────────
def main() -> None:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("summary", cmd_summary))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(
        MessageHandler(filters.PHOTO | filters.TEXT & ~filters.COMMAND, handle_message)
    )

    logger.info("NutriBot starting…")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
