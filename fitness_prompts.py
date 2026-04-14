"""
fitness_prompts.py — Claude prompt templates for fitness coaching.

All prompts receive both fitness AND nutrition context for integrated coaching.
"""

from fitness_storage import FitnessProfile, WeeklyPlan, WorkoutLog
from storage import UserProfile, DailyLog


def _fitness_profile_section(fp: FitnessProfile) -> str:
    return (
        f"Training environment: {fp.training_environment}\n"
        f"  Fitness level: {fp.fitness_level}\n"
        f"  Fitness goal: {fp.fitness_goal}\n"
        f"  Limitations: {fp.limitations or 'None'}\n"
        f"  Weeks of tracked training: {fp.weeks_completed}"
    )


def _nutrition_context(profile: UserProfile | None, today_log: DailyLog | None, nutrition_targets: dict | None) -> str:
    if not profile or not today_log or not nutrition_targets:
        return ""
    return (
        f"\nTODAY'S NUTRITION CONTEXT (for integrated coaching):\n"
        f"  Calories so far: {today_log.total_kcal:.0f} / {nutrition_targets.get('kcal_max', '?')} kcal\n"
        f"  Protein so far: {today_log.total_protein:.0f} / {nutrition_targets.get('protein_max', '?')}g\n"
        f"  Meals logged: {today_log.meal_count}"
    )


def _recent_weeks_summary(recent_weeks: list[WeeklyPlan]) -> str:
    if not recent_weeks:
        return "No previous weeks on record — this is week 1."
    lines = []
    for w in recent_weeks:
        lines.append(
            f"  {w.week_key}: {w.days_completed()}/{len(w.days)} days completed"
            + (f" — {w.notes}" if w.notes else "")
        )
    return "RECENT TRAINING HISTORY:\n" + "\n".join(lines)


def _recent_logs_summary(logs: list[WorkoutLog]) -> str:
    if not logs:
        return ""
    lines = ["RECENT WORKOUT LOGS (for progression):"]
    for log in logs[:8]:
        if log.exercises:
            ex_summary = ", ".join(
                f"{e['name']} {e.get('sets','?')}×{e.get('reps','?')}"
                + (f" @{e.get('weight_kg','?')}kg" if e.get('weight_kg') else "")
                for e in log.exercises[:4]
            )
            lines.append(f"  {log.date} — {log.workout_day}: {ex_summary}")
        elif log.type == "activity":
            lines.append(f"  {log.date} — {log.activity_type} ({log.duration_min} min)")
    return "\n".join(lines)


# ── Weekly plan generation ────────────────────────────────────────────────────

def build_weekly_plan_prompt(
    fitness_profile: FitnessProfile,
    user_profile: UserProfile,
    recent_weeks: list[WeeklyPlan],
    recent_logs: list[WorkoutLog],
    today_log: DailyLog | None = None,
    nutrition_targets: dict | None = None,
    lang: str = "en",
) -> str:
    lang_instruction = "Respond entirely in Russian." if lang == "ru" else "Respond entirely in English."
    weeks_history = _recent_weeks_summary(recent_weeks)
    logs_history = _recent_logs_summary(recent_logs)
    nutrition_ctx = _nutrition_context(user_profile, today_log, nutrition_targets)

    # Determine if deload week is needed
    deload_note = ""
    if fitness_profile.weeks_completed > 0 and fitness_profile.weeks_completed % 4 == 3:
        deload_note = "\n⚠️ DELOAD WEEK: This is week 4 of the training block. Reduce volume by ~40%, keep intensity moderate. Focus on recovery."

    return f"""You are an expert personal trainer and strength coach with deep knowledge of periodisation, progressive overload, and integrated nutrition-training planning.

LANGUAGE: {lang_instruction}

USER PROFILE
  Age: {user_profile.age}, Sex: {user_profile.sex}
  Weight: {user_profile.weight_kg} kg
  Nutrition goal: {user_profile.goal}

FITNESS PROFILE
  {_fitness_profile_section(fitness_profile)}
{deload_note}

{weeks_history}

{logs_history}
{nutrition_ctx}

TASK: Generate this week's complete training plan.

RULES:
- Choose the optimal number of training days based on fitness goal and level
- For gym: use barbells, dumbbells, cables, machines as appropriate
- For home: dumbbells and bodyweight only
- Include specific sets, reps, and suggested starting weights based on log history (or beginner estimates if no history)
- Strategically place rest days — never two hard days back to back
- Note which days are higher-carb (training) vs lower-carb (rest) for nutrition synergy
- If previous weeks show missed days, slightly reduce volume this week
- If previous weeks show consistent completion, increase volume or intensity by ~5-10%
- Flag the deload week clearly if applicable

FORMAT your response exactly like this:

📅 **Week of [dates] — [theme e.g. "Strength Block 2" or "Deload Week"]**

[1-2 sentence overview of this week's focus and any progressions from last week]

---

**Day 1 — [muscle group/type] | [suggested day e.g. Monday]**
🍽️ *Nutrition: Training day targets apply*

| Exercise | Sets | Reps | Weight |
|----------|------|------|--------|
| [name] | [sets] | [reps] | [kg or bodyweight] |
...

💡 *Coach note: [one specific tip for this day]*

---

[repeat for each training day]

---

**Rest Days: [list days]**
🍽️ *Nutrition: Rest day targets apply — focus on protein and recovery foods*

---

**This week's focus:** [one sentence on the key progression or theme]
"""


