"""
LeanRitual — Telegram wellness bot powered by Claude.
Sections: 🥗 Nutrition + 🏋️ Fitness, integrated coaching, bilingual EN/RU.
"""

import os
import logging
import base64
import json
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import anthropic
from telegram import (
    Update, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove,
    InlineKeyboardMarkup, InlineKeyboardButton,
)
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    filters,
    ContextTypes,
)

from storage import (
    UserProfile,
    get_today_log, get_log, save_log, reset_log, undo_last_meal, find_meal_by_message,
    get_profile, save_profile,
    record_and_check_trial, mark_paid,
)
import meal_ai
from meal_ai import md_to_tg_html, strip_tags, split_message, render_meal
from targets import (
    get_targets, DAY_TYPE_TRAINING, DAY_TYPE_REST,
    GOAL_FAT_LOSS, GOAL_MUSCLE_GAIN, GOAL_MAINTAIN, GOAL_LABELS,
)
from prompts import build_summary_prompt
from fitness_storage import (
    FitnessProfile,
    get_fitness_profile, save_fitness_profile,
    WeeklyPlan, get_weekly_plan, save_weekly_plan, get_recent_weeks,
    WorkoutLog, save_workout_log, get_recent_workout_logs,
    get_checkin_status, save_checkin_status, record_checkin,
    is_checkin_due, current_week_key, today_str,
)
from fitness_prompts import (
    build_weekly_plan_prompt,
    build_workout_log_prompt,
    build_activity_log_prompt,
    build_monthly_checkin_prompt,
    build_progress_prompt,
)

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
# Override in Railway with the ANTHROPIC_MODEL variable if needed.
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-opus-5-5")
# Used automatically if the main model errors (not available, rate-limited, overloaded).
FALLBACK_MODEL = os.getenv("FALLBACK_MODEL", "claude-sonnet-5-5")
USER_TIMEZONE = ZoneInfo(os.getenv("USER_TIMEZONE", "America/Toronto"))

ALLOWED_USERS_RAW = os.getenv("ALLOWED_USER_IDS", "")
ALLOWED_USERS: set[int] = (
    {int(uid.strip()) for uid in ALLOWED_USERS_RAW.split(",") if uid.strip()}
    if ALLOWED_USERS_RAW else set()
)

anthropic_client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
async_client = anthropic.AsyncAnthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

# ── Conversation states ───────────────────────────────────────────────────────
# Nutrition onboarding
ASK_LANG, ASK_SEX, ASK_AGE, ASK_WEIGHT, ASK_GOAL = range(5)
# Nutrition profile edit
EDIT_CHOOSE, EDIT_SEX, EDIT_AGE, EDIT_WEIGHT, EDIT_GOAL = range(5, 10)
# Fitness onboarding
FIT_ENV, FIT_LEVEL, FIT_GOAL, FIT_LIMITATIONS = range(10, 14)
# Monthly check-in
CHECKIN_WEIGHT, CHECKIN_NOTES = range(14, 16)
# Workout logging
LOG_WORKOUT_TEXT = 16
# Activity logging
LOG_ACTIVITY_TEXT = 17
# Height (onboarding + profile edit)
ASK_HEIGHT, EDIT_HEIGHT = 18, 19


# ── Auth ──────────────────────────────────────────────────────────────────────
def is_allowed(user_id: int) -> bool:
    return not ALLOWED_USERS or user_id in ALLOWED_USERS


# ── Localisation helpers ──────────────────────────────────────────────────────
def t(key: str, lang: str) -> str:
    """Return UI string in the correct language."""
    strings = {
        "welcome_back":     {"en": "👋 Welcome back! Send me a meal photo or description.",
                             "ru": "👋 С возвращением! Отправьте фото еды или описание."},
        "choose_lang":      {"en": "👋 Hi! I'm NutriBot — your personal nutrition tracker.\n\nPlease choose your language:",
                             "ru": "👋 Привет! Я NutriBot — ваш персональный трекер питания.\n\nПожалуйста, выберите язык:"},
        "ask_sex":          {"en": "✅ Got it!\n\n*What is your biological sex?*\n_(Used for calorie calculations)_",
                             "ru": "✅ Отлично!\n\n*Ваш биологический пол?*\n_(Используется для расчёта калорий)_"},
        "ask_age":          {"en": "✅ Got it!\n\n*How old are you?*\n_(Type a number, e.g. 28)_",
                             "ru": "✅ Отлично!\n\n*Сколько вам лет?*\n_(Введите число, например 28)_"},
        "bad_age":          {"en": "Please enter a valid age (e.g. 28).",
                             "ru": "Введите корректный возраст (например, 28)."},
        "ask_weight":       {"en": "✅ Got it!\n\n*What is your current weight in kg?*\n_(e.g. 65 or 65.5)_",
                             "ru": "✅ Отлично!\n\n*Ваш текущий вес в кг?*\n_(например, 65 или 65,5)_"},
        "bad_weight":       {"en": "Please enter a valid weight in kg (e.g. 65).",
                             "ru": "Введите корректный вес в кг (например, 65)."},
        "ask_goal":         {"en": "✅ Got it!\n\n*What is your goal?*",
                             "ru": "✅ Отлично!\n\n*Ваша цель?*"},
        "profile_saved":    {"en": "✅ *Profile saved!*",
                             "ru": "✅ *Профиль сохранён!*"},
        "ready":            {"en": "Now send me a meal photo or description! 🍽️",
                             "ru": "Теперь отправьте фото еды или описание! 🍽️"},
        "no_meals":         {"en": "No meals logged yet today. Send me a photo or description! 🥗",
                             "ru": "Сегодня ещё нет записей. Отправьте фото или описание! 🥗"},
        "generating":       {"en": "⏳ Generating summary…",
                             "ru": "⏳ Формирую сводку…"},
        "log_reset":        {"en": "✅ Daily log reset! 🌅",
                             "ru": "✅ Дневной журнал сброшен! 🌅"},
        "analysing":        {"en": "🔍 Analysing your meal…",
                             "ru": "🔍 Анализирую ваш приём пищи…"},
        "analyse_error":    {"en": "⚠️ Sorry, couldn't analyse that. Please try again.",
                             "ru": "⚠️ Извините, не удалось проанализировать. Попробуйте ещё раз."},
        "send_meal":        {"en": "Please send a meal photo or describe what you ate. 🍽️",
                             "ru": "Отправьте фото еды или опишите, что вы съели. 🍽️"},
        "no_profile":       {"en": "No profile found. Send /start to set one up.",
                             "ru": "Профиль не найден. Отправьте /start для настройки."},
        "setup_first":      {"en": "Please set up your profile first. Send /start",
                             "ru": "Сначала настройте профиль. Отправьте /start"},
        "cancelled":        {"en": "Setup cancelled. Send /start to try again.",
                             "ru": "Настройка отменена. Отправьте /start чтобы начать заново."},
        "training_set":     {"en": "🏋️ *Training day set!*",
                             "ru": "🏋️ *Тренировочный день выбран!*"},
        "rest_set":         {"en": "😴 *Rest day set!*",
                             "ru": "😴 *День отдыха выбран!*"},
        "calories":         {"en": "Calories", "ru": "Калории"},
        "protein":          {"en": "Protein",  "ru": "Белок"},
        "profile_title":    {"en": "👤 *Your Profile*", "ru": "👤 *Ваш профиль*"},
        "sex_label":        {"en": "Sex",    "ru": "Пол"},
        "age_label":        {"en": "Age",    "ru": "Возраст"},
        "weight_label":     {"en": "Weight", "ru": "Вес"},
        "goal_label":       {"en": "Goal",   "ru": "Цель"},
        "lang_label":       {"en": "Language", "ru": "Язык"},
        "training_label":   {"en": "🏋️ Training days", "ru": "🏋️ Тренировочные дни"},
        "rest_label":       {"en": "😴 Rest days",      "ru": "😴 Дни отдыха"},
        "edit_prompt":      {"en": "What would you like to update?",
                             "ru": "Что вы хотите изменить?"},
        "edit_sex":         {"en": "✏️ Sex",    "ru": "✏️ Пол"},
        "edit_age":         {"en": "✏️ Age",    "ru": "✏️ Возраст"},
        "edit_weight":      {"en": "✏️ Weight", "ru": "✏️ Вес"},
        "edit_goal":        {"en": "✏️ Goal",   "ru": "✏️ Цель"},
        "edit_cancel":      {"en": "✖️ Cancel", "ru": "✖️ Отмена"},
        "edit_height":      {"en": "✏️ Height", "ru": "✏️ Рост"},
        "height_label":     {"en": "Height", "ru": "Рост"},
        "ask_height":       {"en": "✅ Got it!\n\n*How tall are you in cm?*\n_(e.g. 160 — makes your calorie targets more accurate)_",
                             "ru": "✅ Отлично!\n\n*Ваш рост в см?*\n_(например, 160 — так цели по калориям будут точнее)_"},
        "bad_height":       {"en": "Please enter your height in cm (e.g. 160).",
                             "ru": "Введите рост в см (например, 160)."},
        "updated":          {"en": "✅ Updated!", "ru": "✅ Обновлено!"},
        "profile_deleted":  {"en": "Profile deleted. Send /start to set up a new one.",
                             "ru": "Профиль удалён. Отправьте /start для настройки нового."},
        "undo_success":     {"en": "↩️ Removed: *{label}* — {kcal:.0f} kcal | P {protein:.0f}g | C {carbs:.0f}g | F {fat:.0f}g\n\nRunning total: {total_kcal:.0f} kcal | {total_protein:.0f}g protein",
                             "ru": "↩️ Удалено: *{label}* — {kcal:.0f} ккал | Б {protein:.0f}г | У {carbs:.0f}г | Ж {fat:.0f}г\n\nИтого за день: {total_kcal:.0f} ккал | {total_protein:.0f}г белка"},
        "undo_empty":       {"en": "Nothing to undo — no meals logged yet today.",
                             "ru": "Нечего отменять — сегодня ещё нет записей."},
        "trial_warning":    {"en": "⚠️ *{days} free day(s) remaining* in your trial.",
                             "ru": "⚠️ *Осталось {days} бесплатных дней* пробного периода."},
        "paywall":          {"en": (
                                "⏳ *Your free trial has ended!*\n\n"
                                "Thank you for trying LeanRitual 🙏\n\n"
                                "To continue tracking your nutrition, unlock full access for just *$2 USD/month*:\n\n"
                                "1️⃣ Send $2 USD to:\n"
                                "`renatashaykheeva@gmail.com` *(PayPal)*\n\n"
                                "2️⃣ Send your *Telegram ID* below to the same email or to the bot owner so we can unlock you:\n"
                                "🪪 Your ID: `{user_id}`\n\n"
                                "✅ You'll be unlocked within a few hours of payment confirmation."
                            ),
                             "ru": (
                                "⏳ *Ваш пробный период завершён!*\n\n"
                                "Спасибо, что попробовали LeanRitual 🙏\n\n"
                                "Чтобы продолжить отслеживание питания, откройте полный доступ всего за *$2 в месяц*:\n\n"
                                "1️⃣ Отправьте $2 USD на:\n"
                                "`renatashaykheeva@gmail.com` *(PayPal)*\n\n"
                                "2️⃣ Отправьте ваш *Telegram ID* ниже на ту же почту или владельцу бота:\n"
                                "🪪 Ваш ID: `{user_id}`\n\n"
                                "✅ Вы будете разблокированы в течение нескольких часов после подтверждения оплаты."
                            )},
    }
    return strings.get(key, {}).get(lang, strings.get(key, {}).get("en", key))


