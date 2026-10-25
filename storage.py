"""
storage.py — Daily log + user profile + trial tracking.

File layout:
  data/{user_id}/profile.json      — onboarding answers, persists forever
  data/{user_id}/trial.json        — first-seen date, paid status, active days
  data/{user_id}/{YYYY-MM-DD}.json — daily meal log (date in user's timezone)
"""

import json
import os
import uuid
from dataclasses import dataclass, asdict, field, fields
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
USER_TIMEZONE = ZoneInfo(os.getenv("USER_TIMEZONE", "America/Toronto"))

TRIAL_DAYS = 7          # free active-use days before paywall
PAID_WHITELIST_RAW = os.getenv("PAID_WHITELIST", "")  # comma-separated user IDs, always free


def now_local() -> datetime:
    return datetime.now(USER_TIMEZONE)


def today_str() -> str:
    """Return today's date string in the user's local timezone."""
    return now_local().date().isoformat()


def is_whitelisted(user_id: int) -> bool:
    if not PAID_WHITELIST_RAW:
        return False
    ids = {int(x.strip()) for x in PAID_WHITELIST_RAW.split(",") if x.strip()}
    return user_id in ids


def _from_dict(cls, data: dict):
    """Build a dataclass from JSON, ignoring unknown keys (forward/backward compatible)."""
    known = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in data.items() if k in known})


