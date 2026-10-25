"""
meal_ai.py — Accurate, itemised meal estimation + Telegram-safe rendering.

Why this exists (v1 bug): the old prompt asked Claude to fill a template with "X"
placeholders in one free-text pass, with running totals in the same prompt. With no
ingredient breakdown, estimates anchored on "typical meal" numbers and came back the same.

Now:
  1. Claude must call the `record_meal` tool → structured JSON, one row per component
     (including cooking oil, butter, sauces, drinks), with grams and per-item macros.
  2. Totals are summed in Python — Claude never does the arithmetic or sees today's totals.
  3. Each item is sanity-checked (kcal ≈ 4P + 4C + 9F).
  4. Output is rendered as escaped Telegram HTML, so messages never fail to send.
"""

import html
import logging
import re

logger = logging.getLogger(__name__)

# ── Tool schema ───────────────────────────────────────────────────────────────

MEAL_TOOL = {
    "name": "record_meal",
    "description": "Record an itemised nutrition estimate for one meal.",
    "input_schema": {
        "type": "object",
        "properties": {
            "is_food": {
                "type": "boolean",
                "description": "False if the photo/text is not food or is too unclear to estimate.",
            },
            "clarification_question": {
                "type": "string",
                "description": "Only when is_food is false or a critical detail is missing: one short question.",
            },
            "meal_name": {"type": "string", "description": "Short, specific name, e.g. 'Savoury oats with 2 boiled eggs'."},
            "items": {
                "type": "array",
                "description": "Every component, one row each. Include cooking fats, sauces, dressings, toppings and drinks.",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "amount": {"type": "string", "description": "Human portion, e.g. '2 large', '1 tsp', '¾ cup cooked'."},
                        "grams": {"type": "number", "description": "Estimated edible weight in grams (as eaten)."},
                        "kcal": {"type": "number"},
                        "protein_g": {"type": "number"},
                        "carbs_g": {"type": "number"},
                        "fat_g": {"type": "number"},
                        "fibre_g": {"type": "number"},
                    },
                    "required": ["name", "amount", "grams", "kcal", "protein_g", "carbs_g", "fat_g"],
                },
            },
            "assumptions": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Up to 3 assumptions that most affect the number (portion, oil, sauce). Short.",
            },
            "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
            "tip": {
                "type": "string",
                "description": "One specific, practical nutrition tip tailored to THIS meal (1–2 sentences).",
            },
        },
        "required": ["is_food", "meal_name", "items", "confidence", "tip"],
    },
}

SYSTEM_PROMPT = """You are a meticulous nutrition analyst. Your job is an ACCURATE, itemised estimate of one meal.
You also write one short coaching tip in the voice of a sharp, warm nutritionist (Rhonda Patrick depth, dietitian pragmatism).

METHOD — do this for every meal:
1. List every component separately: proteins, starches, vegetables, cheese, toppings, sauces, dressings, drinks.
2. ALWAYS account for hidden fat. If anything is fried, pan-fried, sautéed, roasted or buttered, add the cooking fat as its own row:
   - one pan-fried egg / small item: ~5 g oil (1 tsp, ~40 kcal) unless told otherwise
   - a pan-cooked dish or stir-fry: ~10–15 g oil (1 tbsp, 90–130 kcal) per serving
   - roasted vegetables: ~5–10 g oil per serving
   - "dry pan", "spray" or "air-fried": 0–2 g
   - restaurant food: assume more fat than home cooking
3. Estimate the grams of each component as eaten. In photos, use visual references: a dinner plate is ~26–27 cm, a fork ~19 cm, a large egg ~50 g, a palm-sized piece of meat ~100–120 g, a tightly packed cup ~150–200 g of cooked grains.
4. Compute each row from standard reference values (USDA / Canadian Nutrient File) per gram, using the correct state (cooked vs dry, drained vs in oil). Each row's kcal must roughly equal 4×protein + 4×carbs + 9×fat (+7 per g alcohol).
5. Quantities, brands, weights or labels the user states are authoritative and override what you see. Use the photo for what is present, the text for how much.
6. Estimate each meal independently from first principles. Do not round to familiar totals like 400 or 500 kcal. Do not reuse numbers from other meals.
7. If it is not food or is too unclear, set is_food=false and ask one short clarification_question. If something critical is ambiguous (e.g. whole vs skim, sauce or not), make the most likely assumption and state it in assumptions — do not refuse.

TIP: one specific, genuinely useful idea for THIS meal — a bioavailability pairing, a micronutrient or fibre gap, a fermented-food add-on, a protein boost, or a simple swap that fits the user's goal. No generic advice ("eat more vegetables"), no lecturing.

Always respond by calling the record_meal tool exactly once."""


