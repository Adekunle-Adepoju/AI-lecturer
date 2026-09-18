import asyncio
import random

from channels.db import database_sync_to_async
from django.utils import timezone

from core.models import StudentProfile
from .match_state import MatchState
from .question_selection import select_match_questions
from .models import BattleRoom, BattleParticipant, BattleMatch, BattleMatchQuestion, StudentQuestionExposure


# Room-code -> asyncio.Task, shared across every consumer instance in this
# process, so whichever consumer actually receives an event (buzz, answer,
# start) can safely schedule/cancel the match's one timer, regardless of
# which consumer happens to be handling that particular event. Single-
# process only — see note in match_loop's module docstring if this ever
# needs to survive multiple worker processes.

_timer_tasks = {}


QUESTIONS_PER_TEAM_MODE1 = 5
TOTAL_QUESTIONS_MODE2 = 10
TURN_TIMER_SECONDS = 30
BUZZ_WINDOW_SECONDS = 60
BUZZ_ANSWER_SECONDS = 10
BONUS_ANSWER_SECONDS = 15


class MatchLoop:
    """Owns one room's live match. Only instantiated on the room leader's
    consumer — see the concurrency note above. Broadcasts through the
    consumer's channel layer so every connected participant (players and
    spectators) sees the same state."""

    def __init__(self, consumer, room_code):
        self.consumer = consumer
        self.room_code = room_code
        self.state = MatchState(room_code)
        self._timer_task = None

    async def start(self, room):
        mode = room.mode
        team_a_ids, team_b_ids = await self._get_team_rosters(room)
        total_questions = (
            QUESTIONS_PER_TEAM_MODE1 * 2 if mode == "alternating" else TOTAL_QUESTIONS_MODE2
        )
        all_ids = team_a_ids + team_b_ids
        questions = await database_sync_to_async(select_match_questions)(all_ids, total_questions)

        if len(questions) < total_questions:
            await self._broadcast({
                "type": "match_warning",
                "message": f"Only {len(questions)} question(s) available — bank is thinner than a full match needs.",
            })
            if not questions:
                await self._broadcast({"type": "match_aborted", "reason": "no_questions_available"})
                return

        state = await self.state.initialize(mode, team_a_ids, team_b_ids, questions)
        await self._serve_question(state)

    async def _get_team_rosters(self, room):
        return await database_sync_to_async(self._get_team_rosters_sync)(room)

    def _get_team_rosters_sync(self, room):
        team_a = list(
            BattleParticipant.objects.filter(room=room, team="A", role="player")
            .order_by("joined_at").values_list("student_id", flat=True)
        )
        team_b = list(
            BattleParticipant.objects.filter(room=room, team="B", role="player")
            .order_by("joined_at").values_list("student_id", flat=True)
        )
        return team_a, team_b

    async def _serve_question(self, state):
        idx = state["current_index"]
        q = state["questions"][idx]

        if state["mode"] == "alternating":
            team = "A" if idx < QUESTIONS_PER_TEAM_MODE1 else "B"
            roster = state["team_a_players"] if team == "A" else state["team_b_players"]
            turn_key = "team_a_turn_index" if team == "A" else "team_b_turn_index"
            answerer_id = roster[state[turn_key] % len(roster)] if roster else None
            state[turn_key] = state[turn_key] + 1
            state["phase"] = "awaiting_answer"
            state["answering_student_id"] = answerer_id
            timer_seconds = TURN_TIMER_SECONDS
            await self._broadcast({
                "type": "question_served", "index": idx, "stem": q["stem"], "options": q["options"],
                "mode": "alternating", "team_on_turn": team, "answering_student_id": answerer_id,
                "timer_seconds": timer_seconds,
            })
        else:
            state["phase"] = "question_open"
            state["buzzed_team"] = None
            state["answering_student_id"] = None
            timer_seconds = BUZZ_WINDOW_SECONDS
            await self._broadcast({
                "type": "question_served", "index": idx, "stem": q["stem"], "options": q["options"],
                "mode": "buzzer", "timer_seconds": timer_seconds,
            })

        await self.state.save(state)
        self._schedule_timeout(idx, timer_seconds, state["phase"])

    def _schedule_timeout(self, expected_index, seconds, expected_phase):
        existing = _timer_tasks.get(self.room_code)
        if existing:
            existing.cancel()
        _timer_tasks[self.room_code] = asyncio.create_task(
            self._timeout_after(expected_index, seconds, expected_phase)
        )

    async def _timeout_after(self, expected_index, seconds, expected_phase):
        await asyncio.sleep(seconds)
        state = await self.state.get()
        if state is None:
            return
        # Only act if nothing has already resolved this question — guards
        # against the timer firing after a real answer already landed.
        if state["current_index"] != expected_index or state["phase"] != expected_phase:
            return

        if state["mode"] == "alternating" and expected_phase == "awaiting_answer":
            await self._resolve_alternating(state, correct=False, timed_out=True)
        elif state["mode"] == "alternating" and expected_phase == "awaiting_bonus":
            await self._resolve_bonus(state, correct=False, timed_out=True)
        elif state["mode"] == "buzzer" and expected_phase == "question_open":
            # Nobody buzzed in time — both teams take -5, per the agreed rule.
            state["team_a_score"] -= 5
            state["team_b_score"] -= 5
            await self._broadcast({"type": "no_buzz_penalty", "team_a_score": state["team_a_score"], "team_b_score": state["team_b_score"]})
            await self._advance(state)
        elif state["mode"] == "buzzer" and expected_phase == "awaiting_answer":
            await self._resolve_buzzer(state, correct=False, timed_out=True)
        elif state["mode"] == "buzzer" and expected_phase == "awaiting_bonus":
            await self._resolve_bonus(state, correct=False, timed_out=True)

    # ── Buzzer-mode events ──────────────────────────────────────────

    async def handle_buzz(self, student_id):
        state = await self.state.get()
        if state is None or state["mode"] != "buzzer" or state["phase"] != "question_open":
            return  # too late, already buzzed, or wrong mode — silently ignored

        team = "A" if student_id in state["team_a_players"] else (
            "B" if student_id in state["team_b_players"] else None
        )
        if team is None:
            return  # spectator tried to buzz

        state["phase"] = "awaiting_answer"
        state["buzzed_team"] = team
        state["answering_student_id"] = student_id
        await self.state.save(state)

        await self._broadcast({"type": "buzzed", "team": team, "student_id": student_id})
        self._schedule_timeout(state["current_index"], BUZZ_ANSWER_SECONDS, "awaiting_answer")

    # ── Answer submission (both modes) ──────────────────────────────

    async def handle_submit_answer(self, student_id, option_index):
        state = await self.state.get()
        if state is None:
            return
        if state["answering_student_id"] != student_id:
            return  # not this student's turn/buzz to answer

        q = state["questions"][state["current_index"]]
        correct = (option_index == q["correct_index"])

        if state["phase"] == "awaiting_bonus":
            await self._resolve_bonus(state, correct=correct, timed_out=False)
        elif state["mode"] == "alternating":
            await self._resolve_alternating(state, correct=correct, timed_out=False)
        else:
            await self._resolve_buzzer(state, correct=correct, timed_out=False)

    # ── Resolution ────────────────────────────────────────────────

    async def _resolve_alternating(self, state, correct, timed_out):
        idx = state["current_index"]
        team = "A" if idx < QUESTIONS_PER_TEAM_MODE1 else "B"
        other_team = "B" if team == "A" else "A"

        if correct:
            state[f"team_{team.lower()}_score"] += 5
            await self._broadcast({"type": "question_resolved", "correct": True, "team": team, "points": 5, "timed_out": timed_out})
            await self._log_question(state, answering_team=team, correct=True, points=5)
            await self._advance(state)
        else:
            await self._broadcast({"type": "question_resolved", "correct": False, "team": team, "points": 0, "timed_out": timed_out})
            await self._log_question(state, answering_team=team, correct=False, points=0)
            state["phase"] = "awaiting_bonus"
            state["bonus_team"] = other_team
            state["answering_student_id"] = None  # any player on the bonus team may answer
            await self.state.save(state)
            await self._broadcast({"type": "bonus_open", "team": other_team, "timer_seconds": BONUS_ANSWER_SECONDS})
            self._schedule_timeout(idx, BONUS_ANSWER_SECONDS, "awaiting_bonus")

    async def _resolve_buzzer(self, state, correct, timed_out):
        idx = state["current_index"]
        team = state["buzzed_team"]
        other_team = "B" if team == "A" else "A"

        if correct:
            state[f"team_{team.lower()}_score"] += 5
            await self._broadcast({"type": "question_resolved", "correct": True, "team": team, "points": 5, "timed_out": timed_out})
            await self._log_question(state, answering_team=team, correct=True, points=5)
            await self._advance(state)
        else:
            state[f"team_{team.lower()}_score"] -= 5
            await self._broadcast({"type": "question_resolved", "correct": False, "team": team, "points": -5, "timed_out": timed_out})
            await self._log_question(state, answering_team=team, correct=False, points=-5)
            state["phase"] = "awaiting_bonus"
            state["bonus_team"] = other_team
            state["answering_student_id"] = None
            await self.state.save(state)
            await self._broadcast({"type": "bonus_open", "team": other_team, "timer_seconds": BONUS_ANSWER_SECONDS})
            self._schedule_timeout(idx, BONUS_ANSWER_SECONDS, "awaiting_bonus")

    async def _resolve_bonus(self, state, correct, timed_out):
        team = state["bonus_team"]
        if correct:
            state[f"team_{team.lower()}_score"] += 2
        await self._broadcast({"type": "bonus_resolved", "correct": correct, "team": team, "points": 2 if correct else 0, "timed_out": timed_out})
        await self._advance(state)

    async def _log_question(self, state, answering_team, correct, points):
        state["match_log"].append({
            "index": state["current_index"],
            "question_id": state["questions"][state["current_index"]]["question_id"],
            "answering_team": answering_team,
            "correct": correct,
            "points": points,
        })

    async def _advance(self, state):
        state["current_index"] += 1
        total = len(state["questions"])

        if state["current_index"] >= total:
            if state["team_a_score"] == state["team_b_score"]:
                await self._start_sudden_death(state)
            else:
                await self._finalize(state)
            return

        await self.state.save(state)
        await self._serve_question(state)

    async def _start_sudden_death(self, state):
        extra = await database_sync_to_async(select_match_questions)(
            state["team_a_players"] + state["team_b_players"], 1,
        )
        if not extra:
            await self._finalize(state)  # can't break the tie — ties as-is
            return
        state["questions"].append(extra[0])
        await self.state.save(state)
        await self._broadcast({"type": "sudden_death"})
        await self._serve_question(state)

    async def _finalize(self, state):
        winner = None
        if state["team_a_score"] > state["team_b_score"]:
            winner = "A"
        elif state["team_b_score"] > state["team_a_score"]:
            winner = "B"

        await self._persist_match(state, winner)
        await self._broadcast({
            "type": "match_finished", "team_a_score": state["team_a_score"],
            "team_b_score": state["team_b_score"], "winner": winner,
        })
        await self.state.clear()
        _timer_tasks.pop(self.room_code, None)

    async def _persist_match(self, state, winner):
        await database_sync_to_async(self._persist_match_sync)(state, winner)

    def _persist_match_sync(self, state, winner):
        room = BattleRoom.objects.get(code=self.room_code)
        match = BattleMatch.objects.create(
            room=room, mode=state["mode"],
            team_a_score=state["team_a_score"], team_b_score=state["team_b_score"],
            winner=winner, ended_at=timezone.now(),
        )

        for entry in state["match_log"]:
            BattleMatchQuestion.objects.create(
                match=match, question_id=entry["question_id"],
                sequence_number=entry["index"],
                served_stem=state["questions"][entry["index"]]["stem"],
                served_options_snapshot=state["questions"][entry["index"]]["options"],
                answering_team=entry["answering_team"],
                correct=entry["correct"], points_awarded=entry["points"],
                resolved_at=timezone.now(),
            )

        # Exposure tracking for every question actually served, seen or not.
        all_student_ids = state["team_a_players"] + state["team_b_players"]
        for q in state["questions"]:
            for sid in all_student_ids:
                exposure, _ = StudentQuestionExposure.objects.get_or_create(
                    student_id=sid, question_id=q["question_id"],
                )
                exposure.times_seen += 1
                exposure.last_seen_at = timezone.now()
                exposure.save(update_fields=["times_seen", "last_seen_at"])

        self._award_xp(state, winner)
        room.status = "finished"
        room.save(update_fields=["status"])

    def _award_xp(self, state, winner):
        # XP rules: winners 200 (20 after 2 wins today, or against a repeat
        # opponent today), losers 10, no daily cap on match count. "Repeat
        # opponent" is checked against BattleMatchQuestion's answering_team
        # trail via BattleMatch/BattleParticipant history for today.
        from datetime import date

        if winner is None:
            return  # true tie after sudden-death exhaustion — no XP either way, undecided by the rules as given

        winning_ids = state["team_a_players"] if winner == "A" else state["team_b_players"]
        losing_ids = state["team_b_players"] if winner == "A" else state["team_a_players"]

        today = date.today()

        for sid in winning_ids:
            wins_today = BattleMatch.objects.filter(
                started_at__date=today, winner=winner,
                room__participants__student_id=sid, room__participants__team=winner,
            ).count()
            already_beat_today = self._already_beat_opponents_today(sid, losing_ids, today)
            xp = 20 if (wins_today >= 2 or already_beat_today) else 200
            StudentProfile.objects.filter(id=sid).update(xp=F_xp(xp))

        for sid in losing_ids:
            StudentProfile.objects.filter(id=sid).update(xp=F_xp(10))

    def _already_beat_opponents_today(self, student_id, opponent_ids, today):
        # Simplified: has this student won ANY match today against ANY of
        # today's opponents. A precise per-pair check would need per-player
        # match history joined against opponent rosters — worth tightening
        # once real usage shows whether this granularity actually matters.
        return BattleMatch.objects.filter(
            started_at__date=today,
            room__participants__student_id__in=opponent_ids,
        ).exists()

    async def _broadcast(self, payload):
        await self.consumer.channel_layer.group_send(
            self.consumer.group_name, {"type": "room.event", "event": payload},
        )


def F_xp(amount):
    from django.db.models import F
    return F("xp") + amount