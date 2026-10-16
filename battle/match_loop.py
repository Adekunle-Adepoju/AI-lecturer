import asyncio
import random
import time
import logging
from .tiers import tier_for, next_tier

from channels.db import database_sync_to_async
from django.utils import timezone

from core.models import StudentProfile
from .match_state import MatchState
from .question_selection import select_match_questions
from .models import BattleRoom, BattleParticipant, BattleMatch, BattleMatchQuestion, StudentQuestionExposure,BattleMatchPlayer, BattleStats
from zoneinfo import ZoneInfo
from .levels import levels_for_room


logger = logging.getLogger(__name__)

# Room-code -> asyncio.Task, shared across every consumer instance in this
# process. Single-process only.
_timer_tasks = {}

# Room-code -> asyncio.Lock. Every action that changes match state (buzz,
# answer, timeout) takes this lock, so two events can never interleave.
_room_locks = {}



QUESTIONS_PER_TEAM_MODE1 = 5
TOTAL_QUESTIONS_MODE2 = 10

# ── Timer lengths (seconds): change these to tune the game ──
TURN_TIMER_SECONDS = 30      # alternating: time to answer
BUZZ_WINDOW_SECONDS = 20     # buzzer: time for someone to buzz
BUZZ_ANSWER_SECONDS = 10     # buzzer: time to answer after buzzing
BONUS_ANSWER_SECONDS = 15    # the other team's bonus attempt
REVEAL_PAUSE_SECONDS = 3     # pause to show the result before the next question
INTRO_SECONDS = 5            # rules screen shown before the first question
FORFEIT_GRACE_SECONDS = 45


def _timer_fields(seconds):
    """Absolute server deadline, so every screen counts down to the same moment."""
    now = time.time()
    return {"timer_seconds": seconds, "deadline": now + seconds, "server_now": now}


def _lock_for(room_code):
    lock = _room_locks.get(room_code)
    if lock is None:
        lock = _room_locks[room_code] = asyncio.Lock()
    return lock

def _rules_for(mode, total):
    if mode == "buzzer":
        return [
            f"{total} questions. The answer options stay locked until someone buzzes in.",
            f"You have {BUZZ_WINDOW_SECONDS} seconds to buzz. The first buzz wins the question.",
            f"The buzzer then has {BUZZ_ANSWER_SECONDS} seconds to answer. Correct: +5 points. Wrong or out of time: -5 points.",
            f"After a miss, the other team gets a free attempt with no buzz needed ({BONUS_ANSWER_SECONDS} seconds, any player can answer) worth +2 points.",
            "If nobody buzzes in time, both teams lose 5 points.",
            "Tied at the end? One sudden-death question decides it.",
        ]
    return [
        f"{total} questions. The teams take turns: Alpha gets question 1, Bravo gets question 2, and so on.",
        "One player answers each question, rotating through the team.",
        f"You have {TURN_TIMER_SECONDS} seconds to answer. Correct: +5 points.",
        f"Wrong or out of time: 0 points, and the other team gets a free bonus attempt ({BONUS_ANSWER_SECONDS} seconds, any player can answer) worth +2 points.",
        "Tied at the end? One sudden-death question decides it.",
    ]

def _team_for_index(idx):
    """Even-numbered questions (1st, 3rd, 5th...) go to Alpha, odd ones to Bravo."""
    return "A" if idx % 2 == 0 else "B"

