import logging
import asyncio
from django.core.cache import cache
import random
import string
from channels.generic.websocket import AsyncJsonWebsocketConsumer
from channels.db import database_sync_to_async

from core.models import StudentProfile
from .models import BattleRoom
from .match_loop import MatchLoop, FORFEIT_GRACE_SECONDS
from .match_state import MatchState
import time
from collections import deque

from django.utils import timezone
from focus_sprint.models import RoomMember
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.db.models import Count, Q
from .models import BattleRoom, BattleParticipant, BattleStats, BattleQuestion
from .levels import levels_for_room
from .tiers import tier_for

_background = set()
_READY = {}   # room code -> set(student_id); memory only, resets on restart

PRESENCE_KEY = "online_users"
PRESENCE_TTL = 75   # seconds without a ping before someone counts as gone


def _mark_online(user_id, name):
    now = time.time()
    data = cache.get(PRESENCE_KEY) or {}
    data = {k: v for k, v in data.items() if now - v[0] < PRESENCE_TTL}
    data[user_id] = (now, name)
    cache.set(PRESENCE_KEY, data, 600)

class UserNotifyConsumer(AsyncJsonWebsocketConsumer):
    """One personal connection per logged-in user, so the server can push
    things like new invites straight to their browser."""

    async def connect(self):
        self.group = None
        await self.accept()
        user = self.scope["user"]
        if not user.is_authenticated:
            await self.close(code=4001)
            return
        self.group = f"user_notify_{user.id}"
        await self.channel_layer.group_add(self.group, self.channel_name)
        await database_sync_to_async(_mark_online)(user.id, user.username)

    async def receive_json(self, content, **kwargs):
        user = self.scope["user"]
        if user.is_authenticated:      # each ping says "still here"
            await database_sync_to_async(_mark_online)(user.id, user.username)

    async def disconnect(self, close_code):
        if self.group:
            await self.channel_layer.group_discard(self.group, self.channel_name)

    async def user_event(self, event):
        await self.send_json(event["event"])


