"""
NutriBot — Telegram nutrition tracking bot powered by Claude Vision.
Handles onboarding, meal photos and text, tracks daily macros.
Supports English and Russian; responds in the user's language.
"""

import os
import logging
import base64
import json
import re
from datetime import date
from zoneinfo import ZoneInfo

import anthropic
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    filters,
    ContextTypes,
)

from storage import (
    DailyLog, UserProfile,
    get_today_log, save_log, reset_log,
    get_profile, save_profile, delete_profile,
)
from targets import (
    get_targets, DAY_TYPE_TRAINING, DAY_TYPE_REST,
    GOAL_FAT_LOSS, GOAL_MUSCLE_GAIN, GOAL_MAINTAIN,
)
from prompts import build_analysis_prompt, build_summary_prompt

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
ANTHROPIC_MODEL = "claude-opus-4-5"
USER_TIMEZONE = ZoneInfo(os.getenv("USER_TIMEZONE", "Europe/London"))

ALLOWED_USERS_RAW = os.getenv("ALLOWED_USER_IDS", "")
ALLOWED_USERS: set[int] = (
    {int(uid.strip()) for uid in ALLOWED_USERS_RAW.split(",") if uid.strip()}
    if ALLOWED_USERS_RAW else set()
)

anthropic_client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

# ── Onboarding conversation states ────────────────────────────────────────────
ASK_SEX, ASK_AGE, ASK_WEIGHT, ASK_GOAL = range(4)


# ── Auth ──────────────────────────────────────────────────────────────────────
def is_allowed(user_id: int) -> bool:
    return not ALLOWED_USERS or user_id in ALLOWED_USERS


# ── Keyboards ─────────────────────────────────────────────────────────────────
def main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("📊 Daily Summary"), KeyboardButton("🏋️ Training Day")],
            [KeyboardButton("😴 Rest Day"), KeyboardButton("🔄 Reset Today")],
            [KeyboardButton("👤 My Profile")],
        ],
        resize_keyboard=True,
    )


def sex_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[KeyboardButton("Female / Женщина"), KeyboardButton("Male / Мужчина")]],
        resize_keyboard=True, one_time_keyboard=True,
    )


def goal_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("🔥 Fat Loss / Похудение")],
            [KeyboardButton("💪 Muscle Gain / Набор мышц")],
            [KeyboardButton("⚖️ Maintain / Поддержание")],
        ],
        resize_keyboard=True, one_time_keyboard=True,
    )


# ── Onboarding handlers ───────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_allowed(update.effective_user.id):
        return ConversationHandler.END

    profile = get_profile(update.effective_user.id)
    if profile and profile.is_complete():
        await update.message.reply_text(
            "👋 Welcome back! Send me a meal photo or description to log it.\n\n"
            "Добро пожаловать! Отправьте фото еды или описание, чтобы записать приём пищи.",
            reply_markup=main_keyboard(),
        )
        return ConversationHandler.END

    # Start onboarding
    await update.message.reply_text(
        "👋 Hi! I'm NutriBot — your personal nutrition tracker.\n"
        "Привет! Я NutriBot — ваш персональный трекер питания.\n\n"
        "Let's set up your profile first. / Давайте сначала настроим ваш профиль.\n\n"
        "**What is your sex? / Ваш пол?**",
        parse_mode="Markdown",
        reply_markup=sex_keyboard(),
    )
    return ASK_SEX