class MatchLoop:
    """Owns one room's live match. A fresh instance is created per incoming
    event; the real state lives in MatchState. Broadcasts through the
    consumer's channel layer so every connected participant (players and
    spectators) sees the same state."""

    def _player_info_sync(self, ids):
        rows = list(StudentProfile.objects.filter(id__in=ids).values_list("level", "department"))
        return [r[0] for r in rows], sorted({r[1] for r in rows if r[1]})

    async def _any_connected(self, student_ids):
        def _q():
            return BattleParticipant.objects.filter(
                room__code=self.room_code, student_id__in=student_ids,
                connection_status="connected",
            ).exists()
        return await database_sync_to_async(_q)()

    async def forfeit_check(self):
        """Runs after the grace period. A team with nobody connected loses."""
        async with _lock_for(self.room_code):
            state = await self.state.get()
            if state is None:
                return
            a_alive = await self._any_connected(state["team_a_players"])
            b_alive = await self._any_connected(state["team_b_players"])
            if a_alive and b_alive:
                return
            if not a_alive and not b_alive:
                await self._abandon()
                return
            await self._finalize(state, forced_winner="A" if a_alive else "B")

    async def _abandon(self):
        """Everyone left. No result, no wins, no stats. Just clean up."""
        def _close():
            BattleRoom.objects.filter(code=self.room_code).update(status="finished")
        await database_sync_to_async(_close)()
        await self.state.clear()
        task = _timer_tasks.pop(self.room_code, None)
        if task and task is not asyncio.current_task():
            task.cancel()
        _room_locks.pop(self.room_code, None)

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
        raw_levels, departments = await database_sync_to_async(self._player_info_sync)(all_ids)
        levels = levels_for_room(raw_levels)
        questions = await database_sync_to_async(select_match_questions)(
            all_ids, total_questions, levels=levels, departments=departments,
            course_codes=[room.course_code] if room.course_code else None,
        )


        if len(questions) < total_questions:
            await self._broadcast({
                "type": "match_warning",
                "message": f"Only {len(questions)} question(s) available — bank is thinner than a full match needs.",
            })
            if not questions:
                await database_sync_to_async(
                    BattleRoom.objects.filter(code=self.room_code).update
                )(status="waiting")
                await self._broadcast({"type": "match_aborted",
                                       "reason": "No questions available for this room's level yet."})
                return

        state = await self.state.initialize(mode, team_a_ids, team_b_ids, questions)
        state["phase"] = "intro"
        state["levels"] = list(levels)
        state["departments"] = departments
        state["course_codes"] = [room.course_code] if room.course_code else []
        await self.state.save(state)
        await self._broadcast({
            "type": "match_intro", "mode": mode,
            "rules": _rules_for(mode, len(questions)),
            **_timer_fields(INTRO_SECONDS),
        })
        self._schedule_timeout(state["current_index"], INTRO_SECONDS, "intro")

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

    def _correct_index(self, state):
        return state["questions"][state["current_index"]]["correct_index"]

    async def _serve_question(self, state):
        idx = state["current_index"]
        q = state["questions"][idx]

        if state["mode"] == "alternating":
            team = _team_for_index(idx)
            roster = state["team_a_players"] if team == "A" else state["team_b_players"]
            turn_key = "team_a_turn_index" if team == "A" else "team_b_turn_index"
            if roster:
                answerer_id, state[turn_key] = await self._pick_answerer(roster, state[turn_key])
            else:
                answerer_id = None
            state["phase"] = "awaiting_answer"
            state["answering_student_id"] = answerer_id
            timer_seconds = TURN_TIMER_SECONDS
            await self._broadcast({
                "type": "question_served", "index": idx, "stem": q["stem"], "options": q["options"],
                "mode": "alternating", "team_on_turn": team, "answering_student_id": answerer_id,
                **_timer_fields(timer_seconds),
            })
        else:
            state["phase"] = "question_open"
            state["buzzed_team"] = None
            state["answering_student_id"] = None
            timer_seconds = BUZZ_WINDOW_SECONDS
            await self._broadcast({
                "type": "question_served", "index": idx, "stem": q["stem"], "options": q["options"],
                "mode": "buzzer", **_timer_fields(timer_seconds),
            })

        await self.state.save(state)
        self._schedule_timeout(idx, timer_seconds, state["phase"])

    def _schedule_timeout(self, expected_index, seconds, expected_phase):
        existing = _timer_tasks.get(self.room_code)
        # Never cancel ourselves: a timeout task that is resolving a question
        # also schedules the next timer.
        if existing and existing is not asyncio.current_task():
            existing.cancel()
        _timer_tasks[self.room_code] = asyncio.create_task(
            self._guarded_timeout(expected_index, seconds, expected_phase)
        )

    async def _guarded_timeout(self, expected_index, seconds, expected_phase):
        try:
            await self._timeout_after(expected_index, seconds, expected_phase)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Battle timer failed in room %s (phase %s)", self.room_code, expected_phase)

    async def _timeout_after(self, expected_index, seconds, expected_phase):
        await asyncio.sleep(seconds)
        async with _lock_for(self.room_code):
            state = await self.state.get()
            if state is None:
                return
            # Only act if nothing has already resolved this question.
            if state["current_index"] != expected_index or state["phase"] != expected_phase:
                return

            if expected_phase == "intro":
                await self._serve_question(state)
            elif expected_phase == "revealing":
                await self._advance_now(state)
            elif state["mode"] == "alternating" and expected_phase == "awaiting_answer":
                await self._resolve_alternating(state, correct=False, timed_out=True)
            elif expected_phase == "awaiting_bonus":
                await self._resolve_bonus(state, correct=False, timed_out=True)
            elif state["mode"] == "buzzer" and expected_phase == "question_open":
                # Nobody buzzed in time: both teams take -5.
                state["team_a_score"] -= 5
                state["team_b_score"] -= 5
                await self._broadcast({
                    "type": "no_buzz_penalty",
                    "team_a_score": state["team_a_score"], "team_b_score": state["team_b_score"],
                    "correct_index": self._reveal(state),
                })
                await self._advance(state)
            elif state["mode"] == "buzzer" and expected_phase == "awaiting_answer":
                await self._resolve_buzzer(state, correct=False, timed_out=True)

    # ── Buzzer-mode events ──────────────────────────────────────────

    async def handle_buzz(self, student_id):
        async with _lock_for(self.room_code):
            state = await self.state.get()
            if state is None or state["mode"] != "buzzer" or state["phase"] != "question_open":
                return  # too late, already buzzed, or wrong mode: silently ignored

            team = "A" if student_id in state["team_a_players"] else (
                "B" if student_id in state["team_b_players"] else None
            )
            if team is None:
                return  # spectator tried to buzz

            state["phase"] = "awaiting_answer"
            state["buzzed_team"] = team
            state["answering_student_id"] = student_id
            await self.state.save(state)

            await self._broadcast({
                "type": "buzzed", "team": team, "student_id": student_id,
                **_timer_fields(BUZZ_ANSWER_SECONDS),
            })
            self._schedule_timeout(state["current_index"], BUZZ_ANSWER_SECONDS, "awaiting_answer")

    # ── Answer submission (both modes) ──────────────────────────────

    async def handle_submit_answer(self, student_id, option_index, question_index=None):
        async with _lock_for(self.room_code):
            state = await self.state.get()
            if state is None:
                return
            if question_index != state["current_index"]:
                return  # stale click from a previous question

            phase = state["phase"]
            if phase == "awaiting_bonus":
                roster = state["team_a_players"] if state["bonus_team"] == "A" else state["team_b_players"]
                if student_id not in roster:
                    return  # only the bonus team may answer
            elif phase == "awaiting_answer":
                if state["answering_student_id"] != student_id:
                    return  # not this student's turn/buzz to answer
            else:
                return

            q = state["questions"][state["current_index"]]
            if (not isinstance(option_index, int) or isinstance(option_index, bool)
                    or not 0 <= option_index < len(q["options"])):
                return
            correct = (option_index == q["correct_index"])

            if phase == "awaiting_bonus":
                await self._resolve_bonus(state, correct=correct, timed_out=False, option_index=option_index)
            elif state["mode"] == "alternating":
                await self._resolve_alternating(state, correct=correct, timed_out=False, option_index=option_index)
            else:
                await self._resolve_buzzer(state, correct=correct, timed_out=False, option_index=option_index)

    # ── Resolution ────────────────────────────────────────────────

    async def _resolve_alternating(self, state, correct, timed_out, option_index=None):
        idx = state["current_index"]
        team = _team_for_index(idx)
        other_team = "B" if team == "A" else "A"

        if correct:
            state[f"team_{team.lower()}_score"] += 5
            await self._broadcast({
                "type": "question_resolved", "correct": True, "team": team, "points": 5,
                "timed_out": timed_out, "answer_index": option_index,
                "correct_index": self._reveal(state),
            })
            await self._log_question(state, answering_team=team, correct=True, points=5)
            await self._advance(state)
        else:
            await self._broadcast({
                "type": "question_resolved", "correct": False, "team": team, "points": 0,
                "timed_out": timed_out, "answer_index": option_index,
            })
            await self._log_question(state, answering_team=team, correct=False, points=0)
            await self._open_bonus(state, other_team)

    async def _resolve_buzzer(self, state, correct, timed_out, option_index=None):
        team = state["buzzed_team"]
        other_team = "B" if team == "A" else "A"

        if correct:
            state[f"team_{team.lower()}_score"] += 5
            await self._broadcast({
                "type": "question_resolved", "correct": True, "team": team, "points": 5,
                "timed_out": timed_out, "answer_index": option_index,
                "correct_index": self._reveal(state),
            })
            await self._log_question(state, answering_team=team, correct=True, points=5)
            await self._advance(state)
        else:
            state[f"team_{team.lower()}_score"] -= 5
            await self._broadcast({
                "type": "question_resolved", "correct": False, "team": team, "points": -5,
                "timed_out": timed_out, "answer_index": option_index,
            })
            await self._log_question(state, answering_team=team, correct=False, points=-5)
            await self._open_bonus(state, other_team)

    async def _open_bonus(self, state, other_team):
        """The other team gets a free attempt, no buzz needed. Any of its
        players may answer."""
        state["phase"] = "awaiting_bonus"
        state["bonus_team"] = other_team
        state["answering_student_id"] = None
        await self.state.save(state)
        await self._broadcast({
            "type": "bonus_open", "team": other_team, **_timer_fields(BONUS_ANSWER_SECONDS),
        })
        self._schedule_timeout(state["current_index"], BONUS_ANSWER_SECONDS, "awaiting_bonus")

    async def _resolve_bonus(self, state, correct, timed_out, option_index=None):
        team = state["bonus_team"]
        if correct:
            state[f"team_{team.lower()}_score"] += 2
        await self._broadcast({
            "type": "bonus_resolved", "correct": correct, "team": team,
            "points": 2 if correct else 0, "timed_out": timed_out,
            "answer_index": option_index, "correct_index": self._reveal(state),
        })
        await self._advance(state)

    async def _log_question(self, state, answering_team, correct, points):
        state["match_log"].append({
            "index": state["current_index"],
            "question_id": state["questions"][state["current_index"]]["question_id"],
            "answering_team": answering_team,
            "correct": correct,
            "points": points,
            "points": points,
            "student_id": state.get("answering_student_id"),
        })

    async def _advance(self, state):
        """The question is over. Pause so everyone can see the result, then
        move on (see _advance_now)."""
        state["phase"] = "revealing"
        await self.state.save(state)
        await self._broadcast({"type": "next_question_in", **_timer_fields(REVEAL_PAUSE_SECONDS)})
        self._schedule_timeout(state["current_index"], REVEAL_PAUSE_SECONDS, "revealing")

    async def _advance_now(self, state):
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
        used = [q["question_id"] for q in state["questions"]]
        extra = await database_sync_to_async(select_match_questions)(
            state["team_a_players"] + state["team_b_players"], 1,
            levels=tuple(state.get("levels", ("100",))),
            departments=state.get("departments"), exclude_ids=used,
            course_codes=state.get("course_codes") or None,
        )
        if not extra:
            await self._finalize(state)
            return
        state["questions"].append(extra[0])
        await self.state.save(state)
        await self._broadcast({"type": "sudden_death"})
        await self._serve_question(state)

    async def _finalize(self, state, forced_winner=None):
        if forced_winner:
            winner = forced_winner
        elif state["team_a_score"] > state["team_b_score"]:
            winner = "A"
        elif state["team_b_score"] > state["team_a_score"]:
            winner = "B"
        else:
            winner = None

        await self._persist_match(state, winner)
        all_ids = state["team_a_players"] + state["team_b_players"]
        names = await database_sync_to_async(self._names_sync)(all_ids)
        results = self._build_results(state, names)
        await self._broadcast({
            "type": "match_finished", "team_a_score": state["team_a_score"],
            "team_b_score": state["team_b_score"], "winner": winner,
            "forfeit": forced_winner is not None,
            "results": results,
        })
        await self.state.clear()
        task = _timer_tasks.pop(self.room_code, None)
        if task and task is not asyncio.current_task():
            task.cancel()
        _room_locks.pop(self.room_code, None)

        progress = await self._persist_match(state, winner)
        all_ids = state["team_a_players"] + state["team_b_players"]
        names = await database_sync_to_async(self._names_sync)(all_ids)
        results = self._build_results(state, names)
        results["progress"] = {str(k): v for k, v in progress.items()}

    async def _persist_match(self, state, winner):
        return await database_sync_to_async(self._persist_match_sync)(state, winner)

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
                answering_student_id=entry.get("student_id"),
            )

        # Exposure tracking for every question actually served, seen or not.
        all_student_ids = state["team_a_players"] + state["team_b_players"]
        for q in state["questions"][: state["current_index"] + 1]:
            for sid in all_student_ids:
                exposure, _ = StudentQuestionExposure.objects.get_or_create(
                    student_id=sid, question_id=q["question_id"],
                )
                exposure.times_seen += 1
                exposure.last_seen_at = timezone.now()
                exposure.save(update_fields=["times_seen", "last_seen_at"])

        xp_map = self._award_xp(state, winner)
        progress = self._record_stats(match, state, winner, xp_map)
        room.status = "finished"
        room.save(update_fields=["status"])
        return progress

    def _lagos_day_start(self):
        now = timezone.now().astimezone(ZoneInfo("Africa/Lagos"))
        return now.replace(hour=0, minute=0, second=0, microsecond=0)

    def _award_xp(self, state, winner):
        xp_map = {}
        if winner is None:
            return xp_map
        winning_ids = state["team_a_players"] if winner == "A" else state["team_b_players"]
        losing_ids = state["team_b_players"] if winner == "A" else state["team_a_players"]
        day_start = self._lagos_day_start()

        for sid in winning_ids:
            my_wins_today = BattleMatchPlayer.objects.filter(
                student_id=sid, won=True, match__ended_at__gte=day_start)
            beat_them_today = BattleMatchPlayer.objects.filter(
                won=False, student_id__in=losing_ids,
                match__in=my_wins_today.values("match"),
            ).exists()
            xp = 20 if (my_wins_today.count() >= 2 or beat_them_today) else 200
            StudentProfile.objects.filter(id=sid).update(xp=F_xp(xp))
            xp_map[sid] = xp

        for sid in losing_ids:
            StudentProfile.objects.filter(id=sid).update(xp=F_xp(10))
            xp_map[sid] = 10
        return xp_map

    def _record_stats(self, match, state, winner, xp_map):
        progress = {}
        for team, ids in (("A", state["team_a_players"]), ("B", state["team_b_players"])):
            for sid in ids:
                won = (winner == team)
                xp = xp_map.get(sid, 0)
                BattleMatchPlayer.objects.create(match=match, student_id=sid, team=team, won=won, xp_awarded=xp)
                stats, _ = BattleStats.objects.get_or_create(student_id=sid)
                before = tier_for(stats.wins)
                if winner is None:
                    stats.draws += 1
                elif won:
                    stats.wins += 1
                    stats.current_streak += 1
                    stats.best_streak = max(stats.best_streak, stats.current_streak)
                else:
                    stats.losses += 1
                    stats.current_streak = 0
                stats.save()
                after = tier_for(stats.wins)
                nxt = next_tier(stats.wins)
                progress[sid] = {"xp": xp, "tier": after, "tier_up": after != before,
                                 "next": list(nxt) if nxt else None}
        return progress

    async def _broadcast(self, payload):
        await self.consumer.channel_layer.group_send(
            self.consumer.group_name, {"type": "room.event", "event": payload},
        )

    async def _pick_answerer(self, roster, start_turn):
        """Next connected player in rotation order. Returns (student_id, next_turn_counter)."""
        def _connected():
            return set(BattleParticipant.objects.filter(
                room__code=self.room_code, student_id__in=roster,
                connection_status="connected",
            ).values_list("student_id", flat=True))
        connected = await database_sync_to_async(_connected)()
        for step in range(len(roster)):
            sid = roster[(start_turn + step) % len(roster)]
            if sid in connected:
                return sid, start_turn + step + 1
        return None, start_turn + 1

    def _names_sync(self, ids):
        return {
            p.id: p.user.username
            for p in StudentProfile.objects.filter(id__in=ids).select_related("user")
        }

    def _build_results(self, state, names):
        team_a = set(state["team_a_players"])
        per = {}
        for e in state["match_log"]:
            sid = e.get("student_id")
            if sid is None:
                continue
            row = per.setdefault(sid, {"answered": 0, "correct": 0})
            row["answered"] += 1
            if e["correct"]:
                row["correct"] += 1

        players = sorted(
            (
                {
                    "student_id": sid,
                    "username": names.get(sid, "Player"),
                    "team": "A" if sid in team_a else "B",
                    **row,
                }
                for sid, row in per.items()
            ),
            key=lambda p: (-p["correct"], p["answered"]),
        )
        mvp = players[0]["student_id"] if players and players[0]["correct"] > 0 else None

        review = []
        for e in state["match_log"]:
            if e["correct"]:
                continue
            q = state["questions"][e["index"]]
            review.append({
                "number": e["index"] + 1,
                "stem": q["stem"],
                "options": q["options"],
                "correct_index": q["correct_index"],
                "team": e["answering_team"],
                "topic": q.get("topic", ""),
            })

        return {"players": players, "mvp": mvp, "review": review}
    
    def _reveal(self, state):
        q = state["questions"][state["current_index"]]
        return {"correct_index": q["correct_index"], "topic": q.get("topic", ""),
                "explanation": q.get("explanation", "")}


def F_xp(amount):
    from django.db.models import F
    return F("xp") + amount

