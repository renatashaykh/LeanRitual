"""
targets.py — Personalised nutrition targets.

Profile: Renata, 28F, 59 kg, 160 cm, fat-loss goal.
Training days: 1,700–1,750 kcal | 120g protein
Rest days:     1,600–1,650 kcal | 110g protein

Macro split derived from:
  - Protein fixed per target
  - Fat: ~0.8–1g/kg → ~50–60g
  - Carbs: remainder of calories
"""

DAY_TYPE_TRAINING = "training"
DAY_TYPE_REST = "rest"

_TARGETS = {
    DAY_TYPE_TRAINING: {
        "kcal_min": 1700,
        "kcal_max": 1750,
        "protein_min": 115,
        "protein_max": 120,
        "carbs_min": 160,
        "carbs_max": 175,
        "fat_min": 52,
        "fat_max": 58,
    },
    DAY_TYPE_REST: {
        "kcal_min": 1600,
        "kcal_max": 1650,
        "protein_min": 108,
        "protein_max": 112,
        "carbs_min": 140,
        "carbs_max": 155,
        "fat_min": 50,
        "fat_max": 56,
    },
}

PROFILE = {
    "age": 28,
    "sex": "female",
    "weight_kg": 59,
    "height_cm": 160,
    "goal": "fat loss, preserve muscle",
    "training_days_per_week": 3,
}


def get_targets(day_type: str) -> dict:
    """Return macro targets dict for the given day type."""
    return _TARGETS.get(day_type, _TARGETS[DAY_TYPE_TRAINING])