# ── Workout completion logging ────────────────────────────────────────────────

def build_workout_log_prompt(
    workout_text: str,
    fitness_profile: FitnessProfile,
    user_profile: UserProfile,
    current_plan: WeeklyPlan | None,
    today_log: DailyLog | None = None,
    nutrition_targets: dict | None = None,
    lang: str = "en",
) -> str:
    lang_instruction = "Respond entirely in Russian." if lang == "ru" else "Respond entirely in English."
    nutrition_ctx = _nutrition_context(user_profile, today_log, nutrition_targets)
    plan_context = f"\nThis week's plan:\n{current_plan.plan_text[:800]}..." if current_plan else ""

    return f"""You are an expert personal trainer reviewing a completed workout.

LANGUAGE: {lang_instruction}

USER: {user_profile.age}y {user_profile.sex}, {user_profile.weight_kg}kg, goal: {user_profile.goal}
FITNESS LEVEL: {fitness_profile.fitness_level}
{plan_context}
{nutrition_ctx}

WORKOUT LOGGED BY USER:
{workout_text}

TASK:
1. Acknowledge and celebrate the workout completion
2. Extract the exercises, sets, reps, and weights mentioned (if any) — output as a JSON block for storage
3. Give 1-2 sentences of specific feedback on the session
4. Note one recovery or nutrition tip relevant to what they just did, considering today's food intake if available

FORMAT:
✅ **[Workout name/type]**

[1 sentence celebration + what they accomplished]

💪 **Feedback:** [specific coaching note]

🍽️ **Recovery tip:** [nutrition/recovery advice integrated with today's food data if available]

Then output on a new line:
<workout_data>{{"workout_day": "...", "exercises": [{{"name": "...", "sets": N, "reps": N, "weight_kg": N}}], "perceived_effort": N, "duration_min": N}}</workout_data>

If no specific exercises/weights were mentioned, use empty exercises list and estimate duration/effort from description.
"""


# ── Activity logging (outside plan) ──────────────────────────────────────────

def build_activity_log_prompt(
    activity_text: str,
    fitness_profile: FitnessProfile,
    user_profile: UserProfile,
    current_plan: WeeklyPlan | None,
    today_log: DailyLog | None = None,
    nutrition_targets: dict | None = None,
    lang: str = "en",
) -> str:
    lang_instruction = "Respond entirely in Russian." if lang == "ru" else "Respond entirely in English."
    nutrition_ctx = _nutrition_context(user_profile, today_log, nutrition_targets)
    remaining_plan = ""
    if current_plan:
        completed = current_plan.completed_days
        total_days = len(current_plan.days)
        remaining_plan = f"Plan progress: {len(completed)}/{total_days} days done. Completed: {', '.join(completed) if completed else 'none yet'}"

    return f"""You are an expert personal trainer adapting a training plan based on an unplanned activity.

LANGUAGE: {lang_instruction}

USER: {user_profile.age}y {user_profile.sex}, {user_profile.weight_kg}kg
FITNESS LEVEL: {fitness_profile.fitness_level}, GOAL: {fitness_profile.fitness_goal}
{remaining_plan}
{nutrition_ctx}

ACTIVITY LOGGED:
{activity_text}

TASK:
1. Acknowledge the activity positively
2. Assess the impact on the rest of this week's plan (fatigue, recovery needs)
3. Suggest any adjustments to upcoming training days if needed
4. Give a nutrition tip specific to this activity type

FORMAT:
🏃 **[Activity type] logged!**

[1 sentence acknowledgment]

📋 **Plan adjustment:** [what (if anything) to change about remaining days this week — be specific]

🍽️ **Nutrition note:** [specific tip for recovery from this activity, considering today's intake]

Then output:
<activity_data>{{"activity_type": "...", "duration_min": N, "perceived_effort": N}}</activity_data>
"""


