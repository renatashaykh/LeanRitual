"""
storage.py — Lightweight JSON-based daily log.

Each user gets a file: data/{user_id}/{YYYY-MM-DD}.json
On Railway/Render with ephemeral disks this resets on redeploy,
which is fine for a single-user bot; swap for Redis/SQLite for persistence.
"""

import json
import os
from dataclasses import dataclass, asdict, field
from datetime import date
from pathlib import Path

DATA_DIR = Path(os.getenv("DATA_DIR", "data"))


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
        """Return how much of each macro is still left to hit targets."""
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