def _lang_line(lang: str) -> str:
    return ("Write meal_name, item names, amounts, assumptions, tip and any question in Russian."
            if lang == "ru" else
            "Write meal_name, item names, amounts, assumptions, tip and any question in English.")


def _context_line(profile, goal_label: str, protein_left: float | None) -> str:
    if not profile:
        return ""
    s = f"User: {profile.age}y {profile.sex}, {profile.weight_kg} kg, goal: {goal_label}."
    if protein_left is not None:
        s += f" Protein still needed today: ~{max(protein_left, 0):.0f} g (use only for the tip, never for the estimate)."
    return s


# ── Core estimation ───────────────────────────────────────────────────────────

def _num(v) -> float:
    try:
        return max(float(v or 0), 0.0)
    except (TypeError, ValueError):
        return 0.0


def finalise_estimate(raw: dict) -> dict:
    """Clean items, sanity-check energy vs macros, and sum totals in code."""
    items = []
    for it in raw.get("items") or []:
        p, c, f = _num(it.get("protein_g")), _num(it.get("carbs_g")), _num(it.get("fat_g"))
        kcal = _num(it.get("kcal"))
        macro_kcal = 4 * p + 4 * c + 9 * f
        # If the stated kcal disagrees badly with the macros, trust the macros.
        if macro_kcal > 20 and (kcal == 0 or abs(kcal - macro_kcal) / macro_kcal > 0.25):
            logger.info("Item %r kcal %s vs macros %s — using macros", it.get("name"), kcal, macro_kcal)
            kcal = macro_kcal
        items.append({
            "name": str(it.get("name", "")).strip()[:80],
            "amount": str(it.get("amount", "")).strip()[:40],
            "grams": round(_num(it.get("grams"))),
            "kcal": round(kcal),
            "protein_g": round(p, 1),
            "carbs_g": round(c, 1),
            "fat_g": round(f, 1),
            "fibre_g": round(_num(it.get("fibre_g")), 1),
        })
    return {
        "is_food": bool(raw.get("is_food", True)) and bool(items),
        "clarification_question": (raw.get("clarification_question") or "").strip(),
        "name": (raw.get("meal_name") or "").strip()[:80] or "Meal",
        "items": items,
        "kcal": round(sum(i["kcal"] for i in items)),
        "protein": round(sum(i["protein_g"] for i in items), 1),
        "carbs": round(sum(i["carbs_g"] for i in items), 1),
        "fat": round(sum(i["fat_g"] for i in items), 1),
        "fibre": round(sum(i["fibre_g"] for i in items), 1),
        "assumptions": [a.strip() for a in (raw.get("assumptions") or []) if a and a.strip()][:3],
        "confidence": raw.get("confidence", "medium"),
        "tip": (raw.get("tip") or "").strip(),
    }


async def _call_tool(client, model: str, content: list) -> dict:
    resp = await client.messages.create(
        model=model,
        max_tokens=2000,
        system=SYSTEM_PROMPT,
        tools=[MEAL_TOOL],
        tool_choice={"type": "tool", "name": "record_meal"},
        messages=[{"role": "user", "content": content}],
    )
    for block in resp.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "record_meal":
            return block.input
    raise RuntimeError("Model did not return a record_meal tool call")


