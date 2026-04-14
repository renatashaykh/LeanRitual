"""
fitness_storage.py — Fitness profile, weekly plans, workout logs, progression history.

File layout:
  data/{user_id}/fitness_profile.json        — training setup, goals, limitations
  data/{user_id}/fitness_week_{YYYY-WNN}.json — weekly plan + completion status
  data/{user_id}/fitness_log_{YYYY-MM-DD}.json — individual workout/activity logs
  data/{user_id}/checkin.json                 — monthly check-in tracking
"""

import json
import os
from dataclasses import dataclass, asdict, field
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
USER_TIMEZONE = ZoneInfo(os.getenv("USER_TIMEZONE", "America/Toronto"))


def today_str() -> str:
    return datetime.now(USER_TIMEZONE).date().isoformat()


def current_week_key() -> str:
    """Returns week key like 2024-W03 based on user's timezone."""
    now = datetime.now(USER_TIMEZONE)
    return f"{now.year}-W{now.isocalendar()[1]:02d}"


def _user_dir(user_id: int) -> Path:
    path = DATA_DIR / str(user_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


# ── Fitness profile ───────────────────────────────────────────────────────────

@dataclass
class FitnessProfile:
    training_environment: str = ""   # "gym" / "home" / "both"
    fitness_level: str = ""          # "beginner" / "intermediate" / "advanced"
    fitness_goal: str = ""           # "fat_loss_strength" / "muscle_gain" / "general_fitness"
    limitations: str = ""            # free text, e.g. "bad left knee"
    preferred_days: list = field(default_factory=list)  # ["monday","wednesday","friday"]
    weeks_completed: int = 0         # total weeks of training tracked

    def is_complete(self) -> bool:
        return all([self.training_environment, self.fitness_level, self.fitness_goal])


def get_fitness_profile(user_id: int) -> FitnessProfile | None:
    path = _user_dir(user_id) / "fitness_profile.json"
    if path.exists():
        with open(path) as f:
            data = json.load(f)
        return FitnessProfile(**data)
    return None


def save_fitness_profile(user_id: int, profile: FitnessProfile) -> None:
    path = _user_dir(user_id) / "fitness_profile.json"
    with open(path, "w") as f:
        json.dump(asdict(profile), f, indent=2)


def delete_fitness_profile(user_id: int) -> None:
    path = _user_dir(user_id) / "fitness_profile.json"
    if path.exists():
        path.unlink()


# ── Weekly plan ───────────────────────────────────────────────────────────────

@dataclass
class WeeklyPlan:
    week_key: str = ""               # e.g. "2024-W03"
    generated_on: str = ""
    plan_text: str = ""              # full plan as Claude wrote it
    days: list = field(default_factory=list)  # list of day dicts
    completed_days: list = field(default_factory=list)  # list of day names logged
    notes: str = ""                  # any mid-week adjustments

    def days_completed(self) -> int:
        return len(self.completed_days)


def get_weekly_plan(user_id: int, week_key: str | None = None) -> WeeklyPlan | None:
    if week_key is None:
        week_key = current_week_key()
    path = _user_dir(user_id) / f"fitness_week_{week_key}.json"
    if path.exists():
        with open(path) as f:
            data = json.load(f)
        return WeeklyPlan(**data)
    return None


def save_weekly_plan(user_id: int, plan: WeeklyPlan) -> None:
    path = _user_dir(user_id) / f"fitness_week_{plan.week_key}.json"
    with open(path, "w") as f:
        json.dump(asdict(plan), f, indent=2)


def get_recent_weeks(user_id: int, n: int = 4) -> list[WeeklyPlan]:
    """Return up to n most recent weekly plans for progression analysis."""
    plans = []
    user_dir = _user_dir(user_id)
    files = sorted(user_dir.glob("fitness_week_*.json"), reverse=True)
    for f in files[:n]:
        with open(f) as fp:
            data = json.load(fp)
        plans.append(WeeklyPlan(**data))
    return plans


# ── Workout / activity log ────────────────────────────────────────────────────

@dataclass
class WorkoutLog:
    date: str = ""
    type: str = ""                   # "workout" / "activity"
    workout_day: str = ""            # e.g. "Day 1 — Lower Body"
    activity_type: str = ""          # e.g. "run", "yoga", "walk" (for activities)
    duration_min: int = 0
    exercises: list = field(default_factory=list)  # list of {name, sets, reps, weight_kg}
    notes: str = ""
    perceived_effort: int = 0        # 1-10 RPE
    completed: bool = True


def get_workout_log(user_id: int, date_str: str | None = None) -> WorkoutLog | None:
    if date_str is None:
        date_str = today_str()
    path = _user_dir(user_id) / f"fitness_log_{date_str}.json"
    if path.exists():
        with open(path) as f:
            data = json.load(f)
        return WorkoutLog(**data)
    return None


def save_workout_log(user_id: int, log: WorkoutLog) -> None:
    path = _user_dir(user_id) / f"fitness_log_{log.date}.json"
    with open(path, "w") as f:
        json.dump(asdict(log), f, indent=2)


def get_recent_workout_logs(user_id: int, days: int = 28) -> list[WorkoutLog]:
    """Return workout logs from the past N days for progression context."""
    logs = []
    user_dir = _user_dir(user_id)
    for i in range(days):
        check_date = (datetime.now(USER_TIMEZONE) - timedelta(days=i)).date().isoformat()
        path = user_dir / f"fitness_log_{check_date}.json"
        if path.exists():
            with open(path) as f:
                data = json.load(f)
            logs.append(WorkoutLog(**data))
    return logs


# ── Monthly check-in tracking ─────────────────────────────────────────────────

@dataclass
class CheckInStatus:
    last_checkin_month: str = ""     # "YYYY-MM"
    last_weight_kg: float = 0.0
    checkin_history: list = field(default_factory=list)  # list of check-in dicts
    reminder_sent_this_month: bool = False


def get_checkin_status(user_id: int) -> CheckInStatus:
    path = _user_dir(user_id) / "checkin.json"
    if path.exists():
        with open(path) as f:
            data = json.load(f)
        return CheckInStatus(**data)
    return CheckInStatus()


def save_checkin_status(user_id: int, status: CheckInStatus) -> None:
    path = _user_dir(user_id) / "checkin.json"
    with open(path, "w") as f:
        json.dump(asdict(status), f, indent=2)


def current_month_str() -> str:
    return datetime.now(USER_TIMEZONE).strftime("%Y-%m")


def is_checkin_due(user_id: int) -> bool:
    """True if it's the 1st of the month and check-in hasn't been sent yet."""
    now = datetime.now(USER_TIMEZONE)
    if now.day != 1:
        return False
    status = get_checkin_status(user_id)
    return (
        status.last_checkin_month != current_month_str()
        and not status.reminder_sent_this_month
    )


def record_checkin(user_id: int, weight_kg: float, notes: str, response: str) -> None:
    status = get_checkin_status(user_id)
    status.last_checkin_month = current_month_str()
    status.last_weight_kg = weight_kg
    status.reminder_sent_this_month = True
    status.checkin_history.append({
        "month": current_month_str(),
        "date": today_str(),
        "weight_kg": weight_kg,
        "notes": notes,
        "response": response,
    })
    save_checkin_status(user_id, status)
