"""
storage.py — Daily log + user profile persistence.

File layout:
  data/{user_id}/profile.json      — onboarding answers, persists forever
  data/{user_id}/{YYYY-MM-DD}.json — daily meal log
"""

import json
import os
from dataclasses import dataclass, asdict, field
from datetime import date
from pathlib import Path

DATA_DIR = Path(os.getenv("DATA_DIR", "data"))


# ── User profile (collected during onboarding) ────────────────────────────────

@dataclass
class UserProfile:
    age: int = 0
    sex: str = ""           # "male" / "female"
    weight_kg: float = 0.0
    goal: str = ""          # "fat_loss" / "muscle_gain" / "maintain"
    language: str = "en"    # "en" / "ru"

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


# ── Daily meal log ─────────────────────────────────────────────────────────────

@dataclass
class DailyLog:
    date: str = ""
    meal_count: int = 0
    total_kcal: float = 0.0
    total_protein: float = 0.0
    total_carbs: float = 0.0
    total_fat: float = 0.0
    meals: list = field(default_factory=list)

    def add_meal(
        self,
        kcal: float,
        protein: float,
        carbs: float,
        fat: float,
        label: str = "",
    ) -> None:
        self.meal_count += 1
        self.total_kcal += kcal
        self.total_protein += protein
        self.total_carbs += carbs
        self.total_fat += fat
        self.meals.append(
            {
                "meal_num": self.meal_count,
                "label": label,
                "kcal": kcal,
                "protein": protein,
                "carbs": carbs,
                "fat": fat,
            }
        )

    def remaining(self, targets: dict) -> dict:
        return {
            "kcal": targets["kcal_max"] - self.total_kcal,
            "protein": targets["protein_max"] - self.total_protein,
            "carbs": targets["carbs_max"] - self.total_carbs,
            "fat": targets["fat_max"] - self.total_fat,
        }


def _log_path(user_id: int) -> Path:
    today = date.today().isoformat()
    path = DATA_DIR / str(user_id)
    path.mkdir(parents=True, exist_ok=True)
    return path / f"{today}.json"


def get_today_log(user_id: int) -> DailyLog:
    path = _log_path(user_id)
    if path.exists():
        with open(path) as f:
            data = json.load(f)
        return DailyLog(**data)
    return DailyLog(date=date.today().isoformat())


def save_log(user_id: int, log: DailyLog) -> None:
    path = _log_path(user_id)
    with open(path, "w") as f:
        json.dump(asdict(log), f, indent=2)


def reset_log(user_id: int) -> None:
    path = _log_path(user_id)
    if path.exists():
        path.unlink()