async def onboard_sex(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.lower()
    if "female" in text or "женщ" in text:
        context.user_data["ob_sex"] = "female"
    else:
        context.user_data["ob_sex"] = "male"

    await update.message.reply_text(
        "✅ Got it!\n\n"
        "**How old are you? / Сколько вам лет?**\n"
        "_(Type a number / Введите число)_",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove(),
    )
    return ASK_AGE


async def onboard_age(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        age = int(update.message.text.strip())
        if not (10 <= age <= 100):
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            "Please enter a valid age (e.g. 28). / Введите корректный возраст (например, 28)."
        )
        return ASK_AGE

    context.user_data["ob_age"] = age
    await update.message.reply_text(
        "✅ Got it!\n\n"
        "**What is your current weight in kg? / Ваш текущий вес в кг?**\n"
        "_(e.g. 65 or 65.5 / например, 65 или 65.5)_",
        parse_mode="Markdown",
    )
    return ASK_WEIGHT


async def onboard_weight(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        weight = float(update.message.text.strip().replace(",", "."))
        if not (30 <= weight <= 300):
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            "Please enter a valid weight in kg (e.g. 65). / Введите вес в кг (например, 65)."
        )
        return ASK_WEIGHT

    context.user_data["ob_weight"] = weight
    await update.message.reply_text(
        "✅ Got it!\n\n"
        "**What is your goal? / Ваша цель?**",
        parse_mode="Markdown",
        reply_markup=goal_keyboard(),
    )
    return ASK_GOAL


async def onboard_goal(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.lower()
    if "fat" in text or "похуд" in text:
        goal = GOAL_FAT_LOSS
    elif "muscle" in text or "набор" in text:
        goal = GOAL_MUSCLE_GAIN
    else:
        goal = GOAL_MAINTAIN

    profile = UserProfile(
        age=context.user_data["ob_age"],
        sex=context.user_data["ob_sex"],
        weight_kg=context.user_data["ob_weight"],
        goal=goal,
    )
    save_profile(update.effective_user.id, profile)

    targets_training = get_targets(DAY_TYPE_TRAINING, profile)
    targets_rest = get_targets(DAY_TYPE_REST, profile)

    await update.message.reply_text(
        f"✅ **Profile saved! / Профиль сохранён!**\n\n"
        f"🏋️ Training days: {targets_training['kcal_min']}–{targets_training['kcal_max']} kcal "
        f"| {targets_training['protein_max']}g protein\n"
        f"😴 Rest days: {targets_rest['kcal_min']}–{targets_rest['kcal_max']} kcal "
        f"| {targets_rest['protein_max']}g protein\n\n"
        f"Now send me a meal photo or description! 🍽️\n"
        f"Теперь отправьте фото еды или описание! 🍽️",
        parse_mode="Markdown",
        reply_markup=main_keyboard(),
    )
    return ConversationHandler.END


async def onboard_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Setup cancelled. Send /start to try again.")
    return ConversationHandler.END


# ── Claude API calls ──────────────────────────────────────────────────────────
def analyse_meal(
    image_b64: str | None,
    text_description: str | None,
    day_log: DailyLog,
    day_type: str,
    profile: UserProfile | None,
) -> str:
    targets = get_targets(day_type, profile)
    prompt = build_analysis_prompt(
        text_description=text_description,
        current_log=day_log,
        targets=targets,
        profile=profile,
    )
    content: list = []
    if image_b64:
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg", "data": image_b64},
        })
    content.append({"type": "text", "text": prompt})

    response = anthropic_client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=1024,
        messages=[{"role": "user", "content": content}],
    )
    return response.content[0].text


def get_daily_summary(day_log: DailyLog, day_type: str, profile: UserProfile | None) -> str:
    targets = get_targets(day_type, profile)
    prompt = build_summary_prompt(day_log, targets, profile)
    response = anthropic_client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text


def extract_macros_from_response(response_text: str) -> dict | None:
    match = re.search(r"<macros>(.*?)</macros>", response_text, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(1).strip())
    except json.JSONDecodeError:
        return None


# ── Regular message & command handlers ───────────────────────────────────────
async def cmd_summary(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update.effective_user.id):
        return
    user_id = update.effective_user.id
    profile = get_profile(user_id)
    day_log = get_today_log(user_id)
    day_type = context.user_data.get("day_type", DAY_TYPE_TRAINING)

    if day_log.meal_count == 0:
        await update.message.reply_text(
            "No meals logged yet today. / Сегодня ещё нет записей.\n\nSend me a photo or description! 🥗"
        )
        return

    await update.message.reply_text("⏳ Generating summary… / Формирую сводку…")
    summary = get_daily_summary(day_log, day_type, profile)
    await update.message.reply_text(summary, parse_mode="Markdown")


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update.effective_user.id):
        return
    reset_log(update.effective_user.id)
    await update.message.reply_text(
        "✅ Daily log reset! / Дневной журнал сброшен! 🌅",
        reply_markup=main_keyboard(),
    )


async def cmd_profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update.effective_user.id):
        return
    profile = get_profile(update.effective_user.id)
    if not profile:
        await update.message.reply_text("No profile found. Send /start to set one up.")
        return
    targets_t = get_targets(DAY_TYPE_TRAINING, profile)
    targets_r = get_targets(DAY_TYPE_REST, profile)
    await update.message.reply_text(
        f"👤 **Your Profile / Ваш профиль**\n\n"
        f"Sex / Пол: {profile.sex}\n"
        f"Age / Возраст: {profile.age}\n"
        f"Weight / Вес: {profile.weight_kg} kg\n"
        f"Goal / Цель: {profile.goal}\n\n"
        f"🏋️ Training: {targets_t['kcal_min']}–{targets_t['kcal_max']} kcal | {targets_t['protein_max']}g protein\n"
        f"😴 Rest: {targets_r['kcal_min']}–{targets_r['kcal_max']} kcal | {targets_r['protein_max']}g protein\n\n"
        f"To reset profile send /resetprofile",
        parse_mode="Markdown",
    )