def lang_of(profile: UserProfile | None) -> str:
    return profile.language if profile else "en"


# ── Keyboards ─────────────────────────────────────────────────────────────────
def lang_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[KeyboardButton("🇬🇧 English"), KeyboardButton("🇷🇺 Русский")]],
        resize_keyboard=True, one_time_keyboard=True,
    )


def home_keyboard(lang: str = "en") -> ReplyKeyboardMarkup:
    """Top-level section selector."""
    if lang == "ru":
        return ReplyKeyboardMarkup([
            [KeyboardButton("🥗 Питание"), KeyboardButton("🏋️ Фитнес")],
            [KeyboardButton("📅 Месячный чек-ин"), KeyboardButton("👤 Мой профиль")],
        ], resize_keyboard=True)
    return ReplyKeyboardMarkup([
        [KeyboardButton("🥗 Nutrition"), KeyboardButton("🏋️ Fitness")],
        [KeyboardButton("📅 Monthly Check-In"), KeyboardButton("👤 My Profile")],
    ], resize_keyboard=True)


def main_keyboard(lang: str = "en") -> ReplyKeyboardMarkup:
    """Nutrition section keyboard."""
    if lang == "ru":
        return ReplyKeyboardMarkup([
            [KeyboardButton("📊 Сводка за день"), KeyboardButton("📋 Приёмы пищи")],
            [KeyboardButton("🏋️ День тренировки"), KeyboardButton("😴 День отдыха")],
            [KeyboardButton("🎯 Цели"),            KeyboardButton("↩️ Отменить запись")],
            [KeyboardButton("🔙 Главное меню")],
        ], resize_keyboard=True)
    return ReplyKeyboardMarkup([
        [KeyboardButton("📊 Daily Summary"), KeyboardButton("📋 Today's Meals")],
        [KeyboardButton("🏋️ Training Day"),  KeyboardButton("😴 Rest Day")],
        [KeyboardButton("🎯 Targets"),       KeyboardButton("↩️ Undo Last Entry")],
        [KeyboardButton("🔙 Main Menu")],
    ], resize_keyboard=True)


def fitness_keyboard(lang: str = "en") -> ReplyKeyboardMarkup:
    """Fitness section keyboard."""
    if lang == "ru":
        return ReplyKeyboardMarkup([
            [KeyboardButton("📋 Мой план на неделю"), KeyboardButton("✅ Записать тренировку")],
            [KeyboardButton("🏃 Записать активность"), KeyboardButton("📈 Мой прогресс")],
            [KeyboardButton("🔙 Главное меню")],
        ], resize_keyboard=True)
    return ReplyKeyboardMarkup([
        [KeyboardButton("📋 My Weekly Plan"), KeyboardButton("✅ Log Workout")],
        [KeyboardButton("🏃 Log Activity"),   KeyboardButton("📈 My Progress")],
        [KeyboardButton("🔙 Main Menu")],
    ], resize_keyboard=True)


def sex_keyboard(lang: str = "en") -> ReplyKeyboardMarkup:
    if lang == "ru":
        return ReplyKeyboardMarkup(
            [[KeyboardButton("Женский"), KeyboardButton("Мужской")]],
            resize_keyboard=True, one_time_keyboard=True,
        )
    return ReplyKeyboardMarkup(
        [[KeyboardButton("Female"), KeyboardButton("Male")]],
        resize_keyboard=True, one_time_keyboard=True,
    )


def goal_keyboard(lang: str = "en") -> ReplyKeyboardMarkup:
    if lang == "ru":
        return ReplyKeyboardMarkup([
            [KeyboardButton("🔥 Похудение")],
            [KeyboardButton("💪 Набор мышц")],
            [KeyboardButton("⚖️ Поддержание веса")],
        ], resize_keyboard=True, one_time_keyboard=True)
    return ReplyKeyboardMarkup([
        [KeyboardButton("🔥 Fat Loss")],
        [KeyboardButton("💪 Muscle Gain")],
        [KeyboardButton("⚖️ Maintain Weight")],
    ], resize_keyboard=True, one_time_keyboard=True)


def edit_keyboard(lang: str = "en") -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([
        [KeyboardButton(t("edit_sex", lang)),  KeyboardButton(t("edit_age", lang))],
        [KeyboardButton(t("edit_weight", lang)), KeyboardButton(t("edit_goal", lang))],
        [KeyboardButton(t("edit_height", lang)), KeyboardButton(t("edit_cancel", lang))],
    ], resize_keyboard=True, one_time_keyboard=True)


# ── Onboarding ────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_allowed(update.effective_user.id):
        return ConversationHandler.END

    profile = get_profile(update.effective_user.id)
    fitness_profile = get_fitness_profile(update.effective_user.id)

    if profile and profile.is_complete() and fitness_profile and fitness_profile.is_complete():
        lang = lang_of(profile)
        await update.message.reply_text(
            t("welcome_back", lang), reply_markup=home_keyboard(lang),
        )
        return ConversationHandler.END

    # If nutrition is done but fitness is missing, jump straight to fitness questions
    if profile and profile.is_complete() and (not fitness_profile or not fitness_profile.is_complete()):
        lang = lang_of(profile)
        env_kb = ReplyKeyboardMarkup([
            [KeyboardButton("🏋️ Gym" if lang == "en" else "🏋️ Тренажёрный зал")],
            [KeyboardButton("🏠 Home (dumbbells & bodyweight)" if lang == "en" else "🏠 Дома (гантели и вес тела)")],
            [KeyboardButton("🔄 Both" if lang == "en" else "🔄 И там и там")],
        ], resize_keyboard=True, one_time_keyboard=True)
        context.user_data["ob_lang"] = lang
        await update.message.reply_text(
            "🏋️ *Let's finish your fitness profile!*\n\n*Where do you train?*"
            if lang == "en" else
            "🏋️ *Давайте завершим фитнес-профиль!*\n\n*Где вы тренируетесь?*",
            parse_mode="Markdown", reply_markup=env_kb,
        )
        return FIT_ENV

    await update.message.reply_text(
        t("choose_lang", "en"), reply_markup=lang_keyboard(),
    )
    return ASK_LANG