def _user_dir(user_id: int) -> Path:
    path = DATA_DIR / str(user_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


# ── User profile ──────────────────────────────────────────────────────────────

@dataclass
class UserProfile:
    age: int = 0
    sex: str = ""
    weight_kg: float = 0.0
    goal: str = ""
    language: str = "en"
    height_cm: float = 0.0
    # Optional manual overrides, e.g. {"training": {"kcal": 1750, "protein": 120}, "rest": {...}}
    custom_targets: dict = field(default_factory=dict)

    def is_complete(self) -> bool:
        return all([self.age, self.sex, self.weight_kg, self.goal])


def get_profile(user_id: int) -> UserProfile | None:
    path = _user_dir(user_id) / "profile.json"
    if path.exists():
        with open(path) as f:
            return _from_dict(UserProfile, json.load(f))
    return None


def save_profile(user_id: int, profile: UserProfile) -> None:
    with open(_user_dir(user_id) / "profile.json", "w") as f:
        json.dump(asdict(profile), f, indent=2, ensure_ascii=False)


def delete_profile(user_id: int) -> None:
    path = _user_dir(user_id) / "profile.json"
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


def get_trial(user_id: int) -> TrialStatus:
    path = _user_dir(user_id) / "trial.json"
    if path.exists():
        with open(path) as f:
            return _from_dict(TrialStatus, json.load(f))
    return TrialStatus()


def save_trial(user_id: int, trial: TrialStatus) -> None:
    with open(_user_dir(user_id) / "trial.json", "w") as f:
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
    return now_local().strftime("%Y-%m")


# ── Daily meal log ────────────────────────────────────────────────────────────
# Each meal entry:
#   {id, time, name, items:[{name, amount, grams, kcal, protein_g, carbs_g, fat_g, fibre_g}],
#    kcal, protein, carbs, fat, fibre, description, photo_file_id, confidence,
#    assumptions, bot_message_id}
# Totals on the log are ALWAYS recomputed from meals, so edits/removals never drift.

@dataclass
class DailyLog:
    date: str = ""
    day_type: str = ""          # "training" / "rest" / "" (not chosen yet)
    meal_count: int = 0
    total_kcal: float = 0.0
    total_protein: float = 0.0
    total_carbs: float = 0.0
    total_fat: float = 0.0
    total_fibre: float = 0.0
    meals: list = field(default_factory=list)

    def recalc(self) -> None:
        for i, m in enumerate(self.meals, start=1):
            m.setdefault("id", uuid.uuid4().hex[:8])
            m["meal_num"] = i
        self.meal_count = len(self.meals)
        self.total_kcal = round(sum(float(m.get("kcal", 0) or 0) for m in self.meals), 1)
        self.total_protein = round(sum(float(m.get("protein", 0) or 0) for m in self.meals), 1)
        self.total_carbs = round(sum(float(m.get("carbs", 0) or 0) for m in self.meals), 1)
        self.total_fat = round(sum(float(m.get("fat", 0) or 0) for m in self.meals), 1)
        self.total_fibre = round(sum(float(m.get("fibre", 0) or 0) for m in self.meals), 1)

    def add_meal_entry(self, entry: dict) -> dict:
        entry = dict(entry)
        entry.setdefault("id", uuid.uuid4().hex[:8])
        entry.setdefault("time", now_local().strftime("%H:%M"))
        self.meals.append(entry)
        self.recalc()
        return entry

    def add_meal(self, kcal, protein, carbs, fat, label="") -> dict:
        """Backwards-compatible simple add."""
        return self.add_meal_entry({
            "name": label, "kcal": kcal, "protein": protein, "carbs": carbs, "fat": fat,
        })

    def find_meal(self, meal_id: str) -> dict | None:
        return next((m for m in self.meals if m.get("id") == meal_id), None)

    def find_by_message(self, message_id: int) -> dict | None:
        return next((m for m in self.meals if m.get("bot_message_id") == message_id), None)

    def replace_meal(self, meal_id: str, new_entry: dict) -> dict | None:
        for i, m in enumerate(self.meals):
            if m.get("id") == meal_id:
                merged = dict(new_entry)
                merged["id"] = meal_id
                merged.setdefault("time", m.get("time"))
                merged.setdefault("bot_message_id", m.get("bot_message_id"))
                merged.setdefault("photo_file_id", m.get("photo_file_id"))
                self.meals[i] = merged
                self.recalc()
                return merged
        return None

    def remove_meal(self, meal_id: str) -> dict | None:
        for i, m in enumerate(self.meals):
            if m.get("id") == meal_id:
                removed = self.meals.pop(i)
                self.recalc()
                return removed
        return None

    def remaining(self, targets: dict) -> dict:
        return {
            "kcal": targets["kcal_max"] - self.total_kcal,
            "protein": targets["protein_max"] - self.total_protein,
            "carbs": targets["carbs_max"] - self.total_carbs,
            "fat": targets["fat_max"] - self.total_fat,
        }


def _log_path(user_id: int, date_str: str) -> Path:
    return _user_dir(user_id) / f"{date_str}.json"


def get_log(user_id: int, date_str: str) -> DailyLog:
    path = _log_path(user_id, date_str)
    if path.exists():
        with open(path) as f:
            log = _from_dict(DailyLog, json.load(f))
        log.date = date_str
        # Older logs: normalise entries ("label" → "name") and recompute totals
        for m in log.meals:
            if "name" not in m and m.get("label"):
                m["name"] = m["label"]
        log.recalc()
        return log
    return DailyLog(date=date_str)


def get_today_log(user_id: int) -> DailyLog:
    """Timezone-aware: flips at local midnight."""
    return get_log(user_id, today_str())


def save_log(user_id: int, log: DailyLog) -> None:
    if not log.date:
        log.date = today_str()
    log.recalc()
    with open(_log_path(user_id, log.date), "w") as f:
        json.dump(asdict(log), f, indent=2, ensure_ascii=False)


def reset_log(user_id: int) -> None:
    path = _log_path(user_id, today_str())
    if path.exists():
        path.unlink()


def undo_last_meal(user_id: int) -> dict | None:
    log = get_today_log(user_id)
    if not log.meals:
        return None
    removed = log.meals.pop()
    save_log(user_id, log)
    return removed


def find_meal_by_message(user_id: int, message_id: int, days_back: int = 2) -> tuple[DailyLog, dict] | None:
    """Find which logged meal a bot message belongs to (for reply-to-correct)."""
    from datetime import timedelta
    base = now_local().date()
    for d in range(days_back + 1):
        log = get_log(user_id, (base - timedelta(days=d)).isoformat())
        meal = log.find_by_message(message_id)
        if meal:
            return log, meal
    return None