def push_to_user(user_id, payload):
    """Call from normal (sync) Django views. Never raises, so a Redis
    problem can't break the request that called it."""
    try:
        async_to_sync(get_channel_layer().group_send)(
            f"user_notify_{user_id}", {"type": "user.event", "event": payload}
        )
    except Exception:
        logger.exception("push_to_user failed for user %s", user_id)

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
            if room.status == "in_progress":      # reconnecting mid-match
                snap = await self._match_snapshot()
                if snap:
                    await self.send_json(snap)
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

    async def _close_if_host_gone(self):
        try:
            await asyncio.sleep(HOST_GRACE_SECONDS)
            closed = await self._close_waiting_room_if_host_gone()
            if closed:
                await self.channel_layer.group_send(self.group_name, {"type": "room.closed"})
        except Exception:
            logger.exception("Host-gone check failed in room %s", self.room_code)

    @database_sync_to_async
    def _close_waiting_room_if_host_gone(self):
        room = BattleRoom.objects.filter(code=self.room_code).first()
        if room is None or room.status != "waiting":
            return False
        host_back = BattleParticipant.objects.filter(
            room=room, student_id=room.leader_id, connection_status="connected",
        ).exists()
        if host_back:
            return False
        _retire_room(room)
        return True

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
            elif event_type == "set_ready":
                await self._handle_set_ready(content)
            elif event_type == "rematch":
                await self._handle_rematch()
            elif event_type == "set_course":
                await self._handle_set_course(content)
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
        ok, error = await self._validate_everyone_ready(room)
        if not ok:
            await self.send_json({"type": "error", "message": error})
            return

        await self._set_room_status(room, "in_progress")
        _READY.pop(self.room_code, None)

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

    async def _handle_set_ready(self, content):
        room = await self._get_room()
        if room is None or room.status != "waiting":
            return
        profile = await self._get_profile()
        ready = _READY.setdefault(self.room_code, set())
        if content.get("ready"):
            ready.add(profile.id)
        else:
            ready.discard(profile.id)
        await self._broadcast_room_state(room)

    async def _handle_rematch(self):
        room = await self._get_room()
        profile = await self._get_profile()
        if room is None:
            return
        if room.leader_id != profile.id:
            await self.send_json({"type": "error", "message": "Only the room leader can start a rematch."})
            return
        if room.status != "finished":
            return
        code = await self._create_rematch_room(room)
        await self.channel_layer.group_send(
            self.group_name,
            {"type": "room.event", "event": {"type": "rematch", "code": code}},
        )

    async def _handle_set_course(self, content):
        room = await self._get_room()
        profile = await self._get_profile()
        if room is None or room.status != "waiting":
            return
        if room.leader_id != profile.id:
            await self.send_json({"type": "error", "message": "Only the room leader can choose the course."})
            return
        code = str(content.get("course") or "").strip()[:10]
        if not await self._set_room_course(room, code):
            await self.send_json({"type": "error", "message": "That course isn't available."})
            return
        await self._broadcast_room_state(room)

    @database_sync_to_async
    def _set_room_course(self, room, code):
        if code and not BattleQuestion.objects.filter(course_code=code, status="approved").exists():
            return False
        room.course_code = code
        room.save(update_fields=["course_code"])
        return True

    @database_sync_to_async
    def _close_or_delete(self, room):
        _retire_room(room)    # keeps rooms with match history, deletes empty ones

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
        wins = dict(
            BattleStats.objects.filter(student_id__in=[p.student_id for p in participants])
            .values_list("student_id", "wins")
        )
        levels = levels_for_room([p.student.level for p in participants])
        depts = sorted({p.student.department for p in participants if p.student.department})
        qs = BattleQuestion.objects.filter(status="approved", question_type="static", level__in=levels)
        if depts:
            qs = qs.filter(Q(department="") | Q(department__in=depts))
        courses = [
            {"code": r["course_code"], "n": r["n"]}
            for r in qs.values("course_code").annotate(n=Count("id")).order_by("course_code")
        ]
        return {
            "type": "room_state",
            "room_code": room.code,
            "status": room.status,
            "mode": room.mode,
            "course": room.course_code,
            "courses": courses,
            "leader_id": room.leader_id,
            "ready": sorted(_READY.get(room.code, set())),
            "participants": [
                {
                    "student_id": p.student_id,
                    "username": p.student.user.username,
                    "team": p.team,
                    "role": p.role,
                    "connection_status": p.connection_status,
                    "tier": tier_for(wins.get(p.student_id, 0)),
                    "dept": p.student.department or "",
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
    @database_sync_to_async
    def _validate_everyone_ready(self, room):
        ready = _READY.get(self.room_code, set())
        players = BattleParticipant.objects.filter(
            room=room, role="player", connection_status="connected",
        ).values_list("student_id", flat=True)
        waiting = [pid for pid in players if pid != room.leader_id and pid not in ready]
        if waiting:
            n = len(waiting)
            return False, f"{n} player{'s' if n != 1 else ''} haven't tapped Ready yet."
        return True, None

    @database_sync_to_async
    def _create_rematch_room(self, old):
        key = f"rematch:{old.code}"
        existing = cache.get(key)           # double-click protection
        if existing:
            return existing
        code = None
        for _ in range(10):
            candidate = "".join(random.choices(string.ascii_uppercase + string.digits, k=6))
            if not BattleRoom.objects.filter(code=candidate).exists():
                code = candidate
                break
        new = BattleRoom.objects.create(code=code, leader_id=old.leader_id, is_public=old.is_public, course_code=old.course_code)
        for p in BattleParticipant.objects.filter(room=old):
            BattleParticipant.objects.create(
                room=new, student_id=p.student_id, team=p.team, role=p.role,
                connection_status="disconnected",
            )
        cache.set(key, code, 600)
        return code

    # ── Focus room chat: text and emojis only, memory only, nothing saved to the database ──

_FOCUS_CHAT_HISTORY = {}      # room code -> last messages (this server process only)
FOCUS_CHAT_MAX_CHARS = 500
FOCUS_CHAT_HISTORY_SIZE = 50


class FocusRoomConsumer(AsyncJsonWebsocketConsumer):
    async def connect(self):
        self.code = self.scope["url_route"]["kwargs"]["code"]
        self.group = f"focus_room_{self.code}"
        self.joined = False
        self.sent_at = []
        await self.accept()
        try:
            if not self.scope["user"].is_authenticated:
                await self.close(code=4001)
                return
            member = await self._member()
            if member is None:
                await self.close(code=4003)
                return
            self.student_id, self.username = member
            self.joined = True
            await self.channel_layer.group_add(self.group, self.channel_name)
            await self._touch()
            await self.send_json({"type": "history", "messages": list(_FOCUS_CHAT_HISTORY.get(self.code, []))})
        except Exception:
            logger.exception("Focus room connect failed in %s", self.code)
            await self.close(code=4500)

    async def disconnect(self, close_code):
        if getattr(self, "joined", False):
            await self.channel_layer.group_discard(self.group, self.channel_name)

    async def receive_json(self, content, **kwargs):
        if not self.joined:
            return
        kind = content.get("type")
        if kind == "ping":
            await self._touch()
            return
        if kind != "chat":
            return
        text = str(content.get("text", "")).strip()[:FOCUS_CHAT_MAX_CHARS]
        if not text:
            return
        now = time.monotonic()
        self.sent_at = [t for t in self.sent_at if now - t < 10]
        if len(self.sent_at) >= 5:
            await self.send_json({"type": "error", "message": "Slow down a little."})
            return
        self.sent_at.append(now)
        msg = {
            "type": "chat", "student_id": self.student_id, "username": self.username,
            "text": text, "ts": int(time.time() * 1000),
        }
        _FOCUS_CHAT_HISTORY.setdefault(self.code, deque(maxlen=FOCUS_CHAT_HISTORY_SIZE)).append(msg)
        await self.channel_layer.group_send(self.group, {"type": "room.event", "event": msg})

    async def room_event(self, event):
        await self.send_json(event["event"])

    async def room_kick(self, event):
        if event["student_id"] == getattr(self, "student_id", None):
            await self.send_json({"type": "kicked"})
            await self.close(code=4003)

    async def room_closed(self, event):
        _FOCUS_CHAT_HISTORY.pop(self.code, None)
        await self.send_json({"type": "room_closed"})
        await self.close(code=4010)

    @database_sync_to_async
    def _member(self):
        m = (
            RoomMember.objects.select_related("student__user")
            .filter(room__code=self.code, room__status="open",
                    student__user=self.scope["user"], left_at__isnull=True)
            .first()
        )
        if m is None:
            return None
        return m.student_id, (m.student.user.first_name or m.student.user.username)

    @database_sync_to_async
    def _touch(self):
        RoomMember.objects.filter(
            room__code=self.code, student_id=self.student_id, left_at__isnull=True,
        ).update(last_seen_at=timezone.now())