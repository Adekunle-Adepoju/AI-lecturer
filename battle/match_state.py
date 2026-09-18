import json

from django.conf import settings
import redis.asyncio as aioredis

_pool = None


def _get_redis():
    """Reuses the same host/port your CHANNEL_LAYERS config already points
    at, so there's one source of truth for where Redis lives — no second,
    separately-configured connection."""
    global _pool
    if _pool is None:
        config = settings.CHANNEL_LAYERS["default"]["CONFIG"]
        url = config["hosts"][0]
        _pool = aioredis.from_url(url, decode_responses=True)
    return _pool


def _key(room_code):
    return f"battle:match:{room_code}"


class MatchState:
    """Fully async wrapper around a match's live Redis state. Transient by
    design — this never gets a schema or migration, because it's discarded
    once BattleMatch/BattleMatchQuestion are written at match end."""

    def __init__(self, room_code):
        self.room_code = room_code
        self.redis = _get_redis()

    async def initialize(self, mode, team_a_players, team_b_players, questions):
        state = {
            "mode": mode,
            "team_a_players": team_a_players,
            "team_b_players": team_b_players,
            "questions": questions,
            "current_index": 0,
            "team_a_score": 0,
            "team_b_score": 0,
            "team_a_turn_index": 0,
            "team_b_turn_index": 0,
            "phase": "question_open",
            "buzzed_team": None,
            "answering_student_id": None,
            "bonus_team": None,
            "match_log": [],
        }
        await self.save(state)
        return state

    async def get(self):
        raw = await self.redis.get(_key(self.room_code))
        return json.loads(raw) if raw else None

    async def save(self, state):
        await self.redis.set(_key(self.room_code), json.dumps(state))

    async def clear(self):
        await self.redis.delete(_key(self.room_code))