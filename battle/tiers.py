TIERS = [  # (wins needed, name), highest first
    (50, "Legend"), (20, "Grandmaster"), (10, "Master"),
    (5, "Professional"), (2, "Amateur"), (0, "Rookie"),
]

def tier_for(wins):
    for needed, name in TIERS:
        if wins >= needed:
            return name

def next_tier(wins):
    """(name, wins_still_needed) or None at the top."""
    for needed, name in reversed(TIERS):
        if wins < needed:
            return name, needed - wins
    return None