"""
storage.py — Daily log + user profile + trial tracking.

File layout:
  data/{user_id}/profile.json      — onboarding answers, persists forever
  data/{user_id}/trial.json        — first-seen date, paid status, active days
  data/{user_id}/{YYYY-MM-DD}.json — daily meal log (date in user's timezone)
"""

import json
import os
from dataclasses import dataclass, asdict, field
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
USER_TIMEZONE = ZoneInfo(os.getenv("USER_TIMEZONE", "America/Toronto"))

TRIAL_DAYS = 7          # free active-use days before paywall
PAID_WHITELIST_RAW = os.getenv("PAID_WHITELIST", "")  # comma-separated user IDs, always free


def today_str() -> str:
    """Return today's date string in the user's local timezone."""
    return datetime.now(USER_TIMEZONE).date().isoformat()


def is_whitelisted(user_id: int) -> bool:
    if not PAID_WHITELIST_RAW:
        return False
    ids = {int(x.strip()) for x in PAID_WHITELIST_RAW.split(",") if x.strip()}
    return user_id in ids


# ── User profile ──────────────────────────────────────────────────────────────

@dataclass
class UserProfile:
    age: int = 0
    sex: str = ""
    weight_kg: float = 0.0
    goal: str = ""
    language: str = "en"

    def is_complete(self) -> bool:
        return all([self.age, self.sex, self.weight_kg, self.goal])


def _profile_path(user_id: int) -> Path:
    path = DATA_DIR / str(user_id)
    path.mkdir(parents=True, exist_ok=True)
    return path / "profile.json"


def get_profile(user_id: int) -> UserProfile | None:
    path = _profile_path(user_id)
    if path.exists():
        with open(path) as f:
            data = json.load(f)
        return UserProfile(**data)
    return None


def save_profile(user_id: int, profile: UserProfile) -> None:
    path = _profile_path(user_id)
    with open(path, "w") as f:
        json.dump(asdict(profile), f, indent=2)


def delete_profile(user_id: int) -> None:
    path = _profile_path(user_id)
    if path.exists():
        path.unlink()


# ── Trial / paywall tracking ──────────────────────────────────────────────────

@dataclass
class TrialStatus:
    first_seen: str = ""        # YYYY-MM-DD of first interaction
    active_days: list = field(default_factory=list)  # list of YYYY-MM-DD strings
    paid: bool = False

    def active_day_count(self) -> int:
        return len(set(self.active_days))

    def is_trial_expired(self) -> bool:
        return self.active_day_count() >= TRIAL_DAYS and not self.paid

    def record_activity(self) -> None:
        today = today_str()
        if not self.first_seen:
            self.first_seen = today
        if today not in self.active_days:
            self.active_days.append(today)

    def days_remaining(self) -> int:
        return max(0, TRIAL_DAYS - self.active_day_count())


def _trial_path(user_id: int) -> Path:
    path = DATA_DIR / str(user_id)
    path.mkdir(parents=True, exist_ok=True)
    return path / "trial.json"


def get_trial(user_id: int) -> TrialStatus:
    path = _trial_path(user_id)
    if path.exists():
        with open(path) as f:
            data = json.load(f)
        return TrialStatus(**data)
    return TrialStatus()


def save_trial(user_id: int, trial: TrialStatus) -> None:
    path = _trial_path(user_id)
    with open(path, "w") as f:
        json.dump(asdict(trial), f, indent=2)


def mark_paid(user_id: int) -> None:
    trial = get_trial(user_id)
    trial.paid = True
    save_trial(user_id, trial)


def record_and_check_trial(user_id: int) -> tuple[bool, TrialStatus]:
    """
    Record today as an active day and check if the user is blocked.
    Returns (is_blocked, trial_status).
    Whitelisted users and paid users are never blocked.
    """
    if is_whitelisted(user_id):
        return False, TrialStatus(paid=True)

    trial = get_trial(user_id)
    trial.record_activity()
    save_trial(user_id, trial)

    return trial.is_trial_expired(), trial


def current_month_str() -> str:
    return datetime.now(USER_TIMEZONE).strftime("%Y-%m")


# ── Daily meal log ────────────────────────────────────────────────────────────

@dataclass
class DailyLog:
    date: str = ""
    meal_count: int = 0
    total_kcal: float = 0.0
    total_protein: float = 0.0
    total_carbs: float = 0.0
    total_fat: float = 0.0
    meals: list = field(default_factory=list)

    def add_meal(self, kcal, protein, carbs, fat, label="") -> None:
        self.meal_count += 1
        self.total_kcal += kcal
        self.total_protein += protein
        self.total_carbs += carbs
        self.total_fat += fat
        self.meals.append({
            "meal_num": self.meal_count,
            "label": label,
            "kcal": kcal,
            "protein": protein,
            "carbs": carbs,
            "fat": fat,
        })

    def remaining(self, targets: dict) -> dict:
        return {
            "kcal": targets["kcal_max"] - self.total_kcal,
            "protein": targets["protein_max"] - self.total_protein,
            "carbs": targets["carbs_max"] - self.total_carbs,
            "fat": targets["fat_max"] - self.total_fat,
        }


def _log_path(user_id: int) -> Path:
    """Use timezone-aware today so midnight resets correctly for Toronto users."""
    path = DATA_DIR / str(user_id)
    path.mkdir(parents=True, exist_ok=True)
    return path / f"{today_str()}.json"


def get_today_log(user_id: int) -> DailyLog:
    path = _log_path(user_id)
    if path.exists():
        with open(path) as f:
            data = json.load(f)
        log = DailyLog(**data)
        # Safety check: if stored date doesn't match today, return fresh log
        if log.date != today_str():
            return DailyLog(date=today_str())
        return log
    return DailyLog(date=today_str())


def save_log(user_id: int, log: DailyLog) -> None:
    path = _log_path(user_id)
    with open(path, "w") as f:
        json.dump(asdict(log), f, indent=2)


def reset_log(user_id: int) -> None:
    path = _log_path(user_id)
    if path.exists():
        path.unlink()


def undo_last_meal(user_id: int) -> dict | None:
    log = get_today_log(user_id)
    if not log.meals:
        return None
    removed = log.meals.pop()
    log.meal_count -= 1
    log.total_kcal -= removed["kcal"]
    log.total_protein -= removed["protein"]
    log.total_carbs -= removed["carbs"]
    log.total_fat -= removed["fat"]
    save_log(user_id, log)
    return removed