# ── Monthly check-in ──────────────────────────────────────────────────────────

def build_monthly_checkin_prompt(
    current_weight_kg: float,
    user_notes: str,
    user_profile: UserProfile,
    fitness_profile: FitnessProfile | None,
    recent_weeks: list[WeeklyPlan],
    checkin_history: list,
    lang: str = "en",
) -> str:
    lang_instruction = "Respond entirely in Russian." if lang == "ru" else "Respond entirely in English."

    history_text = ""
    if checkin_history:
        prev = checkin_history[-1]
        weight_change = current_weight_kg - prev.get("weight_kg", current_weight_kg)
        direction = "↓" if weight_change < 0 else "↑" if weight_change > 0 else "→"
        history_text = f"Previous check-in: {prev['month']} — {prev['weight_kg']}kg\nWeight change: {direction} {abs(weight_change):.1f}kg"

    training_summary = ""
    if recent_weeks:
        total_days = sum(len(w.days) for w in recent_weeks)
        completed_days = sum(w.days_completed() for w in recent_weeks)
        training_summary = f"Training adherence (last 4 weeks): {completed_days}/{total_days} sessions completed ({int(completed_days/max(total_days,1)*100)}%)"

    fitness_section = ""
    if fitness_profile:
        fitness_section = f"Fitness goal: {fitness_profile.fitness_goal}\nLevel: {fitness_profile.fitness_level}\nWeeks tracked: {fitness_profile.weeks_completed}"

    return f"""You are a holistic wellness coach conducting a monthly progress review. You have deep expertise in both nutrition and fitness, and you look at the whole picture.

LANGUAGE: {lang_instruction}

USER PROFILE
  Age: {user_profile.age}, Sex: {user_profile.sex}
  Starting/reference weight: {user_profile.weight_kg}kg
  Current weight: {current_weight_kg}kg
  Nutrition goal: {user_profile.goal}
  {fitness_section}

{history_text}
{training_summary}

USER'S NOTES THIS MONTH:
"{user_notes}"

TASK: Generate a warm, honest, and actionable monthly review covering both nutrition and fitness.

FORMAT:

📊 **Monthly Check-In — [Month Year]**

**Weight:** [current]kg [change from last month if available]

---

🥗 **Nutrition Review**
[2-3 sentences on nutrition patterns, what's working, what needs attention]

🏋️ **Training Review**
[2-3 sentences on training consistency, strength progression, what to be proud of]

🔗 **Integrated Insight**
[1-2 sentences on how nutrition and training are working together — or not — and the most important lever to pull next month]

---

🎯 **Next Month's Focus**
• Nutrition: [one specific, measurable target]
• Training: [one specific progression goal]
• Habit: [one small daily habit to add or reinforce]

[Closing sentence — warm and motivating, acknowledging the effort regardless of outcome]
"""


# ── Progress summary ──────────────────────────────────────────────────────────

def build_progress_prompt(
    fitness_profile: FitnessProfile,
    user_profile: UserProfile,
    recent_weeks: list[WeeklyPlan],
    recent_logs: list[WorkoutLog],
    lang: str = "en",
) -> str:
    lang_instruction = "Respond entirely in Russian." if lang == "ru" else "Respond entirely in English."
    weeks_history = _recent_weeks_summary(recent_weeks)
    logs_history = _recent_logs_summary(recent_logs)

    return f"""You are a personal trainer giving a progress update.

LANGUAGE: {lang_instruction}

USER: {user_profile.age}y {user_profile.sex}, {user_profile.weight_kg}kg
{_fitness_profile_section(fitness_profile)}

{weeks_history}

{logs_history}

Give a concise progress summary:

📈 **Your Progress**

**Consistency:** [X/Y sessions completed over Z weeks — honest assessment]

**Strength trends:** [any weights that have increased, or exercises where form/reps improved]

**Biggest win:** [one specific thing to celebrate]

**Next focus:** [one thing to prioritise this week for continued progress]
"""