async def onboard_lang(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.lower()
    lang = "ru" if "русск" in text or "ru" in text else "en"
    context.user_data["ob_lang"] = lang

    await update.message.reply_text(
        t("ask_sex", lang), parse_mode="Markdown",
        reply_markup=sex_keyboard(lang),
    )
    return ASK_SEX


async def onboard_sex(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    lang = context.user_data.get("ob_lang", "en")
    text = update.message.text.lower()
    context.user_data["ob_sex"] = "female" if any(w in text for w in ["female", "женск"]) else "male"

    await update.message.reply_text(
        t("ask_age", lang), parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove(),
    )
    return ASK_AGE


async def onboard_age(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    lang = context.user_data.get("ob_lang", "en")
    try:
        age = int(update.message.text.strip())
        if not (10 <= age <= 100):
            raise ValueError
    except ValueError:
        await update.message.reply_text(t("bad_age", lang))
        return ASK_AGE

    context.user_data["ob_age"] = age
    await update.message.reply_text(t("ask_weight", lang), parse_mode="Markdown")
    return ASK_WEIGHT


async def onboard_weight(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    lang = context.user_data.get("ob_lang", "en")
    try:
        weight = float(update.message.text.strip().replace(",", "."))
        if not (30 <= weight <= 300):
            raise ValueError
    except ValueError:
        await update.message.reply_text(t("bad_weight", lang))
        return ASK_WEIGHT

    context.user_data["ob_weight"] = weight
    await update.message.reply_text(t("ask_height", lang), parse_mode="Markdown")
    return ASK_HEIGHT


def _parse_height(text: str) -> float | None:
    try:
        h = float(text.strip().lower().replace("cm", "").replace("см", "").replace(",", ".").strip())
        return h if 120 <= h <= 230 else None
    except ValueError:
        return None


async def onboard_height(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    lang = context.user_data.get("ob_lang", "en")
    height = _parse_height(update.message.text)
    if height is None:
        await update.message.reply_text(t("bad_height", lang))
        return ASK_HEIGHT
    context.user_data["ob_height"] = height
    await update.message.reply_text(
        t("ask_goal", lang), parse_mode="Markdown",
        reply_markup=goal_keyboard(lang),
    )
    return ASK_GOAL


async def onboard_goal(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    lang = context.user_data.get("ob_lang", "en")
    text = update.message.text.lower()
    if any(w in text for w in ["fat", "похуд"]):
        goal = GOAL_FAT_LOSS
    elif any(w in text for w in ["muscle", "набор"]):
        goal = GOAL_MUSCLE_GAIN
    else:
        goal = GOAL_MAINTAIN

    profile = UserProfile(
        age=context.user_data["ob_age"],
        sex=context.user_data["ob_sex"],
        weight_kg=context.user_data["ob_weight"],
        height_cm=context.user_data.get("ob_height", 0.0),
        goal=goal,
        language=lang,
    )
    save_profile(update.effective_user.id, profile)

    # Transition straight into fitness onboarding
    env_prompt = (
        "✅ *Nutrition profile saved!*\n\nNow let's set up your *fitness profile*.\n\n*Where do you train?*"
        if lang == "en" else
        "✅ *Профиль питания сохранён!*\n\nТеперь настроим ваш *фитнес-профиль*.\n\n*Где вы тренируетесь?*"
    )
    env_kb = ReplyKeyboardMarkup([
        [KeyboardButton("🏋️ Gym" if lang == "en" else "🏋️ Тренажёрный зал")],
        [KeyboardButton("🏠 Home (dumbbells & bodyweight)" if lang == "en" else "🏠 Дома (гантели и вес тела)")],
        [KeyboardButton("🔄 Both" if lang == "en" else "🔄 И там и там")],
    ], resize_keyboard=True, one_time_keyboard=True)
    await update.message.reply_text(env_prompt, parse_mode="Markdown", reply_markup=env_kb)
    return FIT_ENV


async def fit_onboard_env(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    lang = context.user_data.get("ob_lang", "en")
    text = update.message.text.lower()
    env = "gym" if any(w in text for w in ["gym", "тренаж"]) else "home" if any(w in text for w in ["home", "дома"]) else "both"
    context.user_data["fit_env"] = env

    level_kb = ReplyKeyboardMarkup([
        [KeyboardButton("🌱 Beginner (< 1 year)" if lang == "en" else "🌱 Начинающий (< 1 года)")],
        [KeyboardButton("💪 Intermediate (1–3 years)" if lang == "en" else "💪 Средний (1–3 года)")],
        [KeyboardButton("🔥 Advanced (3+ years)" if lang == "en" else "🔥 Продвинутый (3+ лет)")],
    ], resize_keyboard=True, one_time_keyboard=True)
    await update.message.reply_text(
        "*What is your current fitness level?*" if lang == "en" else "*Ваш уровень физической подготовки?*",
        parse_mode="Markdown", reply_markup=level_kb,
    )
    return FIT_LEVEL


async def fit_onboard_level(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    lang = context.user_data.get("ob_lang", "en")
    text = update.message.text.lower()
    level = "beginner" if any(w in text for w in ["beginner", "начин"]) else "advanced" if any(w in text for w in ["advanced", "продвин"]) else "intermediate"
    context.user_data["fit_level"] = level

    goal_kb = ReplyKeyboardMarkup([
        [KeyboardButton("🔥 Fat loss + strength" if lang == "en" else "🔥 Похудение + сила")],
        [KeyboardButton("💪 Muscle gain" if lang == "en" else "💪 Набор мышечной массы")],
        [KeyboardButton("⚡ General fitness + endurance" if lang == "en" else "⚡ Общая физическая форма")],
    ], resize_keyboard=True, one_time_keyboard=True)
    await update.message.reply_text(
        "*Primary fitness goal?*" if lang == "en" else "*Основная фитнес-цель?*",
        parse_mode="Markdown", reply_markup=goal_kb,
    )
    return FIT_GOAL


async def fit_onboard_goal(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    lang = context.user_data.get("ob_lang", "en")
    text = update.message.text.lower()
    fit_goal = "fat_loss_strength" if any(w in text for w in ["fat", "похуд"]) else "muscle_gain" if any(w in text for w in ["muscle", "набор"]) else "general_fitness"
    context.user_data["fit_goal"] = fit_goal

    await update.message.reply_text(
        "*Any injuries or areas to avoid?*\n_(e.g. 'bad left knee' — or type 'None')_"
        if lang == "en" else
        "*Есть ли травмы или ограничения?*\n_(например, 'колено' — или напишите 'Нет')_",
        parse_mode="Markdown", reply_markup=ReplyKeyboardRemove(),
    )
    return FIT_LIMITATIONS


async def fit_onboard_limitations(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    lang = context.user_data.get("ob_lang", "en")
    text = update.message.text.strip()
    limitations = "" if text.lower() in ["none", "нет", "no"] else text

    fitness_profile = FitnessProfile(
        training_environment=context.user_data["fit_env"],
        fitness_level=context.user_data["fit_level"],
        fitness_goal=context.user_data["fit_goal"],
        limitations=limitations,
    )
    save_fitness_profile(update.effective_user.id, fitness_profile)

    profile = get_profile(update.effective_user.id)
    targets_t = get_targets(DAY_TYPE_TRAINING, profile)
    targets_r = get_targets(DAY_TYPE_REST, profile)

    complete_msg = (
        f"🎉 *All set! Your LeanRitual profile is complete.*\n\n"
        f"🥗 *Nutrition:* Training {targets_t['kcal_min']}–{targets_t['kcal_max']} kcal | Rest {targets_r['kcal_min']}–{targets_r['kcal_max']} kcal\n"
        f"🏋️ *Fitness:* {fitness_profile.training_environment.title()} · {fitness_profile.fitness_level.title()} · {fitness_profile.fitness_goal.replace('_',' ').title()}\n\n"
        f"Use the menu to log meals, get your weekly plan, and track everything in one place!"
        if lang == "en" else
        f"🎉 *Готово! Ваш профиль LeanRitual создан.*\n\n"
        f"🥗 *Питание:* Тренировки {targets_t['kcal_min']}–{targets_t['kcal_max']} ккал | Отдых {targets_r['kcal_min']}–{targets_r['kcal_max']} ккал\n"
        f"🏋️ *Фитнес:* {fitness_profile.training_environment} · {fitness_profile.fitness_level} · {fitness_profile.fitness_goal}\n\n"
        f"Используйте меню ниже!"
    )
    await update.message.reply_text(complete_msg, parse_mode="Markdown", reply_markup=home_keyboard(lang))
    return ConversationHandler.END


async def onboard_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    lang = context.user_data.get("ob_lang", "en")
    await update.message.reply_text(t("cancelled", lang))
    return ConversationHandler.END


# ── Edit profile conversation ─────────────────────────────────────────────────
async def cmd_whitelist(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin command: /whitelist <user_id> — mark a user as paid."""
    if not ALLOWED_USERS or update.effective_user.id not in ALLOWED_USERS:
        return
    if not context.args:
        await update.message.reply_text("Usage: /whitelist <telegram_user_id>")
        return
    try:
        target_id = int(context.args[0])
        mark_paid(target_id)
        await update.message.reply_text(f"✅ User {target_id} has been unlocked.")
    except ValueError:
        await update.message.reply_text("Invalid user ID.")


async def edit_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point: show current profile then offer edit options."""
    if not is_allowed(update.effective_user.id):
        return ConversationHandler.END

    profile = get_profile(update.effective_user.id)
    if not profile:
        await update.message.reply_text(t("no_profile", "en"))
        return ConversationHandler.END

    lang = lang_of(profile)
    targets_t = get_targets(DAY_TYPE_TRAINING, profile)
    targets_r = get_targets(DAY_TYPE_REST, profile)
    goal_label = GOAL_LABELS.get(profile.goal, profile.goal)
    sex_display = ("Female" if profile.sex == "female" else "Male") if lang == "en" else ("Женский" if profile.sex == "female" else "Мужской")

    await update.message.reply_text(
        f"{t('profile_title', lang)}\n\n"
        f"{t('lang_label', lang)}: {'English' if lang == 'en' else 'Русский'}\n"
        f"{t('sex_label', lang)}: {sex_display}\n"
        f"{t('age_label', lang)}: {profile.age}\n"
        f"{t('weight_label', lang)}: {profile.weight_kg} kg\n"
        f"{t('height_label', lang)}: {str(int(profile.height_cm)) + ' cm' if profile.height_cm else '—'}\n"
        f"{t('goal_label', lang)}: {goal_label}\n\n"
        f"{t('training_label', lang)}: {targets_t['kcal_min']}–{targets_t['kcal_max']} kcal | {targets_t['protein_max']}g {t('protein', lang).lower()}\n"
        f"{t('rest_label', lang)}: {targets_r['kcal_min']}–{targets_r['kcal_max']} kcal | {targets_r['protein_max']}g {t('protein', lang).lower()}\n\n"
        f"{t('edit_prompt', lang)}",
        parse_mode="Markdown",
        reply_markup=edit_keyboard(lang),
    )
    return EDIT_CHOOSE


async def edit_choose(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    profile = get_profile(update.effective_user.id)
    lang = lang_of(profile)
    text = update.message.text

    cancel_texts = [t("edit_cancel", "en"), t("edit_cancel", "ru"), "cancel", "отмена"]
    if any(c.lower() in text.lower() for c in cancel_texts):
        await update.message.reply_text(
            t("welcome_back", lang), reply_markup=main_keyboard(lang)
        )
        return ConversationHandler.END

    context.user_data["editing"] = text

    if any(w in text.lower() for w in ["sex", "пол"]):
        await update.message.reply_text(t("ask_sex", lang), parse_mode="Markdown", reply_markup=sex_keyboard(lang))
        return EDIT_SEX
    elif any(w in text.lower() for w in ["age", "возраст"]):
        await update.message.reply_text(t("ask_age", lang), parse_mode="Markdown", reply_markup=ReplyKeyboardRemove())
        return EDIT_AGE
    elif any(w in text.lower() for w in ["height", "рост"]):
        await update.message.reply_text(t("ask_height", lang), parse_mode="Markdown", reply_markup=ReplyKeyboardRemove())
        return EDIT_HEIGHT
    elif any(w in text.lower() for w in ["weight", "вес"]):
        await update.message.reply_text(t("ask_weight", lang), parse_mode="Markdown", reply_markup=ReplyKeyboardRemove())
        return EDIT_WEIGHT
    elif any(w in text.lower() for w in ["goal", "цель"]):
        await update.message.reply_text(t("ask_goal", lang), parse_mode="Markdown", reply_markup=goal_keyboard(lang))
        return EDIT_GOAL

    return ConversationHandler.END


async def edit_sex(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    profile = get_profile(update.effective_user.id)
    lang = lang_of(profile)
    text = update.message.text.lower()
    profile.sex = "female" if any(w in text for w in ["female", "женск"]) else "male"
    save_profile(update.effective_user.id, profile)
    await update.message.reply_text(t("updated", lang), reply_markup=main_keyboard(lang))
    return ConversationHandler.END


async def edit_age(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    profile = get_profile(update.effective_user.id)
    lang = lang_of(profile)
    try:
        age = int(update.message.text.strip())
        if not (10 <= age <= 100):
            raise ValueError
        profile.age = age
        save_profile(update.effective_user.id, profile)
        await update.message.reply_text(t("updated", lang), reply_markup=main_keyboard(lang))
        return ConversationHandler.END
    except ValueError:
        await update.message.reply_text(t("bad_age", lang))
        return EDIT_AGE


async def edit_weight(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    profile = get_profile(update.effective_user.id)
    lang = lang_of(profile)
    try:
        weight = float(update.message.text.strip().replace(",", "."))
        if not (30 <= weight <= 300):
            raise ValueError
        profile.weight_kg = weight
        save_profile(update.effective_user.id, profile)
        await update.message.reply_text(t("updated", lang), reply_markup=main_keyboard(lang))
        return ConversationHandler.END
    except ValueError:
        await update.message.reply_text(t("bad_weight", lang))
        return EDIT_WEIGHT


async def edit_height(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    profile = get_profile(update.effective_user.id)
    lang = lang_of(profile)
    height = _parse_height(update.message.text)
    if height is None:
        await update.message.reply_text(t("bad_height", lang))
        return EDIT_HEIGHT
    profile.height_cm = height
    save_profile(update.effective_user.id, profile)
    await update.message.reply_text(t("updated", lang), reply_markup=main_keyboard(lang))
    return ConversationHandler.END


async def edit_goal(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    profile = get_profile(update.effective_user.id)
    lang = lang_of(profile)
    text = update.message.text.lower()
    if any(w in text for w in ["fat", "похуд"]):
        profile.goal = GOAL_FAT_LOSS
    elif any(w in text for w in ["muscle", "набор"]):
        profile.goal = GOAL_MUSCLE_GAIN
    else:
        profile.goal = GOAL_MAINTAIN
    save_profile(update.effective_user.id, profile)
    await update.message.reply_text(t("updated", lang), reply_markup=main_keyboard(lang))
    return ConversationHandler.END


# ── Safe sending (Telegram HTML, never fails on formatting) ──────────────────
async def send_html(message, text_html: str, reply_markup=None, edit: bool = False):
    """
    Send (or edit into) a message as Telegram HTML. Splits long text, and falls
    back to plain text if Telegram rejects the markup. Returns the last Message.
    """
    chunks = split_message(text_html)
    sent = None
    for i, chunk in enumerate(chunks):
        is_last = i == len(chunks) - 1
        markup = reply_markup if is_last else None
        try:
            if edit and i == 0:
                sent = await message.edit_text(chunk, parse_mode="HTML", reply_markup=markup)
            else:
                sent = await message.reply_text(chunk, parse_mode="HTML", reply_markup=markup)
        except BadRequest as err:
            logger.warning("HTML send failed (%s) — falling back to plain text", err)
            plain = strip_tags(chunk)
            if edit and i == 0:
                sent = await message.edit_text(plain, reply_markup=markup)
            else:
                sent = await message.reply_text(plain, reply_markup=markup)
    return sent


async def send_ai_text(message, raw_markdown: str, reply_markup=None, edit: bool = False):
    """Render free-text Claude output (Markdown) safely."""
    return await send_html(message, md_to_tg_html(raw_markdown), reply_markup=reply_markup, edit=edit)


# ── Day type (persisted per day, no longer lost on redeploy) ─────────────────
def current_day_type(user_id: int, log=None) -> str:
    log = log or get_today_log(user_id)
    if log.day_type:
        return log.day_type
    # Auto: a workout logged today makes it a training day
    try:
        from fitness_storage import get_workout_log
        wl = get_workout_log(user_id)
        if wl and wl.type == "workout":
            return DAY_TYPE_TRAINING
    except Exception:
        pass
    return DAY_TYPE_TRAINING


def set_day_type(user_id: int, day_type: str) -> None:
    log = get_today_log(user_id)
    log.day_type = day_type
    save_log(user_id, log)


# ── Meal logging ──────────────────────────────────────────────────────────────
def meal_buttons(date_str: str, meal_id: str, lang: str) -> InlineKeyboardMarkup:
    fix = "✏️ Fix" if lang == "en" else "✏️ Исправить"
    rm = "🗑 Remove" if lang == "en" else "🗑 Удалить"
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(fix, callback_data=f"meal|fix|{date_str}|{meal_id}"),
        InlineKeyboardButton(rm, callback_data=f"meal|del|{date_str}|{meal_id}"),
    ]])


async def download_image(context, file_id: str) -> str:
    file = await context.bot.get_file(file_id)
    data = await file.download_as_bytearray()
    return base64.b64encode(bytes(data)).decode("utf-8")


def _goal_label(profile) -> str:
    return GOAL_LABELS.get(profile.goal, profile.goal) if profile else ""


async def log_meal(update: Update, context: ContextTypes.DEFAULT_TYPE, profile, *,
                   image_file_id: str | None, media_type: str, description: str) -> None:
    user_id = update.effective_user.id
    lang = lang_of(profile)
    log = get_today_log(user_id)
    day_type = current_day_type(user_id, log)
    targets = get_targets(day_type, profile)
    status_msg = await update.message.reply_text(t("analysing", lang))

    try:
        image_b64 = await download_image(context, image_file_id) if image_file_id else None
        est = await meal_ai.estimate_meal(
            async_client, ANTHROPIC_MODEL,
            image_b64=image_b64, media_type=media_type, description=description,
            profile=profile, goal_label=_goal_label(profile),
            protein_left=targets["protein_max"] - log.total_protein, lang=lang,
        )
    except Exception as err:
        logger.exception("Meal estimation failed: %s", err)
        await status_msg.edit_text(f"{t('analyse_error', lang)}\n({api_error_reason(err)})")
        return

    if not est["is_food"]:
        q = est["clarification_question"] or (
            "I couldn't identify a meal here — could you describe what you ate?" if lang == "en"
            else "Не получилось распознать блюдо — опишите, пожалуйста, что вы съели?")
        await status_msg.edit_text(f"🤔 {q}")
        return

    entry = {
        "name": est["name"], "items": est["items"],
        "kcal": est["kcal"], "protein": est["protein"], "carbs": est["carbs"],
        "fat": est["fat"], "fibre": est["fibre"],
        "assumptions": est["assumptions"], "confidence": est["confidence"], "tip": est["tip"],
        "description": description, "photo_file_id": image_file_id, "media_type": media_type,
    }
    entry = log.add_meal_entry(entry)
    entry["bot_message_id"] = status_msg.message_id
    save_log(user_id, log)

    text = render_meal(entry, log, targets, day_type, lang,
                       show_fix_hint=bool(image_file_id and not description))
    await send_html(status_msg, text, reply_markup=meal_buttons(log.date, entry["id"], lang), edit=True)


async def apply_correction(update: Update, context: ContextTypes.DEFAULT_TYPE, profile,
                           date_str: str, meal_id: str, correction: str) -> None:
    user_id = update.effective_user.id
    lang = lang_of(profile)
    log = get_log(user_id, date_str)
    meal = log.find_meal(meal_id)
    if not meal:
        await update.message.reply_text(
            "I couldn't find that meal any more." if lang == "en" else "Не нашёл эту запись.")
        return
    status_msg = await update.message.reply_text(
        "✏️ Updating that meal…" if lang == "en" else "✏️ Обновляю запись…")
    try:
        image_b64 = None
        if meal.get("photo_file_id"):
            try:
                image_b64 = await download_image(context, meal["photo_file_id"])
            except Exception:
                logger.warning("Could not re-download photo for correction")
        est = await meal_ai.correct_meal(
            async_client, ANTHROPIC_MODEL, previous=meal, correction=correction,
            image_b64=image_b64, media_type=meal.get("media_type", "image/jpeg"),
            profile=profile, goal_label=_goal_label(profile), lang=lang,
        )
    except Exception as err:
        logger.exception("Correction failed: %s", err)
        await status_msg.edit_text(t("analyse_error", lang))
        return

    description = " · ".join(x for x in [meal.get("description", ""), correction] if x)
    new_entry = {
        "name": est["name"], "items": est["items"],
        "kcal": est["kcal"], "protein": est["protein"], "carbs": est["carbs"],
        "fat": est["fat"], "fibre": est["fibre"],
        "assumptions": est["assumptions"], "confidence": est["confidence"], "tip": est["tip"],
        "description": description,
    }
    updated = log.replace_meal(meal_id, new_entry)
    updated["bot_message_id"] = status_msg.message_id
    save_log(user_id, log)

    day_type = current_day_type(user_id, log) if log.date == get_today_log(user_id).date else (log.day_type or DAY_TYPE_TRAINING)
    targets = get_targets(day_type, profile)
    text = render_meal(updated, log, targets, day_type, lang, updated=True)
    await send_html(status_msg, text, reply_markup=meal_buttons(log.date, meal_id, lang), edit=True)


async def on_meal_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user_id = query.from_user.id
    profile = get_profile(user_id)
    lang = lang_of(profile)
    try:
        _, action, date_str, meal_id = query.data.split("|", 3)
    except ValueError:
        await query.answer()
        return
    log = get_log(user_id, date_str)
    meal = log.find_meal(meal_id)
    if not meal:
        await query.answer("Already removed" if lang == "en" else "Уже удалено", show_alert=False)
        return

    if action == "del":
        log.remove_meal(meal_id)
        save_log(user_id, log)
        await query.answer("Removed" if lang == "en" else "Удалено")
        note = (f"🗑 <s>{meal_ai.e(meal.get('name', 'Meal'))} — {meal.get('kcal', 0):.0f} kcal</s>\n"
                f"Removed. Today: {log.total_kcal:.0f} kcal · protein {log.total_protein:.0f} g"
                if lang == "en" else
                f"🗑 <s>{meal_ai.e(meal.get('name', 'Блюдо'))} — {meal.get('kcal', 0):.0f} ккал</s>\n"
                f"Удалено. Сегодня: {log.total_kcal:.0f} ккал · белок {log.total_protein:.0f} г")
        try:
            await query.edit_message_text(note, parse_mode="HTML")
        except BadRequest:
            await query.message.reply_text(strip_tags(note))
        return

    if action == "fix":
        context.user_data["fixing_meal"] = {"date": date_str, "id": meal_id}
        await query.answer()
        await query.message.reply_text(
            f"✏️ What should I change about “{meal.get('name', 'this meal')}”?\n\n"
            "e.g. “it was 2 eggs, not 3”, “cooked in 1 tbsp butter”, “half the rice”, “no sauce”."
            if lang == "en" else
            f"✏️ Что изменить в «{meal.get('name', 'этом блюде')}»?\n\n"
            "например: «яиц было 2, а не 3», «жарила на 1 ст. л. сливочного масла», «риса в два раза меньше».",
        )


# ── Today's meals / summary / targets ────────────────────────────────────────
def _status(value: float, lo: float, hi: float, higher_is_ok: bool = False) -> str:
    if lo <= value <= hi:
        return "🟢"
    if higher_is_ok and value > hi:
        return "🟢"
    gap = (lo - value) / lo if value < lo else (value - hi) / hi
    return "🟡" if gap <= 0.1 else "🔴"


def render_totals(log, targets: dict, day_type: str, lang: str) -> str:
    dt = ("🏋️ Training day" if day_type == DAY_TYPE_TRAINING else "😴 Rest day") if lang == "en" else \
         ("🏋️ Тренировочный день" if day_type == DAY_TYPE_TRAINING else "😴 День отдыха")
    names = {"en": ("Calories", "Protein", "Carbs", "Fat", "Fibre"),
             "ru": ("Калории", "Белок", "Углеводы", "Жиры", "Клетчатка")}[lang if lang in ("en", "ru") else "en"]
    rows = [
        (names[0], log.total_kcal, targets["kcal_min"], targets["kcal_max"], "kcal", False),
        (names[1], log.total_protein, targets["protein_min"], targets["protein_max"], "g", True),
        (names[2], log.total_carbs, targets["carbs_min"], targets["carbs_max"], "g", False),
        (names[3], log.total_fat, targets["fat_min"], targets["fat_max"], "g", False),
    ]
    lines = [f"<b>{dt}</b>"]
    for label, val, lo, hi, unit, hi_ok in rows:
        lines.append(f"{_status(val, lo, hi, hi_ok)} {label}: <b>{val:.0f}</b> / {lo}–{hi} {unit}")
    if log.total_fibre:
        lines.append(f"{'🟢' if log.total_fibre >= 25 else '🟡'} {names[4]}: <b>{log.total_fibre:.0f}</b> / 25+ g")
    return "\n".join(lines)


async def show_today(update: Update, profile) -> None:
    user_id = update.effective_user.id
    lang = lang_of(profile)
    log = get_today_log(user_id)
    if not log.meals:
        await update.message.reply_text(t("no_meals", lang))
        return
    day_type = current_day_type(user_id, log)
    targets = get_targets(day_type, profile)
    lines = ["📋 <b>" + ("Today's meals" if lang == "en" else "Приёмы пищи сегодня") + "</b>", ""]
    for m in log.meals:
        lines.append(f"{m['meal_num']}. {meal_ai.e(m.get('time', ''))} <b>{meal_ai.e(m.get('name') or 'Meal')}</b> — "
                     f"{m.get('kcal', 0):.0f} kcal · P {m.get('protein', 0):.0f}")
    lines += ["", render_totals(log, targets, day_type, lang)]
    rows = [[
        InlineKeyboardButton(f"✏️ {m['meal_num']}", callback_data=f"meal|fix|{log.date}|{m['id']}"),
        InlineKeyboardButton(f"🗑 {m['meal_num']}", callback_data=f"meal|del|{log.date}|{m['id']}"),
    ] for m in log.meals[-8:]]
    await send_html(update.message, "\n".join(lines), reply_markup=InlineKeyboardMarkup(rows))


async def show_summary(update: Update, profile) -> None:
    user_id = update.effective_user.id
    lang = lang_of(profile)
    log = get_today_log(user_id)
    if not log.meals:
        await update.message.reply_text(t("no_meals", lang))
        return
    day_type = current_day_type(user_id, log)
    targets = get_targets(day_type, profile)
    header = "📊 <b>" + ("Daily Summary" if lang == "en" else "Сводка за день") + "</b>\n\n" + \
             render_totals(log, targets, day_type, lang)
    status = await update.message.reply_text(t("generating", lang))
    try:
        workout_note = ""
        from fitness_storage import get_workout_log
        wl = get_workout_log(user_id)
        if wl:
            workout_note = f"logged {wl.type}: {wl.workout_day or wl.activity_type} {wl.duration_min or ''} min".strip()
        prompt = build_summary_prompt(log, targets, profile, day_type=day_type, workout_note=workout_note)
        coaching = await call_claude(prompt, max_tokens=700)
        body = header + "\n\n" + md_to_tg_html(coaching)
    except Exception as err:
        logger.exception("Summary failed: %s", err)
        body = header
    await send_html(status, body, edit=True)


def _targets_text(profile, lang: str) -> str:
    out = ["🎯 <b>" + ("Your daily targets" if lang == "en" else "Ваши дневные цели") + "</b>", ""]
    for dt in (DAY_TYPE_TRAINING, DAY_TYPE_REST):
        tg = get_targets(dt, profile)
        label = ("🏋️ Training day" if dt == DAY_TYPE_TRAINING else "😴 Rest day") if lang == "en" else \
                ("🏋️ Тренировочный день" if dt == DAY_TYPE_TRAINING else "😴 День отдыха")
        src = ("custom" if tg.get("custom") else "auto") if lang == "en" else ("вручную" if tg.get("custom") else "авто")
        out.append(f"<b>{label}</b> <i>({src})</i>")
        out.append(f"  {tg['kcal_min']}–{tg['kcal_max']} kcal · P {tg['protein_min']}–{tg['protein_max']} g · "
                   f"C {tg['carbs_min']}–{tg['carbs_max']} g · F {tg['fat_min']}–{tg['fat_max']} g")
    out += ["", ("<i>Auto = calculated from your age, sex, height, weight and goal. "
                 "Carbs fill whatever calories are left after protein and fat.</i>") if lang == "en" else
                ("<i>Авто — расчёт по возрасту, полу, росту, весу и цели. "
                 "Углеводы заполняют оставшиеся калории после белка и жиров.</i>")]
    return "\n".join(out)


def targets_buttons(lang: str) -> InlineKeyboardMarkup:
    en = lang == "en"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ Training day" if en else "✏️ Тренировка", callback_data="tgt|set|training"),
         InlineKeyboardButton("✏️ Rest day" if en else "✏️ Отдых", callback_data="tgt|set|rest")],
        [InlineKeyboardButton("↺ Use auto-calculated" if en else "↺ Вернуть авто", callback_data="tgt|auto|all")],
    ])


async def show_targets(update: Update, profile) -> None:
    lang = lang_of(profile)
    await send_html(update.message, _targets_text(profile, lang), reply_markup=targets_buttons(lang))


async def on_targets_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user_id = query.from_user.id
    profile = get_profile(user_id)
    lang = lang_of(profile)
    await query.answer()
    if not profile:
        return
    _, action, which = query.data.split("|", 2)
    if action == "auto":
        profile.custom_targets = {}
        save_profile(user_id, profile)
        await query.edit_message_text(_targets_text(profile, lang), parse_mode="HTML",
                                      reply_markup=targets_buttons(lang))
        return
    context.user_data["setting_targets"] = which
    label = ("training" if which == "training" else "rest") if lang == "en" else \
            ("тренировочного" if which == "training" else "дня отдыха")
    await query.message.reply_text(
        f"Send your {label} day calories and protein, e.g. <code>1750 120</code>\n"
        "Optionally add fat in grams: <code>1750 120 55</code>"
        if lang == "en" else
        f"Отправьте калории и белок для {label} дня, например <code>1750 120</code>\n"
        "Можно добавить жиры в граммах: <code>1750 120 55</code>",
        parse_mode="HTML",
    )


async def save_custom_targets(update: Update, context: ContextTypes.DEFAULT_TYPE, profile, text: str) -> bool:
    """Handle the reply after tapping ✏️ in Targets. Returns True if handled."""
    which = context.user_data.get("setting_targets")
    if not which:
        return False
    lang = lang_of(profile)
    nums = [float(n.replace(",", ".")) for n in re.findall(r"\d+(?:[.,]\d+)?", text)]
    if len(nums) < 2 or not (800 <= nums[0] <= 5000) or not (30 <= nums[1] <= 350) or \
            (len(nums) > 2 and not (20 <= nums[2] <= 200)):
        await update.message.reply_text(
            "Please send two numbers like 1750 120 (calories, protein g), optionally fat: 1750 120 55"
            if lang == "en" else
            "Отправьте два числа, например 1750 120 (калории, белок г), можно добавить жиры: 1750 120 55")
        return True
    custom = {"kcal": int(nums[0]), "protein": int(nums[1])}
    if len(nums) > 2:
        custom["fat"] = int(nums[2])
    profile.custom_targets = dict(profile.custom_targets or {})
    profile.custom_targets[which] = custom
    save_profile(update.effective_user.id, profile)
    context.user_data.pop("setting_targets", None)
    await send_html(update.message, "✅ " + _targets_text(profile, lang),
                    reply_markup=targets_buttons(lang))
    return True


# ── Fitness Claude API calls ──────────────────────────────────────────────────
def api_error_reason(err: Exception) -> str:
    """Short, human-readable reason for a failed Claude call (shown to the user + logged)."""
    name = type(err).__name__
    status = getattr(err, "status_code", None)
    mapping = {
        "NotFoundError": "model not available on this API key",
        "AuthenticationError": "API key rejected",
        "PermissionDeniedError": "API key lacks access",
        "RateLimitError": "rate limit hit",
        "APITimeoutError": "request timed out",
        "APIConnectionError": "couldn't reach Claude",
        "InternalServerError": "Claude is overloaded",
        "BadRequestError": "request rejected",
    }
    reason = mapping.get(name, name)
    if status == 529:
        reason = "Claude is overloaded"
    msg = str(getattr(err, "message", "") or err)
    if "credit" in msg.lower() and "balance" in msg.lower():
        reason = "API credit balance too low"
    return reason


async def call_claude(prompt: str, max_tokens: int = 1500) -> str:
    """Text completion with automatic fallback to FALLBACK_MODEL. Non-blocking."""
    last_err = None
    for model in dict.fromkeys([ANTHROPIC_MODEL, FALLBACK_MODEL]):
        try:
            response = await async_client.messages.create(
                model=model, max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}],
            )
            text = "".join(b.text for b in response.content if getattr(b, "type", "") == "text")
            if model != ANTHROPIC_MODEL:
                logger.warning("Used fallback model %s", model)
            return text
        except Exception as err:
            last_err = err
            logger.error("Claude call failed on %s (%s): %s", model, api_error_reason(err), err)
            if type(err).__name__ in ("AuthenticationError",) or "credit" in str(err).lower():
                break  # a different model won't help
    raise last_err


def extract_tagged(text: str, tag: str) -> dict | None:
    match = re.search(rf"<{tag}>(.*?)</{tag}>", text, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(1).strip())
    except json.JSONDecodeError:
        return None


# ── Fitness section handlers ──────────────────────────────────────────────────
async def handle_weekly_plan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    profile = get_profile(user_id)
    lang = lang_of(profile)
    fitness_profile = get_fitness_profile(user_id)

    if not fitness_profile:
        await update.message.reply_text(
            "Please complete setup first — send /start" if lang == "en"
            else "Сначала завершите настройку — отправьте /start"
        )
        return

    status = await update.message.reply_text(
        "⏳ Generating your weekly plan…" if lang == "en" else "⏳ Составляю план на неделю…"
    )

    recent_weeks = get_recent_weeks(user_id, 4)
    recent_logs = get_recent_workout_logs(user_id, 28)
    day_log = get_today_log(user_id)
    day_type = context.user_data.get("day_type", DAY_TYPE_TRAINING)
    nutrition_targets = get_targets(day_type, profile)

    prompt = build_weekly_plan_prompt(
        fitness_profile=fitness_profile,
        user_profile=profile,
        recent_weeks=recent_weeks,
        recent_logs=recent_logs,
        today_log=day_log,
        nutrition_targets=nutrition_targets,
        lang=lang,
    )

    try:
        plan_text = await call_claude(prompt, max_tokens=2000)
    except Exception as e:
        logger.error("Claude error generating plan: %s", e)
        await status.edit_text(
            f"⚠️ Couldn't generate your plan ({api_error_reason(e)}). Please try again in a minute."
            if lang == "en" else
            f"⚠️ Не удалось составить план ({api_error_reason(e)}). Попробуйте через минуту.")
        return

    # Save the plan
    week_key = current_week_key()
    plan = WeeklyPlan(
        week_key=week_key,
        generated_on=today_str(),
        plan_text=plan_text,
        days=[],
    )
    save_weekly_plan(user_id, plan)

    # Increment weeks_completed
    fitness_profile.weeks_completed += 1
    save_fitness_profile(user_id, fitness_profile)

    await send_ai_text(status, plan_text, edit=True)


async def handle_log_workout_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    profile = get_profile(user_id)
    lang = lang_of(profile)

    prompt = (
        "💪 *Log your workout!*\n\nTell me what you did — exercises, sets, reps, and weights if you know them.\n\n"
        "_Example: 'Squats 4x8 @ 60kg, Romanian deadlift 3x10 @ 50kg, leg press 3x12'_"
        if lang == "en" else
        "💪 *Запишите тренировку!*\n\nРасскажите, что вы делали — упражнения, подходы, повторения и веса.\n\n"
        "_Пример: 'Приседания 4x8 @ 60кг, румынская тяга 3x10 @ 50кг'_"
    )
    await update.message.reply_text(prompt, parse_mode="Markdown", reply_markup=ReplyKeyboardRemove())
    return LOG_WORKOUT_TEXT


async def handle_log_workout_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    profile = get_profile(user_id)
    lang = lang_of(profile)
    fitness_profile = get_fitness_profile(user_id)
    workout_text = update.message.text

    status = await update.message.reply_text(
        "⏳ Logging your workout…" if lang == "en" else "⏳ Записываю тренировку…"
    )

    current_plan = get_weekly_plan(user_id)
    day_log = get_today_log(user_id)
    day_type = context.user_data.get("day_type", DAY_TYPE_TRAINING)
    nutrition_targets = get_targets(day_type, profile)

    prompt = build_workout_log_prompt(
        workout_text=workout_text,
        fitness_profile=fitness_profile,
        user_profile=profile,
        current_plan=current_plan,
        today_log=day_log,
        nutrition_targets=nutrition_targets,
        lang=lang,
    )

    try:
        response = await call_claude(prompt)
    except Exception as e:
        logger.error("Claude error logging workout: %s", e)
        await status.edit_text("⚠️ Error logging workout. Please try again.")
        await update.message.reply_text("🏋️", reply_markup=fitness_keyboard(lang))
        return ConversationHandler.END

    # Extract and save workout data
    workout_data = extract_tagged(response, "workout_data")
    if workout_data:
        log = WorkoutLog(
            date=today_str(),
            type="workout",
            workout_day=workout_data.get("workout_day", "Workout"),
            exercises=workout_data.get("exercises", []),
            perceived_effort=workout_data.get("perceived_effort", 0),
            duration_min=workout_data.get("duration_min", 0),
        )
        save_workout_log(user_id, log)

        # Mark day complete in weekly plan
        if current_plan and log.workout_day not in current_plan.completed_days:
            current_plan.completed_days.append(log.workout_day)
            save_weekly_plan(user_id, current_plan)

    clean = re.sub(r"<workout_data>.*?</workout_data>", "", response, flags=re.DOTALL).strip()
    if workout_data:
        set_day_type(user_id, DAY_TYPE_TRAINING)
    await status.delete()
    await send_ai_text(update.message, clean, reply_markup=fitness_keyboard(lang))
    return ConversationHandler.END


async def handle_log_activity_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    profile = get_profile(user_id)
    lang = lang_of(profile)

    prompt = (
        "🏃 *Log an activity!*\n\nWhat did you do outside your plan?\n\n"
        "_Example: 'Went for a 5km run, about 35 minutes' or 'Hot yoga class, 60 min'_"
        if lang == "en" else
        "🏃 *Запишите активность!*\n\nЧто вы делали вне плана?\n\n"
        "_Пример: 'Пробежал 5км, около 35 минут' или 'Горячая йога, 60 мин'_"
    )
    await update.message.reply_text(prompt, parse_mode="Markdown", reply_markup=ReplyKeyboardRemove())
    return LOG_ACTIVITY_TEXT


async def handle_log_activity_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    profile = get_profile(user_id)
    lang = lang_of(profile)
    fitness_profile = get_fitness_profile(user_id)
    activity_text = update.message.text

    status = await update.message.reply_text(
        "⏳ Logging your activity…" if lang == "en" else "⏳ Записываю активность…"
    )

    current_plan = get_weekly_plan(user_id)
    day_log = get_today_log(user_id)
    day_type = context.user_data.get("day_type", DAY_TYPE_TRAINING)
    nutrition_targets = get_targets(day_type, profile)

    prompt = build_activity_log_prompt(
        activity_text=activity_text,
        fitness_profile=fitness_profile,
        user_profile=profile,
        current_plan=current_plan,
        today_log=day_log,
        nutrition_targets=nutrition_targets,
        lang=lang,
    )

    try:
        response = await call_claude(prompt)
    except Exception as e:
        logger.error("Claude error logging activity: %s", e)
        await status.edit_text("⚠️ Error. Please try again.")
        return ConversationHandler.END

    activity_data = extract_tagged(response, "activity_data")
    if activity_data:
        log = WorkoutLog(
            date=today_str(),
            type="activity",
            activity_type=activity_data.get("activity_type", "activity"),
            duration_min=activity_data.get("duration_min", 0),
            perceived_effort=activity_data.get("perceived_effort", 0),
        )
        save_workout_log(user_id, log)

    clean = re.sub(r"<activity_data>.*?</activity_data>", "", response, flags=re.DOTALL).strip()
    await status.delete()
    await send_ai_text(update.message, clean, reply_markup=fitness_keyboard(lang))
    return ConversationHandler.END


async def handle_progress(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    profile = get_profile(user_id)
    lang = lang_of(profile)
    fitness_profile = get_fitness_profile(user_id)

    if not fitness_profile:
        await update.message.reply_text("No fitness profile found. Send /start to set up.")
        return

    status = await update.message.reply_text(
        "⏳ Analysing your progress…" if lang == "en" else "⏳ Анализирую прогресс…"
    )
    recent_weeks = get_recent_weeks(user_id, 4)
    recent_logs = get_recent_workout_logs(user_id, 28)

    prompt = build_progress_prompt(fitness_profile, profile, recent_weeks, recent_logs, lang)
    try:
        response = await call_claude(prompt, max_tokens=800)
        await send_ai_text(status, response, edit=True)
    except Exception as e:
        logger.error("Progress error: %s", e)
        await status.edit_text("⚠️ Error. Please try again.")


# ── Monthly check-in handlers ─────────────────────────────────────────────────
async def checkin_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    profile = get_profile(user_id)
    lang = lang_of(profile)

    prompt = (
        "📅 *Monthly Check-In*\n\nLet's review your progress!\n\n*What is your current weight in kg?*"
        if lang == "en" else
        "📅 *Месячный чек-ин*\n\nДавайте подведём итоги!\n\n*Ваш текущий вес в кг?*"
    )
    await update.message.reply_text(prompt, parse_mode="Markdown", reply_markup=ReplyKeyboardRemove())
    return CHECKIN_WEIGHT


async def checkin_weight(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    profile = get_profile(user_id)
    lang = lang_of(profile)

    try:
        weight = float(update.message.text.strip().replace(",", "."))
        if not (30 <= weight <= 300):
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            "Please enter a valid weight (e.g. 62.5)." if lang == "en"
            else "Введите корректный вес (например, 62.5)."
        )
        return CHECKIN_WEIGHT

    context.user_data["checkin_weight"] = weight
    await update.message.reply_text(
        "✅ Got it!\n\n*How has this month been?* Any highlights, struggles, or things you want the coach to know?\n_(Or just type 'nothing special')_"
        if lang == "en" else
        "✅ Отлично!\n\n*Как прошёл этот месяц?* Успехи, трудности, или что-то важное для тренера?\n_(Или напишите 'всё обычно')_",
        parse_mode="Markdown",
    )
    return CHECKIN_NOTES


async def checkin_notes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    profile = get_profile(user_id)
    lang = lang_of(profile)
    fitness_profile = get_fitness_profile(user_id)
    notes = update.message.text
    weight = context.user_data["checkin_weight"]

    status = await update.message.reply_text(
        "⏳ Generating your monthly review…" if lang == "en" else "⏳ Формирую месячный отчёт…"
    )

    recent_weeks = get_recent_weeks(user_id, 4)
    checkin_status = get_checkin_status(user_id)

    prompt = build_monthly_checkin_prompt(
        current_weight_kg=weight,
        user_notes=notes,
        user_profile=profile,
        fitness_profile=fitness_profile,
        recent_weeks=recent_weeks,
        checkin_history=checkin_status.checkin_history,
        lang=lang,
    )

    try:
        response = await call_claude(prompt, max_tokens=1200)
    except Exception as e:
        logger.error("Check-in error: %s", e)
        await status.edit_text("⚠️ Error generating review. Please try again.")
        return ConversationHandler.END

    record_checkin(user_id, weight, notes, response)
    await status.delete()
    await send_ai_text(update.message, response, reply_markup=home_keyboard(lang))
    return ConversationHandler.END


# ── Monthly check-in scheduler job ───────────────────────────────────────────
async def scheduled_checkin_reminder(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Runs daily — sends check-in prompt on the 1st of each month."""
    now = datetime.now(USER_TIMEZONE)
    if now.day != 1:
        return

    data_dir = context.bot_data.get("data_dir", "data")
    for user_dir in Path(data_dir).iterdir():
        if not user_dir.is_dir():
            continue
        try:
            user_id = int(user_dir.name)
        except ValueError:
            continue

        if not is_checkin_due(user_id):
            continue

        profile = get_profile(user_id)
        if not profile:
            continue
        lang = lang_of(profile)

        checkin_status = get_checkin_status(user_id)
        checkin_status.reminder_sent_this_month = True
        save_checkin_status(user_id, checkin_status)

        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=(
                    "📅 *It's time for your monthly check-in!*\n\nTap the button below or send /checkin to review your progress."
                    if lang == "en" else
                    "📅 *Время для месячного чек-ина!*\n\nНажмите кнопку или отправьте /checkin для подведения итогов."
                ),
                parse_mode="Markdown",
                reply_markup=home_keyboard(lang),
            )
        except Exception as e:
            logger.warning("Could not send check-in reminder to %s: %s", user_id, e)


# ── Main message handler ──────────────────────────────────────────────────────
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update.effective_user.id):
        return

    user_id = update.effective_user.id
    text = update.message.text or update.message.caption or ""
    profile = get_profile(user_id)
    lang = lang_of(profile)

    # ── Section navigation ────────────────────────────────────────────────────
    home_texts    = {"🔙 Main Menu", "🔙 Главное меню"}
    nutrition_texts = {"🥗 Nutrition", "🥗 Питание"}
    fitness_texts   = {"🏋️ Fitness", "🏋️ Фитнес"}
    plan_texts      = {"📋 My Weekly Plan", "📋 Мой план на неделю"}
    progress_texts  = {"📈 My Progress", "📈 Мой прогресс"}

    if text in home_texts:
        await update.message.reply_text(
            "🏠 Main menu" if lang == "en" else "🏠 Главное меню",
            reply_markup=home_keyboard(lang),
        )
        return

    if text in nutrition_texts:
        await update.message.reply_text(
            "🥗 *Nutrition* — log meals, track your daily targets"
            if lang == "en" else
            "🥗 *Питание* — записывайте еду и следите за дневными целями",
            parse_mode="Markdown", reply_markup=main_keyboard(lang),
        )
        return

    if text in fitness_texts:
        fp = get_fitness_profile(user_id)
        if not fp:
            await update.message.reply_text(
                "No fitness profile found. Send /start to complete setup."
                if lang == "en" else
                "Фитнес-профиль не найден. Отправьте /start для настройки."
            )
            return
        await update.message.reply_text(
            "🏋️ *Fitness* — plans, workouts, progress"
            if lang == "en" else
            "🏋️ *Фитнес* — планы, тренировки, прогресс",
            parse_mode="Markdown", reply_markup=fitness_keyboard(lang),
        )
        return

    if text in plan_texts:
        await handle_weekly_plan(update, context)
        return

    if text in progress_texts:
        await handle_progress(update, context)
        return

    # ── Nutrition keyboard buttons ────────────────────────────────────────────
    summary_texts  = {"📊 Daily Summary", "📊 Сводка за день"}
    today_texts    = {"📋 Today's Meals", "📋 Приёмы пищи"}
    targets_texts  = {"🎯 Targets", "🎯 Цели"}
    training_texts = {"🏋️ Training Day", "🏋️ День тренировки"}
    rest_texts     = {"😴 Rest Day", "😴 День отдыха"}
    reset_texts    = {"🔄 Reset Today", "🔄 Сбросить день"}
    undo_texts     = {"↩️ Undo Last Entry", "↩️ Отменить запись"}
    all_buttons = summary_texts | today_texts | targets_texts | training_texts | rest_texts | reset_texts | undo_texts

    if text in all_buttons:
        # Tapping any menu button cancels a pending fix / target edit
        context.user_data.pop("fixing_meal", None)
        context.user_data.pop("setting_targets", None)
        if not profile or not profile.is_complete():
            await update.message.reply_text(t("setup_first", lang))
            return

    if text in summary_texts:
        await show_summary(update, profile)
        return

    if text in today_texts:
        await show_today(update, profile)
        return

    if text in targets_texts:
        await show_targets(update, profile)
        return

    if text in training_texts or text in rest_texts:
        day_type = DAY_TYPE_TRAINING if text in training_texts else DAY_TYPE_REST
        set_day_type(user_id, day_type)
        targets = get_targets(day_type, profile)
        log = get_today_log(user_id)
        await update.message.reply_text(
            f"{t('training_set' if day_type == DAY_TYPE_TRAINING else 'rest_set', lang)}\n"
            f"{t('calories', lang)}: {log.total_kcal:.0f} / {targets['kcal_min']}–{targets['kcal_max']} kcal | "
            f"{t('protein', lang)}: {log.total_protein:.0f} / {targets['protein_max']}g",
            parse_mode="Markdown", reply_markup=main_keyboard(lang),
        )
        return

    if text in reset_texts:
        reset_log(user_id)
        await update.message.reply_text(t("log_reset", lang), reply_markup=main_keyboard(lang))
        return

    if text in undo_texts:
        removed = undo_last_meal(user_id)
        if not removed:
            await update.message.reply_text(t("undo_empty", lang), reply_markup=main_keyboard(lang))
        else:
            log = get_today_log(user_id)
            label = removed.get("name") or removed.get("label") or f"Meal {removed.get('meal_num', '?')}"
            msg = t("undo_success", lang).format(
                label=label.replace("*", ""), kcal=removed.get("kcal", 0), protein=removed.get("protein", 0),
                carbs=removed.get("carbs", 0), fat=removed.get("fat", 0),
                total_kcal=log.total_kcal, total_protein=log.total_protein,
            )
            await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=main_keyboard(lang))
        return

    # ── Everything else is a meal (photo, image file, or text) ────────────────
    if not profile or not profile.is_complete():
        await update.message.reply_text(t("setup_first", lang))
        return

    # Waiting for new custom targets?
    if update.message.text and await save_custom_targets(update, context, profile, update.message.text):
        return

    # Correction: after tapping ✏️ Fix, or replying to a logged-meal message
    fixing = context.user_data.pop("fixing_meal", None)
    if not fixing and update.message.reply_to_message and update.message.text:
        found = find_meal_by_message(user_id, update.message.reply_to_message.message_id)
        if found:
            fixing = {"date": found[0].date, "id": found[1]["id"]}
    if fixing and update.message.text:
        await apply_correction(update, context, profile, fixing["date"], fixing["id"], update.message.text)
        return

    is_blocked, trial = record_and_check_trial(user_id)
    if is_blocked:
        await update.message.reply_text(t("paywall", lang).format(user_id=user_id), parse_mode="Markdown")
        return
    if trial.days_remaining() in (1, 2) and not trial.paid:
        await update.message.reply_text(t("trial_warning", lang).format(days=trial.days_remaining()), parse_mode="Markdown")

    image_file_id, media_type = None, "image/jpeg"
    if update.message.photo:
        image_file_id = update.message.photo[-1].file_id
    elif update.message.document and (update.message.document.mime_type or "").startswith("image/"):
        image_file_id = update.message.document.file_id
        media_type = update.message.document.mime_type
        if media_type not in ("image/jpeg", "image/png", "image/gif", "image/webp"):
            media_type = "image/jpeg"
    elif not text.strip():
        await update.message.reply_text(t("send_meal", lang))
        return

    await log_meal(update, context, profile, image_file_id=image_file_id,
                   media_type=media_type, description=text.strip())


# ── Entry point ───────────────────────────────────────────────────────────────
def main() -> None:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    app = Application.builder().token(token).build()
    app.bot_data["data_dir"] = os.getenv("DATA_DIR", "data")

    # Nutrition onboarding (now includes fitness questions)
    onboarding = ConversationHandler(
        entry_points=[CommandHandler("start", cmd_start)],
        states={
            ASK_LANG:        [MessageHandler(filters.TEXT & ~filters.COMMAND, onboard_lang)],
            ASK_SEX:         [MessageHandler(filters.TEXT & ~filters.COMMAND, onboard_sex)],
            ASK_AGE:         [MessageHandler(filters.TEXT & ~filters.COMMAND, onboard_age)],
            ASK_WEIGHT:      [MessageHandler(filters.TEXT & ~filters.COMMAND, onboard_weight)],
            ASK_HEIGHT:      [MessageHandler(filters.TEXT & ~filters.COMMAND, onboard_height)],
            ASK_GOAL:        [MessageHandler(filters.TEXT & ~filters.COMMAND, onboard_goal)],
            FIT_ENV:         [MessageHandler(filters.TEXT & ~filters.COMMAND, fit_onboard_env)],
            FIT_LEVEL:       [MessageHandler(filters.TEXT & ~filters.COMMAND, fit_onboard_level)],
            FIT_GOAL:        [MessageHandler(filters.TEXT & ~filters.COMMAND, fit_onboard_goal)],
            FIT_LIMITATIONS: [MessageHandler(filters.TEXT & ~filters.COMMAND, fit_onboard_limitations)],
        },
        fallbacks=[CommandHandler("cancel", onboard_cancel)],
    )

    edit_profile = ConversationHandler(
        entry_points=[
            CommandHandler("profile", edit_start),
            MessageHandler(filters.Regex(r"^(👤 My Profile|👤 Мой профиль)$"), edit_start),
        ],
        states={
            EDIT_CHOOSE: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_choose)],
            EDIT_SEX:    [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_sex)],
            EDIT_AGE:    [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_age)],
            EDIT_WEIGHT: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_weight)],
            EDIT_HEIGHT: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_height)],
            EDIT_GOAL:   [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_goal)],
        },
        fallbacks=[CommandHandler("cancel", onboard_cancel)],
    )

    log_workout_conv = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex(r"^(✅ Log Workout|✅ Записать тренировку)$"), handle_log_workout_start),
        ],
        states={
            LOG_WORKOUT_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_log_workout_text)],
        },
        fallbacks=[CommandHandler("cancel", onboard_cancel)],
    )

    log_activity_conv = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex(r"^(🏃 Log Activity|🏃 Записать активность)$"), handle_log_activity_start),
        ],
        states={
            LOG_ACTIVITY_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_log_activity_text)],
        },
        fallbacks=[CommandHandler("cancel", onboard_cancel)],
    )

    checkin_conv = ConversationHandler(
        entry_points=[
            CommandHandler("checkin", checkin_start),
            MessageHandler(filters.Regex(r"^(📅 Monthly Check-In|📅 Месячный чек-ин)$"), checkin_start),
        ],
        states={
            CHECKIN_WEIGHT: [MessageHandler(filters.TEXT & ~filters.COMMAND, checkin_weight)],
            CHECKIN_NOTES:  [MessageHandler(filters.TEXT & ~filters.COMMAND, checkin_notes)],
        },
        fallbacks=[CommandHandler("cancel", onboard_cancel)],
    )

    app.add_handler(onboarding)
    app.add_handler(edit_profile)
    app.add_handler(log_workout_conv)
    app.add_handler(log_activity_conv)
    app.add_handler(checkin_conv)
    app.add_handler(CommandHandler("whitelist", cmd_whitelist))
    app.add_handler(CommandHandler("checkin", checkin_start))
    app.add_handler(CallbackQueryHandler(on_meal_button, pattern=r"^meal\|"))
    app.add_handler(CallbackQueryHandler(on_targets_button, pattern=r"^tgt\|"))
    app.add_handler(
        MessageHandler(
            filters.PHOTO | filters.Document.IMAGE | (filters.TEXT & ~filters.COMMAND),
            handle_message,
        )
    )

    # Daily job to check if monthly check-in reminders should be sent
    try:
        if app.job_queue:
            app.job_queue.run_daily(
                scheduled_checkin_reminder,
                time=datetime.strptime("09:00", "%H:%M").time().replace(tzinfo=USER_TIMEZONE),
            )
            logger.info("Monthly check-in scheduler started.")
    except Exception as e:
        logger.warning("Job queue not available, monthly reminders disabled: %s", e)

    logger.info("LeanRitual starting…")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