def _image_block(image_b64: str, media_type: str) -> dict:
    return {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": image_b64}}


async def estimate_meal(client, model: str, *, image_b64: str | None, media_type: str = "image/jpeg",
                        description: str = "", profile=None, goal_label: str = "",
                        protein_left: float | None = None, lang: str = "en") -> dict:
    content = []
    if image_b64:
        content.append(_image_block(image_b64, media_type))
    parts = [_lang_line(lang), _context_line(profile, goal_label, protein_left)]
    if description:
        parts.append(f"User's description: {description}")
    elif image_b64:
        parts.append("No description given — estimate from the photo alone and state your portion and cooking-fat assumptions.")
    content.append({"type": "text", "text": "\n".join(p for p in parts if p)})
    return finalise_estimate(await _call_tool(client, model, content))


async def correct_meal(client, model: str, *, previous: dict, correction: str,
                       image_b64: str | None = None, media_type: str = "image/jpeg",
                       profile=None, goal_label: str = "", lang: str = "en") -> dict:
    """Re-estimate an already-logged meal using the user's correction."""
    prev_lines = "\n".join(
        f"- {i['name']} ({i['amount']}, {i['grams']} g): {i['kcal']} kcal, "
        f"P {i['protein_g']} C {i['carbs_g']} F {i['fat_g']}"
        for i in previous.get("items", [])
    ) or f"- {previous.get('name', 'meal')}: {previous.get('kcal', 0)} kcal (no breakdown saved)"
    content = []
    if image_b64:
        content.append(_image_block(image_b64, media_type))
    content.append({"type": "text", "text": "\n".join(p for p in [
        _lang_line(lang),
        _context_line(profile, goal_label, None),
        f"Original description: {previous.get('description') or '(photo only)'}",
        f"Previous estimate:\n{prev_lines}",
        f"USER CORRECTION (authoritative): {correction}",
        "Return the full corrected meal: apply the correction, keep items it does not affect, "
        "add or remove items as needed, and recompute every affected row.",
    ] if p)})
    return finalise_estimate(await _call_tool(client, model, content))


# ── Rendering (Telegram HTML) ─────────────────────────────────────────────────

def e(s) -> str:
    return html.escape(str(s), quote=False)


def _fmt(n: float) -> str:
    return f"{n:,.0f}"


L = {
    "en": {"total": "Total", "kcal": "kcal", "g": "g", "assumed": "Assumed", "today": "Today",
           "training": "🏋️ training day", "rest": "😴 rest day", "left": "left", "over": "over",
           "protein": "protein", "conf_low": "⚠️ Rough estimate — tap ✏️ Fix to add portions or oil.",
           "fix_hint": "Tip: tap ✏️ Fix (or reply to this message) to add details like oil, sauce or portion size — it updates this entry instead of adding a new one.",
           "updated": "✏️ Updated"},
    "ru": {"total": "Итого", "kcal": "ккал", "g": "г", "assumed": "Допущения", "today": "Сегодня",
           "training": "🏋️ тренировочный день", "rest": "😴 день отдыха", "left": "осталось", "over": "сверх нормы",
           "protein": "белок", "conf_low": "⚠️ Приблизительная оценка — нажмите ✏️ Исправить, чтобы уточнить порцию или масло.",
           "fix_hint": "Совет: нажмите ✏️ Исправить (или ответьте на это сообщение), чтобы добавить масло, соус или размер порции — запись обновится, а не продублируется.",
           "updated": "✏️ Обновлено"},
}


def render_meal(meal: dict, log, targets: dict, day_type: str, lang: str = "en",
                updated: bool = False, show_fix_hint: bool = False) -> str:
    T = L.get(lang, L["en"])
    lines = []
    if updated:
        lines.append(f"<i>{T['updated']}</i>")
    lines.append(f"🍽 <b>{e(meal['name'])}</b>")
    for it in meal.get("items", []):
        already_grams = re.search(r"\d\s*(g|г|гр)\b", it.get("amount", ""))
        grams = f", {it['grams']} {T['g']}" if it.get("grams") and not already_grams else ""
        lines.append(f"• {e(it['name'])} ({e(it['amount'])}{grams}) — {_fmt(it['kcal'])} {T['kcal']}")
    lines.append("")
    lines.append(
        f"<b>{T['total']}: {_fmt(meal['kcal'])} {T['kcal']}</b> · "
        f"P {meal['protein']:.0f} · C {meal['carbs']:.0f} · F {meal['fat']:.0f}"
        + (f" · Fib {meal['fibre']:.0f}" if meal.get("fibre") else "")
    )
    if meal.get("assumptions"):
        lines.append(f"<i>{T['assumed']}: {e('; '.join(meal['assumptions']))}</i>")
    if meal.get("confidence") == "low":
        lines.append(T["conf_low"])

    kcal_left = targets["kcal_max"] - log.total_kcal
    prot_left = targets["protein_max"] - log.total_protein
    dt = T["training"] if day_type == "training" else T["rest"]
    lines.append("")
    lines.append(
        f"📈 <b>{T['today']}</b> ({dt}): {_fmt(log.total_kcal)} / {_fmt(targets['kcal_max'])} {T['kcal']} · "
        f"{T['protein']} {log.total_protein:.0f} / {targets['protein_max']} {T['g']}"
    )
    kcal_part = (f"{_fmt(kcal_left)} {T['kcal']} {T['left']}" if kcal_left >= 0
                 else f"{_fmt(-kcal_left)} {T['kcal']} {T['over']}")
    prot_part = f"{max(prot_left, 0):.0f} {T['g']} {T['protein']} {T['left']}"
    lines.append(f"→ {kcal_part} · {prot_part}")

    if meal.get("tip"):
        lines.append("")
        lines.append(f"💡 {e(meal['tip'])}")
    if show_fix_hint:
        lines.append("")
        lines.append(f"<i>{T['fix_hint']}</i>")
    return "\n".join(lines)


# ── Markdown → Telegram HTML (for free-text AI replies) ──────────────────────

def md_to_tg_html(text: str) -> str:
    """
    Convert Claude-style Markdown into the small HTML subset Telegram accepts.
    Tables become readable lines; headings become bold. Everything else is escaped,
    so a stray *, _ or < can never make Telegram reject the message.
    """
    out = []
    for raw in (text or "").splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        # Table separator rows like |---|---|
        if re.fullmatch(r"\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?", stripped):
            continue
        if stripped.startswith("|") and stripped.endswith("|"):
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            line = " · ".join(c for c in cells if c)
        if stripped in ("---", "***", "___"):
            out.append("")
            continue
        heading = re.match(r"^\s*#{1,6}\s+(.*)$", line)
        line = e(heading.group(1) if heading else line)
        line = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", line)
        line = re.sub(r"__(.+?)__", r"<b>\1</b>", line)
        line = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"<i>\1</i>", line)
        line = re.sub(r"(?<![\w_])_(?!\s)(.+?)(?<!\s)_(?![\w_])", r"<i>\1</i>", line)
        line = re.sub(r"`([^`]+)`", r"<code>\1</code>", line)
        line = re.sub(r"^(\s*)[-*]\s+", r"\1• ", line)
        if heading:
            line = f"<b>{line}</b>"
        out.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


def strip_tags(s: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", s))


def split_message(text: str, limit: int = 3800) -> list[str]:
    """Split on line boundaries to stay under Telegram's 4096-char limit."""
    chunks, current = [], ""
    for line in text.split("\n"):
        if len(current) + len(line) + 1 > limit and current:
            chunks.append(current.rstrip())
            current = ""
        current += line + "\n"
    if current.strip():
        chunks.append(current.rstrip())
    return chunks or [""]
