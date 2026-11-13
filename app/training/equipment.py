from typing import Any

LOCATIONS = {"gym": "商业健身房", "home": "家庭训练", "bodyweight": "徒手训练", "custom": "自定义场地"}
GYM_EQUIPMENT = {"body only", "barbell", "dumbbell", "kettlebells", "cable", "machine", "bands", "e-z curl bar"}


def effective_equipment(profile: dict[str, Any]) -> set[str] | None:
    """None means unknown; an empty set means explicitly nothing is available."""
    location = profile.get("training_location")
    if location == "gym":
        available = set(GYM_EQUIPMENT)
    elif location == "bodyweight":
        available = {"body only"}
    elif profile.get("equipment") is not None or location in {"home", "custom"}:
        available = set(profile.get("equipment") or []) | {"body only"}
    else:
        return None
    return available - set(profile.get("excluded_equipment") or [])
