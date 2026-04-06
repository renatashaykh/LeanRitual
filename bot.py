"""
NutriBot — Telegram nutrition tracking bot powered by Claude Vision.
- Language chosen first during onboarding, all UI follows that choice
- Profile button shows current info with edit options
- Supports English and Russian
"""

import os
import logging
import base64
import json
import re
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
    GOAL_FAT_LOSS, GOAL_MUSCLE_GAIN, GOAL_MAINTAIN, GOAL_LABELS,
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

# ── Conversation states ───────────────────────────────────────────────────────
# Onboarding
ASK_LANG, ASK_SEX, ASK_AGE, ASK_WEIGHT, ASK_GOAL = range(5)
# Edit profile
EDIT_CHOOSE, EDIT_SEX, EDIT_AGE, EDIT_WEIGHT, EDIT_GOAL = range(5, 10)


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
        "updated":          {"en": "✅ Updated!", "ru": "✅ Обновлено!"},
        "profile_deleted":  {"en": "Profile deleted. Send /start to set up a new one.",
                             "ru": "Профиль удалён. Отправьте /start для настройки нового."},
    }
    return strings.get(key, {}).get(lang, strings.get(key, {}).get("en", key))


def lang_of(profile: UserProfile | None) -> str:
    return profile.language if profile else "en"


# ── Keyboards (language-aware) ────────────────────────────────────────────────
def lang_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[KeyboardButton("🇬🇧 English"), KeyboardButton("🇷🇺 Русский")]],
        resize_keyboard=True, one_time_keyboard=True,
    )


def main_keyboard(lang: str = "en") -> ReplyKeyboardMarkup:
    if lang == "ru":
        return ReplyKeyboardMarkup([
            [KeyboardButton("📊 Сводка за день"), KeyboardButton("🏋️ День тренировки")],
            [KeyboardButton("😴 День отдыха"),    KeyboardButton("🔄 Сбросить день")],
            [KeyboardButton("👤 Мой профиль")],
        ], resize_keyboard=True)
    return ReplyKeyboardMarkup([
        [KeyboardButton("📊 Daily Summary"), KeyboardButton("🏋️ Training Day")],
        [KeyboardButton("😴 Rest Day"),      KeyboardButton("🔄 Reset Today")],
        [KeyboardButton("👤 My Profile")],
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
        [KeyboardButton(t("edit_cancel", lang))],
    ], resize_keyboard=True, one_time_keyboard=True)


# ── Onboarding ────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_allowed(update.effective_user.id):
        return ConversationHandler.END

    profile = get_profile(update.effective_user.id)
    if profile and profile.is_complete():
        lang = lang_of(profile)
        await update.message.reply_text(
            t("welcome_back", lang), reply_markup=main_keyboard(lang),
        )
        return ConversationHandler.END

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
        goal=goal,
        language=lang,
    )
    save_profile(update.effective_user.id, profile)

    targets_t = get_targets(DAY_TYPE_TRAINING, profile)
    targets_r = get_targets(DAY_TYPE_REST, profile)

    await update.message.reply_text(
        f"{t('profile_saved', lang)}\n\n"
        f"{t('training_label', lang)}: {targets_t['kcal_min']}–{targets_t['kcal_max']} kcal | {targets_t['protein_max']}g {t('protein', lang).lower()}\n"
        f"{t('rest_label', lang)}: {targets_r['kcal_min']}–{targets_r['kcal_max']} kcal | {targets_r['protein_max']}g {t('protein', lang).lower()}\n\n"
        f"{t('ready', lang)}",
        parse_mode="Markdown",
        reply_markup=main_keyboard(lang),
    )
    return ConversationHandler.END


async def onboard_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    lang = context.user_data.get("ob_lang", "en")
    await update.message.reply_text(t("cancelled", lang))
    return ConversationHandler.END


# ── Edit profile conversation ─────────────────────────────────────────────────
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


# ── Claude API calls ──────────────────────────────────────────────────────────
def analyse_meal(image_b64, text_description, day_log, day_type, profile):
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
        model=ANTHROPIC_MODEL, max_tokens=1024,
        messages=[{"role": "user", "content": content}],
    )
    return response.content[0].text