async def cmd_resetprofile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update.effective_user.id):
        return
    delete_profile(update.effective_user.id)
    await update.message.reply_text(
        "Profile deleted. Send /start to set up a new one.\n"
        "Профиль удалён. Отправьте /start для настройки нового."
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update.effective_user.id):
        return

    user_id = update.effective_user.id
    text = update.message.text or update.message.caption or ""

    # Keyboard shortcuts
    if text == "📊 Daily Summary":
        await cmd_summary(update, context)
        return
    if text == "🏋️ Training Day":
        context.user_data["day_type"] = DAY_TYPE_TRAINING
        profile = get_profile(user_id)
        targets = get_targets(DAY_TYPE_TRAINING, profile)
        await update.message.reply_text(
            f"🏋️ *Training day set!*\n"
            f"Calories: {targets['kcal_min']}–{targets['kcal_max']} kcal | "
            f"Protein: {targets['protein_max']}g",
            parse_mode="Markdown", reply_markup=main_keyboard(),
        )
        return
    if text == "😴 Rest Day":
        context.user_data["day_type"] = DAY_TYPE_REST
        profile = get_profile(user_id)
        targets = get_targets(DAY_TYPE_REST, profile)
        await update.message.reply_text(
            f"😴 *Rest day set!*\n"
            f"Calories: {targets['kcal_min']}–{targets['kcal_max']} kcal | "
            f"Protein: {targets['protein_max']}g",
            parse_mode="Markdown", reply_markup=main_keyboard(),
        )
        return
    if text == "🔄 Reset Today":
        await cmd_reset(update, context)
        return
    if text == "👤 My Profile":
        await cmd_profile(update, context)
        return

    # Check profile exists
    profile = get_profile(user_id)
    if not profile or not profile.is_complete():
        await update.message.reply_text(
            "Please set up your profile first. / Сначала настройте профиль.\n\nSend /start"
        )
        return

    # Photo or text meal log
    image_b64 = None
    if update.message.photo:
        photo = update.message.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        file_bytes = await file.download_as_bytearray()
        image_b64 = base64.b64encode(file_bytes).decode("utf-8")
    elif not text:
        await update.message.reply_text(
            "Please send a meal photo or describe what you ate. / "
            "Отправьте фото еды или опишите, что вы съели. 🍽️"
        )
        return

    day_type = context.user_data.get("day_type", DAY_TYPE_TRAINING)
    day_log = get_today_log(user_id)

    status_msg = await update.message.reply_text("🔍 Analysing… / Анализирую…")

    try:
        response_text = analyse_meal(image_b64, text, day_log, day_type, profile)
    except Exception as e:
        logger.error("Claude API error: %s", e)
        await status_msg.edit_text(
            "⚠️ Sorry, couldn't analyse that. Please try again. / "
            "Извините, не удалось проанализировать. Попробуйте ещё раз."
        )
        return

    macros = extract_macros_from_response(response_text)
    if macros:
        day_log.add_meal(
            kcal=macros.get("kcal", 0),
            protein=macros.get("protein", 0),
            carbs=macros.get("carbs", 0),
            fat=macros.get("fat", 0),
        )
        save_log(user_id, day_log)

    clean_response = re.sub(r"<macros>.*?</macros>", "", response_text, flags=re.DOTALL).strip()
    await status_msg.edit_text(clean_response, parse_mode="Markdown")


# ── Entry point ───────────────────────────────────────────────────────────────
def main() -> None:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    app = Application.builder().token(token).build()

    # Onboarding conversation
    onboarding = ConversationHandler(
        entry_points=[CommandHandler("start", cmd_start)],
        states={
            ASK_SEX:    [MessageHandler(filters.TEXT & ~filters.COMMAND, onboard_sex)],
            ASK_AGE:    [MessageHandler(filters.TEXT & ~filters.COMMAND, onboard_age)],
            ASK_WEIGHT: [MessageHandler(filters.TEXT & ~filters.COMMAND, onboard_weight)],
            ASK_GOAL:   [MessageHandler(filters.TEXT & ~filters.COMMAND, onboard_goal)],
        },
        fallbacks=[CommandHandler("cancel", onboard_cancel)],
    )

    app.add_handler(onboarding)
    app.add_handler(CommandHandler("summary", cmd_summary))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("profile", cmd_profile))
    app.add_handler(CommandHandler("resetprofile", cmd_resetprofile))
    app.add_handler(
        MessageHandler(filters.PHOTO | filters.TEXT & ~filters.COMMAND, handle_message)
    )

    logger.info("NutriBot starting…")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
