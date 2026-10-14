import re

def level_number(raw):
    """Works whether level is stored as 300, "300" or "300L"."""
    m = re.search(r"\d+", str(raw or ""))
    return int(m.group()) if m else None

def allowed_question_levels(level):
    n = level_number(level)
    if n is None or n <= 100:
        return ("100",)
    return tuple(str(l) for l in range(100, n, 100))   # 300 -> ("100", "200")

def levels_for_room(player_levels):
    """The lowest-level player decides what the whole room can be asked."""
    nums = [n for n in (level_number(l) for l in player_levels) if n]
    return allowed_question_levels(min(nums) if nums else 100)