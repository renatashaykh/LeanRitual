"""
prompts.py — Prompt for the end-of-day nutrition coaching note.

Meal estimation lives in meal_ai.py (structured, itemised). The daily totals table is
built in code (bot.py) — Claude only writes the coaching part, so numbers are always exact.
"""

from storage import DailyLog, UserProfile
from targets import GOAL_LABELS


def _profile_description(profile: UserProfile) -> str:
    goal_label = GOAL_LABELS.get(profile.goal, profile.goal)
    return f"{profile.age}y {profile.sex}, {profile.weight_kg} kg, goal: {goal_label}"


def build_summary_prompt(
    log: DailyLog,
    targets: dict,
    profile: UserProfile | None = None,
    day_type: str = "training",
    workout_note: str = "",
) -> str:
    meal_lines = []
    for m in log.meals:
        items = ", ".join(f"{i['name']} ({i.get('amount', '')})" for i in m.get("items", [])) or m.get("name", "")
        meal_lines.append(
            f"- {m.get('time', '')} {m.get('name') or 'Meal'}: {m.get('kcal', 0):.0f} kcal, "
            f"P {m.get('protein', 0):.0f} C {m.get('carbs', 0):.0f} F {m.get('fat', 0):.0f} — {items}"
        )
    profile_section = _profile_description(profile) if profile else "Unknown user"
    lang = profile.language if profile else "en"
    lang_instruction = "Write entirely in Russian." if lang == "ru" else "Write entirely in English."

    return f"""You are a nutrition coach with the scientific depth of Rhonda Patrick and the practical, goal-oriented mindset of a registered dietitian. Warm, specific, never preachy.

{lang_instruction}

USER: {profile_section}
DAY TYPE: {day_type}{(" — " + workout_note) if workout_note else ""}

TODAY'S MEALS
{chr(10).join(meal_lines)}

TOTALS: {log.total_kcal:.0f} kcal (target {targets['kcal_min']}–{targets['kcal_max']}), protein {log.total_protein:.0f} g (target {targets['protein_min']}–{targets['protein_max']}), carbs {log.total_carbs:.0f} g, fat {log.total_fat:.0f} g, fibre {log.total_fibre:.0f} g

The user already sees a numeric table — do NOT repeat the numbers or draw tables.
Write exactly three short sections, each 1–2 sentences, specific to what was actually eaten:

🏆 **Win of the day:** ...
🔬 **Nutritionist note:** (a pattern across the day: a micronutrient or fibre gap, protein distribution across meals, gut health, blood-sugar stability)
🔧 **Tomorrow's focus:** (one concrete, easy action)
"""
