import logging
import asyncio

from channels.generic.websocket import AsyncJsonWebsocketConsumer
from channels.db import database_sync_to_async

from core.models import StudentProfile
from .models import BattleRoom, BattleParticipant
from .match_loop import MatchLoop, FORFEIT_GRACE_SECONDS
from .match_state import MatchState

_background = set()


def _spawn(coro):
    t = asyncio.create_task(coro)
    _background.add(t)
    t.add_done_callback(_background.discard)


logger = logging.getLogger(__name__)
MAX_PLAYERS_PER_TEAM = 5
MAX_SPECTATORS = 10
HOST_GRACE_SECONDS = 20


def _retire_room(room):
    """Rooms with match history are kept as 'finished'; empty ones are deleted."""
    if room.matches.exists():
        room.status = "finished"
        room.save(update_fields=["status"])
    else:
        room.delete()


class BattleConsumer(AsyncJsonWebsocketConsumer):
    """One connection per person in a room. Server->client is a broadcast to
    the room's Channels group; client->server is a named event dispatched
    in receive_json."""

    async def connect(self):
        self.room_code = self.scope["url_route"]["kwargs"]["room_code"]
        self.group_name = f"battle_room_{self.room_code}"
        self.student = self.scope["user"]
        self.joined = False

        await self.accept()   # finish the handshake before any slow DB work

        try:
            if not self.student.is_authenticated:
                logger.warning("WS reject %s: not authenticated", self.room_code)
                await self.close(code=4001)
                return

            room = await self._get_room()
            if room is None:
                await self.close(code=4004)
                return

            if room.status == "finished":
                await self.close(code=4005)
                return

            profile = await self._get_profile()
            if profile is None:
                await self.close(code=4001)
                return

            participant = await self._get_or_reconnect_participant(room, profile)
            if participant is None:
                await self.close(code=4003)
                return

            self.profile = profile
            self.joined = True
            await self.channel_layer.group_add(self.group_name, self.channel_name)
            await self._broadcast_room_state(room)
        except Exception:
            logger.exception("WS connect crashed in room %s", self.room_code)
            await self.close(code=4500)

    async def disconnect(self, close_code):
        if not getattr(self, "joined", False):
            return
        try:
            room = await self._get_room()
            if room is not None:
                await self._mark_disconnected(room)
                await self._broadcast_room_state(room)

                if room.status == "in_progress":
                    _spawn(self._forfeit_after_grace())   # scheduled first so a snapshot error can't skip it
                    snap = await self._match_snapshot()
                    if snap:
                        await self.send_json(snap)
                elif room.status == "waiting" and room.leader_id == self.profile.id:
                    _spawn(self._close_if_host_gone())
        except Exception:
            logger.exception("Disconnect cleanup failed in room %s", self.room_code)
        finally:
            await self.channel_layer.group_discard(self.group_name, self.channel_name)

    async def _forfeit_after_grace(self):
        try:
            await asyncio.sleep(FORFEIT_GRACE_SECONDS)
            await MatchLoop(self, self.room_code).forfeit_check()
        except Exception:
            logger.exception("Forfeit check failed in room %s", self.room_code)

    async def receive_json(self, content, **kwargs):
        event_type = content.get("type")
        try:
            if event_type == "assign_team":
                await self._handle_assign_team(content)
            elif event_type == "set_mode":
                await self._handle_set_mode(content)
            elif event_type == "start_match":
                await self._handle_start_match(content)
            elif event_type == "buzz":
                await self._handle_buzz(content)
            elif event_type == "submit_answer":
                await self._handle_submit_answer(content)
            elif event_type == "close_room":
                await self._handle_close_room()
            else:
                await self.send_json({"type": "error", "message": f"Unknown event type: {event_type}"})
        except Exception:
            logger.exception("Battle event %r failed in room %s", event_type, self.room_code)
            await self.send_json({"type": "error", "message": "Server error. Tell the host and check the logs."})

    # ── Event handlers ──────────────────────────────────────────────

    async def _handle_assign_team(self, content):
        room = await self._get_room()
        if room.status != "waiting":
            await self.send_json({"type": "error", "message": "The match has already started."})
            return
        profile = await self._get_profile()

        target_team = content.get("team")
        target_role = "spectator" if target_team is None else "player"

        ok, error = await self._set_participant_team(room, profile, target_team, target_role)
        if not ok:
            await self.send_json({"type": "error", "message": error})
            return

        await self._broadcast_room_state(room)

    async def _handle_set_mode(self, content):
        room = await self._get_room()
        if room.status != "waiting":
            await self.send_json({"type": "error", "message": "The match has already started."})
            return
        profile = await self._get_profile()

        if room.leader_id != profile.id:
            await self.send_json({"type": "error", "message": "Only the room leader can set the mode."})
            return

        mode = content.get("mode")
        if mode not in ("alternating", "buzzer"):
            await self.send_json({"type": "error", "message": "Invalid mode."})
            return

        await self._set_room_mode(room, mode)
        await self._broadcast_room_state(room)

    async def _handle_start_match(self, content):
        room = await self._get_room()
        profile = await self._get_profile()

        if room.leader_id != profile.id:
            await self.send_json({"type": "error", "message": "Only the room leader can start the match."})
            return

        if room.status != "waiting":
            return

        if room.mode is None:
            await self.send_json({"type": "error", "message": "Pick a mode before starting."})
            return

        ok, error = await self._validate_teams_ready(room)
        if not ok:
            await self.send_json({"type": "error", "message": error})
            return

        await self._set_room_status(room, "in_progress")
        match_loop = MatchLoop(self, self.room_code)
        await match_loop.start(room)

    async def _handle_buzz(self, content):
        profile = await self._get_profile()
        match_loop = MatchLoop(self, self.room_code)
        await match_loop.handle_buzz(profile.id)

    async def _handle_submit_answer(self, content):
        profile = await self._get_profile()
        match_loop = MatchLoop(self, self.room_code)
        await match_loop.handle_submit_answer(
            profile.id, content.get("option_index"), content.get("question_index"),
        )

    async def _handle_close_room(self):
        room = await self._get_room()
        profile = await self._get_profile()
        if room is None:
            return
        if room.leader_id != profile.id:
            await self.send_json({"type": "error", "message": "Only the room leader can close the room."})
            return
        if room.status == "in_progress":
            await self.send_json({"type": "error", "message": "You can't close a room during a match."})
            return
        await self.channel_layer.group_send(self.group_name, {"type": "room.closed"})
        await self._close_or_delete(room)

    @database_sync_to_async
    def _close_or_delete(self, room):
        if room.battlematch_set.exists():      # keep history; just retire the room
            room.status = "finished"
            room.save(update_fields=["status"])
        else:
            room.delete()
    # ── Broadcast relays (called by group_send) ─────────────────────

    async def room_event(self, event):
        await self.send_json(event["event"])

    async def room_closed(self, event):
        await self.send_json({"type": "room_closed"})
        await self.close(code=4010)

    async def _broadcast_room_state(self, room):
        state = await self._get_room_state_snapshot(room)
        await self.channel_layer.group_send(
            self.group_name, {"type": "room.event", "event": state}
        )

    # ── DB helpers ───────────────────────────────────────────────────

    @database_sync_to_async
    def _get_room(self):
        return BattleRoom.objects.filter(code=self.room_code).first()

    @database_sync_to_async
    def _get_profile(self):
        return StudentProfile.objects.filter(user=self.student).select_related("user").first()

    @database_sync_to_async
    def _get_or_reconnect_participant(self, room, profile):
        existing = BattleParticipant.objects.filter(room=room, student=profile).first()
        if existing:
            existing.connection_status = "connected"
            existing.save(update_fields=["connection_status"])
            return existing
        if room.status == "finished":
            return None
        specs = BattleParticipant.objects.filter(room=room, role="spectator").count()
        total = BattleParticipant.objects.filter(room=room).count()
        if room.status == "in_progress":
            if specs >= MAX_SPECTATORS:
                return None
        elif total >= MAX_PLAYERS_PER_TEAM * 2 + MAX_SPECTATORS:
            return None
        return BattleParticipant.objects.create(
            room=room, student=profile, role="spectator", connection_status="connected",
        )

    @database_sync_to_async
    def _mark_disconnected(self, room):
        BattleParticipant.objects.filter(room=room, student__user=self.student).update(
            connection_status="disconnected"
        )

    @database_sync_to_async
    def _set_participant_team(self, room, profile, target_team, target_role):
        if target_role == "player":
            if target_team not in ("A", "B"):
                return False, "Invalid team."
            current_count = BattleParticipant.objects.filter(
                room=room, team=target_team, role="player"
            ).exclude(student=profile).count()
            if current_count >= MAX_PLAYERS_PER_TEAM:
                return False, f"Team {target_team} is full."
        else:
            spectator_count = BattleParticipant.objects.filter(
                room=room, role="spectator"
            ).exclude(student=profile).count()
            if spectator_count >= MAX_SPECTATORS:
                return False, "Spectator slots are full."

        BattleParticipant.objects.filter(room=room, student=profile).update(
            team=target_team, role=target_role,
        )
        return True, None

    @database_sync_to_async
    def _set_room_mode(self, room, mode):
        room.mode = mode
        room.save(update_fields=["mode"])

    @database_sync_to_async
    def _set_room_status(self, room, status):
        room.status = status
        room.save(update_fields=["status"])

    @database_sync_to_async
    def _validate_teams_ready(self, room):
        team_a = BattleParticipant.objects.filter(room=room, team="A", role="player").count()
        team_b = BattleParticipant.objects.filter(room=room, team="B", role="player").count()
        if team_a == 0 or team_b == 0:
            return False, "Both teams need at least one player."
        return True, None

    @database_sync_to_async
    def _get_room_state_snapshot(self, room):
        participants = list(
            BattleParticipant.objects.filter(room=room).select_related("student__user")
        )
        return {
            "type": "room_state",
            "room_code": room.code,
            "status": room.status,
            "mode": room.mode,
            "leader_id": room.leader_id,
            "participants": [
                {
                    "student_id": p.student_id,
                    "username": p.student.user.username,
                    "team": p.team,
                    "role": p.role,
                    "connection_status": p.connection_status,
                }
                for p in participants
            ],
        }

    @database_sync_to_async
    def _delete_room(self, room):
        BattleRoom.objects.filter(pk=room.pk).delete()

    async def _match_snapshot(self):
        state = await MatchState(self.room_code).get()
        if not state or state["phase"] == "intro":
            return None
        q = state["questions"][state["current_index"]]
        return {
            "type": "match_sync", "index": state["current_index"],
            "stem": q["stem"], "options": q["options"], "mode": state["mode"],
            "phase": state["phase"],
            "team_a_score": state["team_a_score"], "team_b_score": state["team_b_score"],
            "answering_student_id": state.get("answering_student_id"),
            "bonus_team": state.get("bonus_team"),
            # only revealed once the question is over, so spectators can't leak answers
            "correct_index": q["correct_index"] if state["phase"] == "revealing" else None,
        }