def get_daily_summary(day_log, day_type, profile):
    targets = get_targets(day_type, profile)
    prompt = build_summary_prompt(day_log, targets, profile)
    response = anthropic_client.messages.create(
        model=ANTHROPIC_MODEL, max_tokens=600,
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


# ── Main message handler ──────────────────────────────────────────────────────
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update.effective_user.id):
        return

    user_id = update.effective_user.id
    text = update.message.text or update.message.caption or ""
    profile = get_profile(user_id)
    lang = lang_of(profile)

    # All keyboard button texts in both languages
    summary_texts   = {"📊 Daily Summary", "📊 Сводка за день"}
    training_texts  = {"🏋️ Training Day", "🏋️ День тренировки"}
    rest_texts      = {"😴 Rest Day", "😴 День отдыха"}
    reset_texts     = {"🔄 Reset Today", "🔄 Сбросить день"}

    if text in summary_texts:
        if not profile or not profile.is_complete():
            await update.message.reply_text(t("setup_first", lang))
            return
        day_log = get_today_log(user_id)
        day_type = context.user_data.get("day_type", DAY_TYPE_TRAINING)
        if day_log.meal_count == 0:
            await update.message.reply_text(t("no_meals", lang))
            return
        await update.message.reply_text(t("generating", lang))
        summary = get_daily_summary(day_log, day_type, profile)
        await update.message.reply_text(summary, parse_mode="Markdown")
        return

    if text in training_texts:
        context.user_data["day_type"] = DAY_TYPE_TRAINING
        targets = get_targets(DAY_TYPE_TRAINING, profile)
        await update.message.reply_text(
            f"{t('training_set', lang)}\n"
            f"{t('calories', lang)}: {targets['kcal_min']}–{targets['kcal_max']} kcal | "
            f"{t('protein', lang)}: {targets['protein_max']}g",
            parse_mode="Markdown", reply_markup=main_keyboard(lang),
        )
        return

    if text in rest_texts:
        context.user_data["day_type"] = DAY_TYPE_REST
        targets = get_targets(DAY_TYPE_REST, profile)
        await update.message.reply_text(
            f"{t('rest_set', lang)}\n"
            f"{t('calories', lang)}: {targets['kcal_min']}–{targets['kcal_max']} kcal | "
            f"{t('protein', lang)}: {targets['protein_max']}g",
            parse_mode="Markdown", reply_markup=main_keyboard(lang),
        )
        return

    if text in reset_texts:
        reset_log(user_id)
        await update.message.reply_text(t("log_reset", lang), reply_markup=main_keyboard(lang))
        return

    # Meal logging
    if not profile or not profile.is_complete():
        await update.message.reply_text(t("setup_first", lang))
        return

    image_b64 = None
    if update.message.photo:
        photo = update.message.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        file_bytes = await file.download_as_bytearray()
        image_b64 = base64.b64encode(file_bytes).decode("utf-8")
    elif not text:
        await update.message.reply_text(t("send_meal", lang))
        return

    day_type = context.user_data.get("day_type", DAY_TYPE_TRAINING)
    day_log = get_today_log(user_id)
    status_msg = await update.message.reply_text(t("analysing", lang))

    try:
        response_text = analyse_meal(image_b64, text, day_log, day_type, profile)
    except Exception as e:
        logger.error("Claude API error: %s", e)
        await status_msg.edit_text(t("analyse_error", lang))
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

    clean = re.sub(r"<macros>.*?</macros>", "", response_text, flags=re.DOTALL).strip()
    await status_msg.edit_text(clean, parse_mode="Markdown")


# ── Entry point ───────────────────────────────────────────────────────────────
def main() -> None:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    app = Application.builder().token(token).build()

    onboarding = ConversationHandler(
        entry_points=[CommandHandler("start", cmd_start)],
        states={
            ASK_LANG:   [MessageHandler(filters.TEXT & ~filters.COMMAND, onboard_lang)],
            ASK_SEX:    [MessageHandler(filters.TEXT & ~filters.COMMAND, onboard_sex)],
            ASK_AGE:    [MessageHandler(filters.TEXT & ~filters.COMMAND, onboard_age)],
            ASK_WEIGHT: [MessageHandler(filters.TEXT & ~filters.COMMAND, onboard_weight)],
            ASK_GOAL:   [MessageHandler(filters.TEXT & ~filters.COMMAND, onboard_goal)],
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
            EDIT_GOAL:   [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_goal)],
        },
        fallbacks=[CommandHandler("cancel", onboard_cancel)],
    )

    app.add_handler(onboarding)
    app.add_handler(edit_profile)
    app.add_handler(
        MessageHandler(filters.PHOTO | filters.TEXT & ~filters.COMMAND, handle_message)
    )

    logger.info("NutriBot starting…")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
