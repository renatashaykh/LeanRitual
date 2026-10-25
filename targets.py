"""
targets.py — Dynamic nutrition targets calculated from user profile.

Uses Mifflin-St Jeor BMR + activity multiplier to estimate TDEE,
then applies a deficit or surplus based on the user's goal.
"""

from storage import UserProfile

DAY_TYPE_TRAINING = "training"
DAY_TYPE_REST = "rest"

# Goal labels as stored in profile
GOAL_FAT_LOSS = "fat_loss"
GOAL_MUSCLE_GAIN = "muscle_gain"
GOAL_MAINTAIN = "maintain"

GOAL_LABELS = {
    GOAL_FAT_LOSS: "fat loss",
    GOAL_MUSCLE_GAIN: "muscle gain",
    GOAL_MAINTAIN: "maintenance",
}


def _calculate_targets(profile: UserProfile, day_type: str) -> dict:
    """
    Calculate personalised macro targets from a UserProfile.

    BMR (Mifflin-St Jeor):
      Female: 10*w + 6.25*h - 5*a - 161   (height assumed 165cm if unknown)
      Male:   10*w + 6.25*h - 5*a + 5

    Activity multiplier:
      Training day: 1.55 (moderate exercise)
      Rest day:     1.375 (lightly active)

    Calorie adjustment by goal:
      Fat loss:     -300 kcal deficit
      Muscle gain:  +250 kcal surplus
      Maintain:      0

    Protein:
      Fat loss:     2.0g / kg
      Muscle gain:  2.2g / kg
      Maintain:     1.8g / kg

    Fat: 0.9g / kg
    Carbs: remainder of calories
    """
    w = profile.weight_kg
    a = profile.age
    sex = profile.sex.lower()
    goal = profile.goal

    # Use real height when known, otherwise an average (165cm female, 175cm male)
    h = profile.height_cm or (165 if sex == "female" else 175)

    # BMR
    if sex == "female":
        bmr = 10 * w + 6.25 * h - 5 * a - 161
    else:
        bmr = 10 * w + 6.25 * h - 5 * a + 5

    # TDEE
    multiplier = 1.55 if day_type == DAY_TYPE_TRAINING else 1.375
    tdee = bmr * multiplier

    # Calorie target
    adjustments = {GOAL_FAT_LOSS: -300, GOAL_MUSCLE_GAIN: 250, GOAL_MAINTAIN: 0}
    adj = adjustments.get(goal, -300)
    kcal_target = round(tdee + adj)
    kcal_min = kcal_target - 25
    kcal_max = kcal_target + 25

    # Protein
    protein_multipliers = {GOAL_FAT_LOSS: 2.0, GOAL_MUSCLE_GAIN: 2.2, GOAL_MAINTAIN: 1.8}
    protein_g = round(w * protein_multipliers.get(goal, 2.0))
    protein_min = protein_g - 5
    protein_max = protein_g + 5

    # Fat
    fat_g = round(w * 0.9)
    fat_min = fat_g - 5
    fat_max = fat_g + 5

    # Carbs (fill remaining calories)
    protein_kcal = protein_g * 4
    fat_kcal = fat_g * 9
    carbs_kcal = kcal_target - protein_kcal - fat_kcal
    carbs_g = max(round(carbs_kcal / 4), 50)
    carbs_min = carbs_g - 15
    carbs_max = carbs_g + 15

    return {
        "kcal_min": kcal_min,
        "kcal_max": kcal_max,
        "protein_min": protein_min,
        "protein_max": protein_max,
        "carbs_min": carbs_min,
        "carbs_max": carbs_max,
        "fat_min": fat_min,
        "fat_max": fat_max,
    }


def _apply_custom(base: dict, custom: dict, weight_kg: float) -> dict:
    """
    Apply a user's manual targets on top of the calculated ones.
    custom = {"kcal": 1750, "protein": 120, "fat": 55 (optional)}
    Carbs fill whatever calories are left.
    """
    kcal = int(custom.get("kcal") or (base["kcal_min"] + base["kcal_max"]) // 2)
    protein = int(custom.get("protein") or (base["protein_min"] + base["protein_max"]) // 2)
    fat = int(custom.get("fat") or round(weight_kg * 0.9) or (base["fat_min"] + base["fat_max"]) // 2)
    carbs = max(round((kcal - protein * 4 - fat * 9) / 4), 50)
    return {
        "kcal_min": kcal - 25, "kcal_max": kcal + 25,
        "protein_min": protein - 5, "protein_max": protein + 5,
        "carbs_min": carbs - 15, "carbs_max": carbs + 15,
        "fat_min": fat - 5, "fat_max": fat + 5,
        "custom": True,
    }


def get_targets(day_type: str, profile: UserProfile | None = None) -> dict:
    """Return macro targets for the given day type and user profile."""
    if not (profile and profile.is_complete()):
        # Fallback defaults (moderate female, fat loss)
        profile = UserProfile(age=30, sex="female", weight_kg=65, goal=GOAL_FAT_LOSS)
    base = _calculate_targets(profile, day_type)
    custom = (profile.custom_targets or {}).get(day_type)
    if custom:
        return _apply_custom(base, custom, profile.weight_kg)
    base["custom"] = False
    return base
