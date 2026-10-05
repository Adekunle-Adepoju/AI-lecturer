import os
import json
import profile
from urllib import request
import markdown
import random
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
import re
import math
import time

from django.contrib import messages
from django.contrib.auth import login, logout, update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import AuthenticationForm, PasswordChangeForm, SetPasswordForm
from .account_forms import EmailOrUsernameAuthenticationForm, AccountDetailsForm
from django.conf import settings
from django.shortcuts import render, redirect, get_object_or_404
from django.utils import timezone
from django.views.decorators.http import require_POST
from google import genai
from google.genai import types
from django.http import StreamingHttpResponse
from django.http import JsonResponse, Http404
from django.db.models import F, Count
from django.db import transaction
from django_q.tasks import async_task
from .simulator_draw import draw_test, NotEnoughQuestions, TEST_MODES
from .simulator_bank import get_topic_availability, process_topic
from .simulator_bank_prompts import SIMULATOR_STRICT_GRADING_PROMPT
from django_q.models import Task, OrmQ


from .forms import SignupForm, OnboardingForm, ProfileEditForm, ElectiveSelectionForm
from .models import (
    StudentProfile, TimetableEntry, Session, TopicSession, ChatMessage,
    SlideDocument, CourseOutline, CourseDefinition,
    PastQuestion, SimulatorTest, PreGeneratedLesson, SlideTopicChunk, SimulatorQuestion, SimulatorBankTopic,
)
from .outline_generation import get_generation_source, NO_SOURCE_MESSAGE
from .prompt import (
    SYSTEM_PROMPT, CHAT_SYSTEM_PROMPT, QUIZ_GENERATION_PROMPT,
    LECTURE_PROMPT, LECTURE_VERIFIER_PROMPT, LECTURE_QUIZ_PROMPT
)
from functools import wraps
from .staff_forms import SlideUploadForm, CourseOutlineUploadForm, PastQuestionUploadForm, CourseDefinitionForm
from .prompt import (
    SIMULATOR_QUESTION_PROMPT, SIMULATOR_GRADING_PROMPT, SIMULATOR_OVERALL_FEEDBACK_PROMPT,
    SIMULATOR_QUESTION_PROMPT_MULTI_TOPIC, SIMULATOR_QUESTION_PROMPT_MULTI_TOPIC_MCQ,
    TOPIC_BLOCK_TEMPLATE,
)
from .slide_topic_extractor import safe_extract_topics_from_slide, is_reference_table_content
from .lecture_completeness import detect_likely_duplicate_reteach, is_lecture_truncated, find_safe_cutoff, continue_truncated_lecture, MAX_CONTINUATION_ATTEMPTS
from .staff_forms import BattleQuestionGenerationForm
from battle.generation import run_battle_question_generation
from battle.models import BattleQuestionGenerationChunk, BattleQuestion
from django.urls import reverse


client = genai.Client(api_key=settings.GEMINI_API_KEY_CHAT)
simulator_client = genai.Client(api_key=settings.GEMINI_API_KEY_SIMULATOR)
extraction_client = genai.Client(api_key=settings.GEMINI_API_KEY_EXTRACTION)
generation_client = genai.Client(api_key=settings.GEMINI_API_KEY_GENERATION)
CITATION_HEURISTIC_RE = re.compile(
    r"\b(edition|glossary|et al\.?|isbn|vol\.|published|textbook)\b", re.IGNORECASE
)
MAX_TOPIC_CONTEXT_CHARS = 24000

_VISUAL_FENCE_RE = re.compile(r"```(?:svg|mermaid|json_chart)[^\n]*\n.*?```", re.DOTALL)

def _strip_visual_blocks(text):
    return _VISUAL_FENCE_RE.sub("[diagram]", text or "")

def _own_topic_session(request, topic_session_id):
    """The topic session if it belongs to the logged-in student, else 404."""
    try:
        pk = int(topic_session_id)
    except (TypeError, ValueError):
        raise Http404
    return get_object_or_404(TopicSession, pk=pk, session__student__user=request.user)

def _build_history(topic_session):
    """Convert saved ChatMessages into Gemini content format."""
    history = []
    messages = list(topic_session.chatmessage_set.order_by("created_at"))
    for msg in messages[:-1]:  # exclude most recent — sent separately
        role = "user" if msg.role == "user" else "model"
        history.append({"role": role, "parts": [{"text": msg.content}]})
    return history

def _check_daily_message_cap(topic_session, cap=10):
    """Returns True if the student has hit their daily message cap."""
    student = topic_session.session.student
    today = date.today()
    count = ChatMessage.objects.filter(
        topic_session__session__student=student,
        role="user",
        created_at__date=today,
    ).count()
    return count >= cap

@login_required
@require_POST
def chat_message_view(request):
    topic_session_id = request.POST.get("topic_session_id")
    user_message = request.POST.get("message")
    is_retry = request.POST.get("retry") == "true"
    topic_session = _own_topic_session(request, topic_session_id)
    is_start_trigger = (user_message == "__START__")

        # ── Daily message cap ─────────────────────────────────────────────────────
    # Cap only applies to real student messages, not start trigger or retries —
    # and never applies to staff/superuser accounts.
    if not is_start_trigger and not is_retry and not _bypasses_restrictions(request):
        if _check_daily_message_cap(topic_session):
            return JsonResponse({
                "error": "cap_reached",
                "message": (
                    "You've reached your 10 message limit for today. "
                    "Come back tomorrow to continue — your progress is saved. 🙏"
                )
            }, status=429)

    if is_retry:
        last_msg = topic_session.chatmessage_set.order_by("-created_at").first()
        if last_msg and last_msg.role == "user":
            user_message = last_msg.content
        else:
            is_retry = False

    if not is_start_trigger and not is_retry:
        ChatMessage.objects.create(
            topic_session=topic_session, role="user", content=user_message
        )

    # ── Message count for remaining display ───────────────────────────────────
    student = topic_session.session.student
    today_count = ChatMessage.objects.filter(
        topic_session__session__student=student,
        role="user",
        created_at__date=date.today(),
    ).count()
    remaining = max(0, 10 - today_count)

    # ── Check for pre-generated content on __START__ ──────────────────────────
    if is_start_trigger and topic_session.lecture_content:
        stored_content = topic_session.lecture_content
        pages = _split_into_pages(stored_content)

        def pregenerated_stream():
            ChatMessage.objects.create(
                topic_session=topic_session,
                role="ai",
                content=stored_content,
                image_url=None,
                is_pregenerated=True,
            )
            import json as _json
            yield f"data: {_json.dumps({'pages': pages, 'is_pregenerated': True})}\n\n"
            yield f"data: {_json.dumps({'done': True, 'topic_complete': False, 'image_url': None, 'remaining_messages': remaining})}\n\n"

        resp = StreamingHttpResponse(pregenerated_stream(), content_type="text/event-stream")
        resp["Cache-Control"] = "no-cache"
        resp["X-Accel-Buffering"] = "no"
        return resp

        # ── Live chat has been retired — every lecture must be pregenerated ───────
    def no_lecture_stream():
        yield f"data: {json.dumps({'error': 'no_lecture', 'message': 'No lecture is available for this course yet. Please check back soon.'})}\n\n"

    resp = StreamingHttpResponse(no_lecture_stream(), content_type="text/event-stream")
    resp["Cache-Control"] = "no-cache"
    resp["X-Accel-Buffering"] = "no"
    return resp

# ─── Auth views ────────────────────────────────────────────────────────────────

def signup_view(request):
    if request.user.is_authenticated:
        return redirect("dashboard")
    if request.method == "POST":
        form = SignupForm(request.POST)
        if form.is_valid():
            try:
                user = form.save(commit=False)
                user.first_name = form.cleaned_data["first_name"]
                user.last_name = form.cleaned_data["last_name"]
                user.save()

                profile = StudentProfile.objects.create(
                    user=user,
                    matric_number=form.cleaned_data["matric_number"],
                    school=form.cleaned_data["school"],
                    department=form.cleaned_data["department"],
                    level=form.cleaned_data["level"],
                    semester=form.cleaned_data["semester"],
                )
                login(request, user, backend="django.contrib.auth.backends.ModelBackend")
                return redirect("elective_selection")
            except Exception as e:
                form.add_error(None, f"Error creating account: {str(e)}")
    else:
        form = SignupForm()
    return render(request, "core/signup.html", {"form": form})

@login_required
def elective_selection_view(request):
    if not hasattr(request.user, "profile"):
        return redirect("onboarding")

    profile = request.user.profile

    # Show ALL courses in the student's department/school, across every
    # level — carry-over students may need to pick up a course from an
    # earlier level alongside their current ones.
    available_courses = CourseDefinition.objects.filter(
        school=profile.school,
        department=profile.department,
    ).order_by("level", "semester", "course_code")

    if not available_courses.exists():
        _generate_timetable(profile)
        return redirect("dashboard")

    # Pre-check the student's own compulsory courses at their level/semester
    # so a normal (non-carryover) student just hits Continue.
    default_checked_codes = set(
        available_courses.filter(
            level=profile.level, semester=profile.semester, is_elective=False,
        ).values_list("course_code", flat=True)
    )

    if request.method == "POST":
        selected_codes = set(request.POST.getlist("selected_courses"))
        entries = []
        for course in available_courses.filter(course_code__in=selected_codes):
            entries.append(TimetableEntry(
                student=profile,
                course_code=course.course_code,
                course_title=course.course_title,
                day="Wed",
                time="12:00",
                week_number=1,
                total_weeks=10,
            ))
        TimetableEntry.objects.bulk_create(entries)
        return redirect("dashboard")

    courses_by_level = {}
    for course in available_courses:
        courses_by_level.setdefault(course.level, []).append(course)

    return render(request, "core/elective_selection.html", {
        "profile": profile,
        "courses_by_level": courses_by_level,
        "default_checked_codes": default_checked_codes,
    })


def login_view(request):
    if request.user.is_authenticated:
        return redirect("dashboard")
    if request.method == "POST":
        form = EmailOrUsernameAuthenticationForm(data=request.POST)
        if form.is_valid():
            user = form.get_user()
            login(request, user, backend="django.contrib.auth.backends.ModelBackend")
            return redirect("dashboard")
    else:
        form = EmailOrUsernameAuthenticationForm()
    return render(request, "core/login.html", {"form": form})


def logout_view(request):
    logout(request)
    return redirect("login")


# ─── Onboarding ────────────────────────────────────────────────────────────────

@login_required
def onboarding_view(request):
    if hasattr(request.user, "profile"):
        return redirect("dashboard")
    if request.method == "POST":
        form = OnboardingForm(request.POST)
        if form.is_valid():
            profile = form.save(commit=False)
            profile.user = request.user
            profile.save()
            _generate_timetable(profile)
            return redirect("dashboard")
    else:
        form = OnboardingForm()
    return render(request, "core/onboarding.html", {"form": form})


# ─── Timetable generator ───────────────────────────────────────────────────────

def _generate_timetable(profile):
    """Generate timetable from CourseDefinition — compulsory + chosen electives"""
    days = ["Mon", "Tue", "Wed", "Thu", "Fri"]
    TimetableEntry.objects.filter(student=profile).delete()

    # Get compulsory courses
    compulsory = list(CourseDefinition.objects.filter(
        level=profile.level,
        semester=profile.semester,
        school=profile.school,
        department=profile.department,
        is_elective=False,
    ).order_by("course_code"))

    # Get chosen electives
    electives = list(profile.elective_courses.filter(
        level=profile.level,
        semester=profile.semester,
    ).order_by("course_code"))

    all_courses = compulsory + electives

    entries = []
    for i, course in enumerate(all_courses):
        day = days[i % len(days)]
        time = "09:00" if i < len(days) else "11:00"
        entries.append(TimetableEntry(
            student=profile,
            course_code=course.course_code,
            course_title=course.course_title,
            day=day,
            time=time,
            week_number=1,
            total_weeks=10,
        ))
    TimetableEntry.objects.bulk_create(entries)

def _generate_course_schedule_preview(profile, num_days=14):
    entries = _ensure_study_days(profile)
    today = _today_lagos()
    out = []
    for i in range(num_days):
        d = today + timedelta(days=i)
        courses = _courses_on(entries, d)
        out.append({"date": d, "courses": courses, "is_rest": not courses})
    return out


# ─── Dashboard ─────────────────────────────────────────────────────────────────

@login_required
def dashboard_view(request):
    if not hasattr(request.user, "profile"):
        return redirect("onboarding")

    profile = request.user.profile

        # Redirect to SIWES if 400L Sem 2
    if profile.level == "400" and profile.semester == "2":
        return redirect("siwes")

    timetable = profile.timetable.all()

    # Build "Your courses" from actual enrollment (TimetableEntry), not the
    # hardcoded COURSES dict — so Manage Courses changes actually reflect here.
    course_defs = {
        c.course_code: c
        for c in CourseDefinition.objects.filter(
            course_code__in=timetable.values_list("course_code", flat=True)
        )
    }
    courses = [
        {
            "code": t.course_code,
            "title": t.course_title,
            "units": course_defs[t.course_code].units if t.course_code in course_defs else 3,
            "progress_pct": _get_course_progress_pct(profile, t.course_code),
        }
        for t in timetable
    ]
    recent_sessions = profile.sessions.all()[:5]
    leaderboard = StudentProfile.objects.select_related("user").order_by("-xp")[:10]
    sessions_done = profile.sessions.count()

    today = _today_lagos()
    entries = _ensure_study_days(profile)
    scheduled_today = _courses_on(entries, today)
    done_today = _done_today_codes(profile, today)
    todays_courses = [
        e for e in scheduled_today
        if not e.is_completed and e.course_code not in done_today
        and e.week_number != TEST_WEEK
    ]
    other_courses = [e for e in entries if e not in todays_courses and not e.is_completed]
    pending_tests, test_heads_up = _test_context(profile, entries)
    is_rest_day = not scheduled_today

    incomplete_sessions = {
        s.course_code: s
        for s in Session.objects.filter(student=profile, is_complete=False)
    }

    # Check if all courses are completed — show end of year message
    total_courses = timetable.count()
    completed_courses = timetable.filter(is_completed=True).count()
    all_done = total_courses > 0 and completed_courses == total_courses

    return render(request, "core/dashboard.html", {
        "profile": profile,
        "courses": courses,
        "timetable": timetable,
        "recent_sessions": recent_sessions,
        "leaderboard": leaderboard,
        "sessions_done": sessions_done,
        "todays_courses": todays_courses,
        "today": today,
        "incomplete_sessions": incomplete_sessions,
        "all_done": all_done,
        "is_rest_day": is_rest_day,
        "other_courses": other_courses,
        "pending_tests": pending_tests,
        "test_heads_up": test_heads_up,
    })


# ─── Helpers ───────────────────--------------------------------───────────────
# Fixed Monday anchor for the rolling course queue. Every student's
# rotation is computed purely from today's date relative to this epoch —
# never from individual login/signup history — so students with the same
# course list always land on the same course on the same calendar day.
LAGOS = ZoneInfo("Africa/Lagos")
TEST_WEEK = 7
DEFAULT_STUDY_WEEKDAYS = [0, 1, 2, 3, 4, 5]   # Mon–Sat; Sunday stays free


def _today_lagos():
    return datetime.now(LAGOS).date()


def _ensure_study_days(profile):
    """Gives every course with no schedule yet the lightest Mon–Sat day.
    Courses the student already edited are never touched."""
    entries = list(profile.timetable.order_by("course_code"))
    pending = [e for e in entries if e.study_days is None]
    if not pending:
        return entries
    load = {d: 0 for d in DEFAULT_STUDY_WEEKDAYS}
    for e in entries:
        for d in (e.study_days or []):
            if d in load:
                load[d] += 1
    for e in pending:
        d = min(DEFAULT_STUDY_WEEKDAYS, key=lambda x: (load[x], x))
        e.study_days = [d]
        load[d] += 1
        e.save(update_fields=["study_days"])
    return entries


def _courses_on(entries, d):
    return [e for e in entries if d.weekday() in (e.study_days or [])]


def _done_today_codes(profile, today):
    start = datetime.combine(today, datetime.min.time(), tzinfo=LAGOS)
    return set(
        Session.objects.filter(
            student=profile, is_complete=True,
            completed_at__gte=start, completed_at__lt=start + timedelta(days=1),
        ).values_list("course_code", flat=True)
    )


def _streak_continues(profile, last, today):
    """Streak survives a gap only if no course was scheduled on the skipped days."""
    gap = (today - last).days
    if gap <= 1:
        return True
    if gap > 14:
        return False
    entries = _ensure_study_days(profile)
    return not any(_courses_on(entries, last + timedelta(days=i)) for i in range(1, gap))

# ─── Test week: every course holds its own test when it reaches week 7 ───────

HEADS_UP_WEEKS = (4, 5, 6)


def _ready_test_topics(course_code, level):
    """Topics from weeks 1-6 that have approved questions."""
    ready = {a["topic"] for a in get_topic_availability(course_code, level) if a["ready"]}
    return [
        t for r in range(1, TEST_WEEK)
        for t in _get_topics_for_week(course_code, level, r) if t in ready
    ]


def _test_context(profile, entries):
    """pending: courses sitting at week 7 (test waiting).
    heads_up: courses on weeks 4-6 (test coming)."""
    pending = []
    for e in entries:
        if e.week_number == TEST_WEEK and not e.is_completed:
            level = _course_level(e.course_code, profile)
            pending.append({
                "entry": e,
                "ready": bool(_ready_test_topics(e.course_code, level)),
                "in_progress": SimulatorTest.objects.filter(
                    student=profile, course_code=e.course_code, mode="auto",
                    week_number=TEST_WEEK, status="in_progress",
                ).exists(),
            })
    heads_up = [
        {"entry": e, "lessons_left": TEST_WEEK - e.week_number}
        for e in entries
        if e.week_number in HEADS_UP_WEEKS and not e.is_completed
    ]
    return pending, heads_up

TOTAL_TEACHING_WEEKS = 15  # fixed course length in teaching rounds


def _get_total_topics_for_course(course_code, level):
    """Full ordered topic list for a course. Prefers whichever source
    list can actually be backed by real slide content: any candidate
    topic (from CourseOutline OR SlideDocument.extracted_topics) with no
    corresponding non-empty SlideTopicChunk is dropped — teaching a topic
    name with nothing behind it is exactly the failure mode where the
    model invents content with no fidelity anchor. If no slide exists at
    all, the outline (or hardcoded COURSE_OUTLINES fallback) is trusted
    as-is, since there's nothing to cross-check it against."""

    def _filter_to_backed_topics(topics, slide):
        backed_names = set(
            slide.topic_chunks.filter(is_empty=False)
            .values_list("topic_name", flat=True)
        )
        return [t for t in topics if t in backed_names]

    slide = None
    try:
        slide = SlideDocument.objects.get(course_code=course_code, level=level, parsed=True)
    except SlideDocument.DoesNotExist:
        pass

    try:
        outline = CourseOutline.objects.get(course_code=course_code, level=level, parsed=True)
        topics = (outline.topics_json or {}).get("topics")
        if topics:
            if slide is not None:
                backed = _filter_to_backed_topics(topics, slide)
                if backed:
                    return backed
                # Outline doesn't match any real slide chunk at all — fall
                # through to the slide's own extracted_topics instead of
                # returning a list with zero backing.
            else:
                return list(topics)
    except (CourseOutline.DoesNotExist, ValueError):
        pass

    if slide is not None and slide.extracted_topics:
        backed = _filter_to_backed_topics(slide.extracted_topics, slide)
        if backed:
            return backed
        # Slide exists but topic-split hasn't run yet — return unfiltered
        # so the existing empty-chunk fallback in _find_slide_content_for_topic
        # still catches it downstream, same as today.
        return list(slide.extracted_topics)


    return []

def _get_course_progress_pct(profile, course_code):
    """Percent of the course's total topic list the student has completed,
    based on real TopicSession completion — not session count or week
    number, so it reflects actual topics finished vs the full syllabus."""
    level = _course_level(course_code, profile)
    total_topics = len(_get_total_topics_for_course(course_code, level))
    if not total_topics:
        return 0
    completed = TopicSession.objects.filter(
        session__student=profile,
        session__course_code=course_code,
        is_complete=True,
    ).count()
    return min(100, round((completed / total_topics) * 100))


TEACHING_ROUNDS = TOTAL_TEACHING_WEEKS - 1   # week 7 is the test, not a lecture


def _week_to_round_index(week):
    """0-based teaching-round index, or None for the test week / out of range."""
    if week == TEST_WEEK or week < 1 or week > TOTAL_TEACHING_WEEKS:
        return None
    return week - 1 if week < TEST_WEEK else week - 2


def _topic_round_sizes(total_topics, total_rounds=TEACHING_ROUNDS):
    if total_topics <= 0:
        return [0] * total_rounds
    base, remainder = divmod(total_topics, total_rounds)
    return [base + 1 if i < remainder else base for i in range(total_rounds)]


def _get_topics_for_week(course_code, level, week_number):
    idx = _week_to_round_index(week_number)
    if idx is None:
        return []
    all_topics = _get_total_topics_for_course(course_code, level)
    sizes = _topic_round_sizes(len(all_topics))
    start = sum(sizes[:idx])
    size = sizes[idx]
    return all_topics[start:start + size] if size else []


def _find_slide_content_for_topic(course_code, level, topic_name):
    try:
        slide = SlideDocument.objects.get(course_code=course_code, level=level, parsed=True)
    except SlideDocument.DoesNotExist:
        return ""

    topic_chunk = slide.topic_chunks.filter(topic_name=topic_name).exclude(is_empty=True).first()
    if topic_chunk and topic_chunk.chunk_text.strip():
        text = topic_chunk.chunk_text
        if len(text) > MAX_TOPIC_CONTEXT_CHARS:
            cut = text.rfind("\n\n--- ", 0, MAX_TOPIC_CONTEXT_CHARS)
            text = text[:cut] if cut > 0 else text[:MAX_TOPIC_CONTEXT_CHARS]
        return text

    # Last-resort keyword fallback only.
    text = slide.extracted_text
    if not text:
        return ""
    keywords = re.findall(r"[A-Za-z]{4,}", topic_name)
    lower_text = text.lower()
    for kw in keywords:
        idx = lower_text.find(kw.lower())
        if idx != -1:
            start = max(0, idx - 500)
            return text[start:start + 3000]
    return ""


def _get_past_questions_for_topic(course_code, level, topic_name, limit=3):
    """Fetch past questions relevant to this topic, for blending into quizzes"""
    from .models import PastQuestion
    relevant = []
    past_qs = PastQuestion.objects.filter(course_code=course_code, level=level, parsed=True)
    
    for pq in past_qs:
        for q in (pq.parsed_questions or []):
            hint = (q.get("topic_hint") or "").lower()
            if any(word.lower() in hint for word in topic_name.split()):
                relevant.append(q)
                
    if not relevant:
        all_questions = []
        for pq in past_qs:
            all_questions.extend(pq.parsed_questions or [])
        relevant = all_questions
        
    random.shuffle(relevant)
    return relevant[:limit]


def _generate_topic_lecture(course_code, course_title, topic_name, week, level, student_name, topic_index=0, slide_text="", total_topics_in_round=3):
    past_questions = _get_past_questions_for_topic(course_code, level, topic_name, limit=2)
    past_q_text = ""
    if past_questions:
        past_q_text = "\n\nREFERENCE PAST QUESTIONS (use similar style/difficulty for your quiz, but don't copy verbatim):\n"
        for pq in past_questions:
            past_q_text += f"- {pq.get('question', '')}\n"

    course_opening = ""
    if week == 1 and topic_index == 0:
        course_opening = (
            "COURSE OPENING: This is the very first lesson of the entire course. "
            "Open with a one- or two-sentence welcome to the course by title, say what this "
            "first topic covers, and assume the student knows nothing yet: define every term "
            "before you use it.\n"
        )

    user_message = (
        f"Student name: {student_name}\n"
        f"Level: {level}L\n"
        f"Course: {course_code} — {course_title}\n"
        f"Topic to teach: {topic_name}\n"
        f"Topic number: {topic_index + 1} of {total_topics_in_round} in this session\n"
        f"Week: {week} of {TOTAL_TEACHING_WEEKS}\n"
        f"STRICT INSTRUCTION: Teach ONLY '{topic_name}'. Do not teach any other topic. "
        f"Follow the course outline strictly. This is the exact topic scheduled for this session."
        f"{course_opening}"
        f"{slide_text}"
        f"{past_q_text}"
    )

    last_error = None
    max_retries = 3
    for attempt in range(max_retries):
        try:
            response = generation_client.models.generate_content(
                model="gemini-3.6-flash",
                contents=user_message,
                config=types.GenerateContentConfig(
                    system_instruction=SYSTEM_PROMPT,
                    max_output_tokens=8000,
                ),
            )
            text = response.text
            if not text or not text.strip():
                raise ValueError("Empty response from generation client")
            return text
        except Exception as e:
            last_error = e
            error_str = str(e)
            is_rate_limit = "429" in error_str or "RESOURCE_EXHAUSTED" in error_str

            if is_rate_limit:
                print(f"[{course_code}] Lecture gen: 429 hit (attempt {attempt + 1}/{max_retries}) — sleeping 20s and retrying same topic...")
                time.sleep(20)
                continue
            else:
                is_transient = any(marker in error_str for marker in [
                    "503", "UNAVAILABLE", "timeout", "Timeout",
                    "ConnectionError", "RemoteDisconnected",
                ])
                if is_transient and attempt < max_retries - 1:
                    print(f"[{course_code}] Lecture gen: transient error (attempt {attempt + 1}/{max_retries}) — retrying in 10s...")
                    time.sleep(10)
                    continue
                print(f"[{course_code}] Lecture gen failed: {e}")
                break

    raise RuntimeError(f"Lecture generation failed for {course_code} — {topic_name}: {last_error}")

def _parse_lecture(full_text):
    import re

    if "---INTRO---" in full_text and "---LECTURE---" in full_text:
        intro_raw = full_text.split("---INTRO---")[1].split("---LECTURE---")[0].strip()
        lecture_and_quiz = full_text.split("---LECTURE---")[1].strip()
    else:
        intro_raw = full_text[:500].strip()
        lecture_and_quiz = full_text

    if "---QUIZ---" in lecture_and_quiz:
        lecture_raw = lecture_and_quiz.split("---QUIZ---")[0].strip()
        quiz_raw = lecture_and_quiz.split("---QUIZ---")[1].strip()
    else:
        lecture_raw = lecture_and_quiz.strip()
        quiz_raw = ""

        question = ""
    options = ["Option A", "Option B", "Option C", "Option D"]
    correct_index = 0
    explanation = ""

    if quiz_raw:
        clean = quiz_raw.replace("```json", "").replace("```", "").strip()
        clean = clean.replace("\\*", "*").replace("\\%", "%")

        # Handle BOTH accepted shapes: a single {...} object (per prompt
        # spec) OR a [...] array of question objects (observed drift —
        # the model sometimes produces a mini-quiz array instead). Detect
        # which one we actually got before trying to extract a substring.
        first_brace = clean.find("{")
        first_bracket = clean.find("[")
        is_array = (
            first_bracket != -1
            and (first_brace == -1 or first_bracket < first_brace)
        )

        parsed_obj = None
        try:
            if is_array:
                start = clean.find("[")
                end = clean.rfind("]")
                if start != -1 and end != -1 and end > start:
                    clean_array = clean[start:end + 1]
                    quiz_array = json.loads(clean_array)
                    if isinstance(quiz_array, list) and quiz_array:
                        parsed_obj = quiz_array[0]  # use only the first question
            else:
                start = clean.find("{")
                end = clean.rfind("}")
                if start != -1 and end != -1 and end > start:
                    clean_obj = clean[start:end + 1]
                    parsed_obj = json.loads(clean_obj)
        except json.JSONDecodeError:
            parsed_obj = None

        if parsed_obj:
            question = parsed_obj.get("question", "")
            options = parsed_obj.get("options", options)
            # Accept "correct_index" (spec) or "answer"/"correct_answer"
            # (observed drift) so a renamed key doesn't silently default
            # to 0 and mark the wrong option correct.
            correct_index = (
                parsed_obj.get("correct_index")
                if parsed_obj.get("correct_index") is not None
                else parsed_obj.get("answer")
                if parsed_obj.get("answer") is not None
                else parsed_obj.get("correct_answer", 0)
            )
            try:
                correct_index = int(correct_index)
            except (TypeError, ValueError):
                correct_index = 0
            explanation = parsed_obj.get("explanation", "")
        else:
            # Last-resort regex fallback — only reached if neither a
            # single object nor an array parsed as valid JSON at all.
            q_match = re.search(r'"question"\s*:\s*"(.*?)"\s*,\s*"options"', clean, re.DOTALL)
            if q_match:
                question = q_match.group(1).strip()

            opt_match = re.search(r'"options"\s*:\s*\[(.*?)\]', clean, re.DOTALL)
            if opt_match:
                raw_opts = opt_match.group(1)
                found_opts = re.findall(r'"(.*?)"', raw_opts)
                if found_opts:
                    options = found_opts

            idx_match = re.search(r'"(?:correct_index|answer|correct_answer)"\s*:\s*(\d+)', clean)
            if idx_match:
                correct_index = int(idx_match.group(1))

            exp_match = re.search(r'"explanation"\s*:\s*"(.*?)"\s*\}', clean, re.DOTALL)
            if exp_match:
                explanation = exp_match.group(1).strip()

    if options and options != ["Option A", "Option B", "Option C", "Option D"]:
        options, correct_index = _shuffle_quiz_options(options, correct_index)

    return {
        "intro": intro_raw,
        "lecture": lecture_raw,
        "question": question,
        "options": options,
        "correct_index": correct_index,
        "explanation": explanation,
    }

_WORKED_EXAMPLE_ANCHOR_RE = re.compile(r"(let us walk through|worked example)", re.IGNORECASE)
_HEADING_START_RE = re.compile(r"^\*\*[^*]+\*\*")
_WORKED_EXAMPLE_MAX_MERGE = 3600  # hard cap so one merge run can't swallow the rest of the lecture


def _merge_worked_example_paragraphs(paragraphs):
    """Group a worked example's intro paragraph with every sub-step
    paragraph that follows it into one atomic block, so the greedy
    packer in _split_into_pages can never place a page break inside
    one — it either keeps the whole example together on one page, or
    (if the example alone exceeds target_chars) gives it its own page,
    but never opens mid-calculation or mid-sentence."""
    merged = []
    i, n = 0, len(paragraphs)
    while i < n:
        para = paragraphs[i]
        if _WORKED_EXAMPLE_ANCHOR_RE.search(para):
            block = para
            j = i + 1
            while j < n:
                nxt = paragraphs[j]
                if _WORKED_EXAMPLE_ANCHOR_RE.search(nxt) or _HEADING_START_RE.match(nxt.strip()):
                    break
                if len(block) + len(nxt) > _WORKED_EXAMPLE_MAX_MERGE:
                    break
                block = f"{block}\n\n{nxt}"
                j += 1
            merged.append(block)
            i = j
        else:
            merged.append(para)
            i += 1
    return merged

def _split_into_pages(text, target_chars=1800):
    """Break a long pre-generated lecture into digestible pages. Fenced
    blocks stay atomic, and a diagram stays glued to its lead-in sentence
    and its short caption line."""
    text = text.strip()

    FENCE_RE = re.compile(r"```.*?\n[\s\S]*?```", re.MULTILINE)
    placeholders = {}
    def _stash(m):
        key = f"\x00FENCE{len(placeholders)}\x00"
        placeholders[key] = m.group(0)
        return key
    protected_text = FENCE_RE.sub(_stash, text)

    paragraphs = [p for p in re.split(r"\n\s*\n", protected_text) if p.strip()]

    if len(paragraphs) <= 1:
        sentences = re.split(r"(?<=[.!?])\s+", protected_text)
        paragraphs = [s for s in sentences if s.strip()]

    def _restore(s):
        for key, val in placeholders.items():
            s = s.replace(key, val)
        return s

    # Glue: lead-in sentence + diagram + short caption stay on one page.
    glued = []
    for para in paragraphs:
        prev = glued[-1] if glued else ""
        if glued and para.strip() in placeholders and prev.strip() not in placeholders:
            glued[-1] = f"{prev}\n\n{para}"
        elif (glued and prev.rstrip().endswith("\x00") and len(para) < 300
              and not para.lstrip().startswith("**")):
            glued[-1] = f"{prev}\n\n{para}"
        else:
            glued.append(para)
    paragraphs = glued

    pages = []
    current = ""
    for para in paragraphs:
        real_len = len(_restore(para))
        if current and len(current) + real_len > target_chars:
            pages.append(_restore(current.strip()))
            current = para
        else:
            current = f"{current}\n\n{para}" if current else para
    if current.strip():
        pages.append(_restore(current.strip()))
    return pages if pages else [_restore(protected_text)]

def _parse_quiz_json(text):
    clean = text.replace("```json", "").replace("```", "").strip()
    clean = clean.replace("\\*", "*").replace("\\%", "%")
    start = clean.find("{")
    end = clean.rfind("}")
    if start != -1 and end != -1 and end > start:
        clean = clean[start:end + 1]

    question = ""
    options = ["Option A", "Option B", "Option C", "Option D"]
    correct_index = 0
    explanation = ""

    try:
        quiz_data = json.loads(clean)
        question = quiz_data.get("question", "")
        options = quiz_data.get("options", options)
        correct_index = (
            quiz_data.get("correct_index")
            if quiz_data.get("correct_index") is not None
            else quiz_data.get("answer")
            if quiz_data.get("answer") is not None
            else quiz_data.get("correct_answer", 0)
        )
        try:
            correct_index = int(correct_index)
        except (TypeError, ValueError):
            correct_index = 0
        explanation = quiz_data.get("explanation", "")
    except json.JSONDecodeError:
        q_match = re.search(r'"question"\s*:\s*"(.*?)"\s*,\s*"options"', clean, re.DOTALL)
        if q_match:
            question = q_match.group(1).strip()
        opt_match = re.search(r'"options"\s*:\s*\[(.*?)\]', clean, re.DOTALL)
        if opt_match:
            found_opts = re.findall(r'"(.*?)"', opt_match.group(1))
            if found_opts:
                options = found_opts
        idx_match = re.search(r'"(?:correct_index|answer|correct_answer)"\s*:\s*(\d+)', clean)
        if idx_match:
            correct_index = int(idx_match.group(1))
        exp_match = re.search(r'"explanation"\s*:\s*"(.*?)"\s*\}', clean, re.DOTALL)
        if exp_match:
            explanation = exp_match.group(1).strip()

    if not question:
        raise ValueError("No question could be parsed from quiz response")

    if options and options != ["Option A", "Option B", "Option C", "Option D"]:
        options, correct_index = _shuffle_quiz_options(options, correct_index)
    return {"question": question, "options": options, "correct_index": correct_index, "explanation": explanation}

# ─── Session ───────────────────────────────────────────────────────────────────

def _course_overview(profile, course_code, today_topics=()):
    """The whole course split into covered / still-to-cover for this student."""
    level = _course_level(course_code, profile)
    all_topics = _get_total_topics_for_course(course_code, level)
    done_names = set(
        TopicSession.objects.filter(
            session__student=profile,
            session__course_code=course_code,
            is_complete=True,
        ).values_list("topic_name", flat=True)
    )
    covered, remaining = [], []
    for number, name in enumerate(all_topics, start=1):
        item = {"number": number, "name": name, "is_today": name in today_topics}
        (covered if name in done_names else remaining).append(item)
    total = len(all_topics)
    return {
        "covered": covered,
        "remaining": remaining,
        "total": total,
        "done_count": len(covered),
        "percent": round(100 * len(covered) / total) if total else 0,
    }

@login_required
def session_view(request, course_code):
    if not hasattr(request.user, "profile"):
        return redirect("onboarding")

    profile = request.user.profile
    entry = get_object_or_404(TimetableEntry, student=profile, course_code=course_code)
    if entry.week_number == TEST_WEEK:
        return redirect(f"{reverse('simulator_setup', kwargs={'mode': 'auto'})}?course_code={course_code}")
    level = _course_level(course_code, profile)

    if request.method == "GET":
        existing_session = Session.objects.filter(
            student=profile, course_code=course_code,
            week_number=entry.week_number, is_complete=False,
        ).first()
        if existing_session:
            return _render_chat_session(request, entry, profile, existing_session)

        topics = _get_topics_for_week(course_code, level, entry.week_number)
        if not topics:
            if not _get_total_topics_for_course(course_code, level):
                messages.info(request, f"Lessons for {entry.course_code} aren't ready yet. Check back soon.")
                return redirect("dashboard")
            entry.is_completed = True
            entry.save(update_fields=["is_completed"])
            messages.success(request, f"You've completed all topics for {entry.course_code}! 🎉")
            return redirect("dashboard")

        return render(request, "core/session.html", {
            "entry": entry,
            "chat_mode": False,
            "upcoming_topics": topics,
            "overview": _course_overview(profile, course_code, topics),
        })

    action = request.POST.get("action")

    if action == "start":
        existing_session = Session.objects.filter(
            student=profile, course_code=course_code,
            week_number=entry.week_number, is_complete=False,
        ).first()
        if not existing_session:
            topics = _get_topics_for_week(course_code, level, entry.week_number)
            existing_session = Session.objects.create(
                student=profile, course_code=course_code, course_title=entry.course_title,
                week_number=entry.week_number, topics=topics, current_topic_index=0,
            )
            if entry.week_number in HEADS_UP_WEEKS:
                messages.info(
                    request,
                    f"Heads up: your {course_code} test comes at week 7 and covers weeks 1–6. "
                    f"You can't move on to week 8 until you've taken it.",
                )
        return redirect("session", course_code=course_code)

    if action == "show_lecture":
        topic_session_id = request.POST.get("topic_session_id")
        return redirect("topic_lecture", topic_session_id=topic_session_id)
    return redirect("session", course_code=course_code)

@login_required
def course_outline_view(request, course_code):
    if not hasattr(request.user, "profile"):
        return redirect("onboarding")

    profile = request.user.profile
    entry = get_object_or_404(TimetableEntry, student=profile, course_code=course_code)

    level = _course_level(course_code, profile)
    sessions = Session.objects.filter(student=profile, course_code=course_code)
    session_by_week = {s.week_number: s for s in sessions}

    weeks_display = []
    for week_number in range(1, TOTAL_TEACHING_WEEKS + 1):
        topics = _get_topics_for_week(course_code, level, week_number)
        if not topics:
            continue
        session = session_by_week.get(week_number)
        completed_indices = set()
        if session:
            completed_indices = set(
                session.topic_sessions.filter(is_complete=True).values_list("topic_index", flat=True)
            )
        weeks_display.append({
            "week_number": week_number,
            "topics": [{"name": n, "is_complete": i in completed_indices} for i, n in enumerate(topics)],
            "all_complete": len(completed_indices) == len(topics),
        })
    return render(request, "core/course_outline.html", {
        "entry": entry,
        "course_code": course_code,
        "course_title": entry.course_title,
        "weeks": weeks_display,
    })

def _teach_topic(request, session, entry, profile, topic_name, topic_index):
    slide_text = ""
    outline_text = ""

    chunk_text = _find_slide_content_for_topic(session.course_code, profile.level, topic_name)
    if chunk_text:
        slide_text = f"\n\nLECTURER SLIDES (focus only on content relevant to this topic):\n{chunk_text}"

    try:
        outline = CourseOutline.objects.get(
            course_code=session.course_code,
            level=profile.level,
            parsed=True,
        )
        if outline.extracted_text:
            outline_text = f"\n\nCOURSE OUTLINE REFERENCE:\n{outline.extracted_text[:2000]}"
    except CourseOutline.DoesNotExist:
        pass

    student_name = profile.user.first_name or profile.user.username

    try:
        full_text = _generate_topic_lecture(
            session.course_code, session.course_title,
            topic_name, session.week_number,
            profile.level, student_name, topic_index,
            slide_text + outline_text,
            total_topics_in_round=len(session.topics),
        )
    except Exception as e:
        return render(request, "core/session.html", {
            "entry": entry,
            "error": f"Could not load lecture: {str(e)}",
        })

    parsed = _parse_lecture(full_text)

    # Retry once if quiz parsing failed
    if not parsed["question"] or parsed["options"] == ["Option A", "Option B", "Option C", "Option D"]:
        try:
            full_text = _generate_topic_lecture(
                session.course_code, session.course_title,
                topic_name, session.week_number,
                profile.level, student_name, topic_index,
                slide_text + outline_text,
                total_topics_in_round=len(session.topics),
            )
            parsed = _parse_lecture(full_text)
        except Exception:
            pass

    if not parsed["question"]:
        parsed["question"] = f"Which of the following best describes a key concept from '{topic_name}'?"
    if parsed["options"] == ["Option A", "Option B", "Option C", "Option D"]:
        parsed["options"] = [
            "A. The concept applies only in theory",
            "B. The concept has direct practical applications",
            "C. The concept is unrelated to engineering",
            "D. The concept was recently discovered",
        ]
        parsed["correct_index"] = 1

    topic_session = TopicSession.objects.create(
        session=session,
        topic_name=topic_name,
        topic_index=topic_index,
        intro_content=parsed["intro"],
        lecture_content=parsed["lecture"],
        quiz_question=parsed["question"],
        quiz_options=parsed["options"],
        correct_answer_index=parsed["correct_index"],
        quiz_explanation=parsed["explanation"],
    )

    intro_html = render_lecture_markdown(parsed["intro"])

    return render(request, "core/session.html", {
        "entry": entry,
        "session": session,
        "topic_session": topic_session,
        "intro": intro_html,
        "topic_number": topic_index + 1,
        "total_topics": len(session.topics),
        "topic_name": topic_name,
    })

def _render_chat_session(request, entry, profile, session):
    current_index = session.current_topic_index
    topic_name = session.topics[current_index]

    topic_session = session.topic_sessions.filter(
        topic_index=current_index, is_complete=False,
    ).first()

    if not topic_session:
        topic_session = TopicSession.objects.create(
            session=session,
            topic_name=topic_name,
            topic_index=current_index,
        )

        # ── Check for pre-generated content ──────────────────────────────────────
    try:
        lesson = PreGeneratedLesson.objects.get(
            course__course_code=session.course_code,
            week_number=session.week_number,
            topic_title=topic_name,
            is_published=True,
        )
    except PreGeneratedLesson.DoesNotExist:
        return render(request, "core/session.html", {
            "entry": entry,
            "session": session,
            "topic_session": topic_session,
            "topic_number": topic_session.topic_index + 1,
            "total_topics": len(session.topics),
            "topic_name": topic_name,
            "chat_mode": True,
            "no_lecture_available": True,
        })

    is_pregenerated = True

    if not topic_session.lecture_content:
        topic_session.lecture_content = lesson.content_chunk
        pages = _split_into_pages(lesson.content_chunk, target_chars=1800)

        student_name = profile.user.first_name or profile.user.username
        greeting = (
            f"Hey {student_name}! 👋 How are you doing today? "
            f"Ready to tackle some engineering concepts together? "
            f"Let's dive into **{topic_name}**.\n\n"
        )
        if pages:
            pages[0] = greeting + pages[0]
        else:
            pages = [greeting]

        topic_session.chunks = pages
        topic_session.save(update_fields=["lecture_content", "chunks"])
    elif not topic_session.chunks:
        topic_session.chunks = _split_into_pages(topic_session.lecture_content, target_chars=700)
        topic_session.save(update_fields=["chunks"])

    saved_chunk_count = topic_session.chatmessage_set.filter(
        role="ai", is_pregenerated=True
    ).count()

    if saved_chunk_count == 0 and topic_session.chunks:
        ChatMessage.objects.create(
            topic_session=topic_session,
            role="ai",
            content=topic_session.chunks[0],
            image_url=None,
            is_pregenerated=True,
        )
        saved_chunk_count = 1

    if topic_session.chunks:
        correct_index = min(saved_chunk_count - 1, len(topic_session.chunks) - 1)
    else:
        correct_index = 0

    if topic_session.current_chunk_index != correct_index:
        topic_session.current_chunk_index = correct_index
        topic_session.save(update_fields=["current_chunk_index"])

    has_started_chat = topic_session.chatmessage_set.filter(role="ai").exists()
    existing_messages = list(
        ChatMessage.objects.filter(topic_session=topic_session)
        .order_by("created_at")
        .values("role", "content", "image_url", "is_pregenerated",
                topic_name=F("topic_session__topic_name"))
    )

    return render(request, "core/session.html", {
        "entry": entry,
        "session": session,
        "topic_session": topic_session,
        "topic_number": topic_session.topic_index + 1,
        "total_topics": len(session.topics),
        "topic_name": topic_name,
        "chat_mode": True,
        "has_started_chat": has_started_chat,
        "existing_messages_json": json.dumps(existing_messages),
        "is_pregenerated": is_pregenerated,
        "current_chunk_index": topic_session.current_chunk_index,
        "total_chunks": len(topic_session.chunks),
        "first_chunk": topic_session.chunks[topic_session.current_chunk_index] if topic_session.chunks else "",
        "is_last_chunk": (
            bool(topic_session.chunks)
            and topic_session.current_chunk_index == len(topic_session.chunks) - 1
        ),
    })


# ─── Quiz ──────────────────────────────────────────────────────────────────────

PLACEHOLDER_QUIZ_STEM = "Which of the following best describes a key concept from"
_GENERIC_STEM_RE = re.compile(r"key concept from|best describes a key concept", re.IGNORECASE)
_LECTURE_REF_RE = re.compile(r"\b(the lecture|the slides?|the passage|as discussed|as mentioned)\b", re.IGNORECASE)
_VAGUE_OPTION_RE = re.compile(r"all of the above|none of the above|both [a-d] and [a-d]", re.IGNORECASE)


def _has_real_quiz(ts):
    q = (ts.quiz_question or "").strip()
    return bool(q) and not q.startswith(PLACEHOLDER_QUIZ_STEM) and len(ts.quiz_options or []) == 4


def _quiz_is_valid(quiz):
    opts = quiz.get("options") or []
    if len(opts) != 4:
        return False
    texts = [re.sub(r"^[A-D]\.\s*", "", str(o)).strip() for o in opts]
    question = (quiz.get("question") or "").strip()
    if len(question) < 30 or _GENERIC_STEM_RE.search(question) or _LECTURE_REF_RE.search(question):
        return False
    if any(len(t) < 2 or re.fullmatch(r"option [a-d]", t.lower()) or _VAGUE_OPTION_RE.search(t) for t in texts):
        return False
    if len({t.lower() for t in texts}) != 4:
        return False
    return 0 <= quiz.get("correct_index", -1) < 4


def _quiz_key_checks_out(lecture, quiz):
    """A second, independent call answers the question from the lecture alone.
    If it disagrees with the stored key, the quiz is thrown away rather than
    risk marking a student wrong for the right answer."""
    prompt = (
        f"LECTURE:\n{lecture}\n\nQUESTION:\n{quiz['question']}\n\n"
        "OPTIONS:\n" + "\n".join(quiz["options"]) + "\n\n"
        "Using ONLY the lecture, which option is correct? Reply with a single letter: A, B, C or D."
    )
    try:
        raw, _ = _call_generation_model(prompt, None, 500, 0.0, "Quiz check", api_client=client, attempts=1)
    except Exception as e:
        print(f"Quiz check unavailable: {e}")
        return False
    m = re.match(r"\s*\(?([ABCD])\b", raw.upper())
    return bool(m) and "ABCD".index(m.group(1)) == quiz["correct_index"]


def _generate_lecture_quiz(topic_session):
    lecture = _strip_visual_blocks(topic_session.lecture_content or "").strip()
    if len(lecture) < 300:
        return None
    contents = f"Topic: {topic_session.topic_name}\n\nLECTURE:\n{lecture}"
    for attempt in range(2):
        try:
            raw, _ = _call_generation_model(
                contents, LECTURE_QUIZ_PROMPT, 3000, 0.5,
                f"Quiz gen — {topic_session.topic_name}", api_client=client, attempts=1,
            )
            quiz = _parse_quiz_json(raw)
        except Exception as e:
            print(f"Quiz gen attempt {attempt + 1} failed: {e}")
            continue
        if _quiz_is_valid(quiz) and _quiz_key_checks_out(lecture, quiz):
            return quiz
        print(f"Quiz gen attempt {attempt + 1} rejected by validation for {topic_session.topic_name}")
    return None


def _ensure_quiz(topic_session):
    """True if the topic has a real quiz (generating one if needed). Never saves a fake."""
    if topic_session.is_complete or _has_real_quiz(topic_session):
        return True
    quiz = _generate_lecture_quiz(topic_session)
    if quiz is None:
        return False
    with transaction.atomic():
        ts = TopicSession.objects.select_for_update().get(pk=topic_session.pk)
        if not _has_real_quiz(ts):
            ts.quiz_question = quiz["question"]
            ts.quiz_options = quiz["options"]
            ts.correct_answer_index = quiz["correct_index"]
            ts.quiz_explanation = quiz["explanation"]
            ts.save(update_fields=["quiz_question", "quiz_options", "correct_answer_index", "quiz_explanation"])
    topic_session.refresh_from_db()
    return True

@login_required
def quiz_view(request, topic_session_id):
    if not hasattr(request.user, "profile"):
        return redirect("onboarding")

    topic_session = get_object_or_404(TopicSession, id=topic_session_id, session__student=request.user.profile)
    session = topic_session.session
    entry = get_object_or_404(TimetableEntry, student=request.user.profile, course_code=session.course_code)
    quiz_ready = _ensure_quiz(topic_session)

    return render(request, "core/quiz.html", {
        "entry": entry,
        "session": session,
        "topic_session": topic_session,
        "question": topic_session.quiz_question,
        "options": topic_session.quiz_options,
        "topic_number": topic_session.topic_index + 1,
        "total_topics": len(session.topics),
        "topic_name": topic_session.topic_name,
        "quiz_ready": quiz_ready,
    })


# ─── Answer ────────────────────────────────────────────────────────────────────

@login_required
@require_POST
def answer_view(request):
    if not hasattr(request.user, "profile"):
        return redirect("onboarding")

    topic_session_id = request.POST.get("topic_session_id")
    answer_index = int(request.POST.get("answer_index", 0))
    profile = request.user.profile

    topic_session = get_object_or_404(TopicSession, id=topic_session_id, session__student=profile)
    if topic_session.is_complete:
        return redirect("quiz_result", topic_session_id=topic_session.id)
    session = topic_session.session
    entry = get_object_or_404(TimetableEntry, student=profile, course_code=session.course_code)

    correct = answer_index == topic_session.correct_answer_index
    xp = 50 if correct else 10

    topic_session.student_answer_index = answer_index
    topic_session.passed_quiz = correct
    topic_session.xp_earned = xp
    topic_session.is_complete = True
    topic_session.completed_at = timezone.now()
    topic_session.save()

    today = _today_lagos()
    if profile.last_session_date != today:
        if profile.last_session_date and _streak_continues(profile, profile.last_session_date, today):
            profile.streak += 1
        else:
            profile.streak = 1
    profile.last_session_date = today
    profile.xp += xp
    profile.save()

    session.xp_earned += xp
    session.save()

    correct_option = topic_session.quiz_options[topic_session.correct_answer_index]
    feedback = (
        "Correct! Well done! 🎉" if correct
        else f"Not quite — the correct answer was {correct_option}. Keep going! 💪"
    )

    next_index = topic_session.topic_index + 1
    total_topics = len(session.topics)
    is_last_topic = next_index >= total_topics

    if is_last_topic:
        session.is_complete = True
        session.current_topic_index = total_topics
        session.completed_at = timezone.now()
        session.save()
        advanced = TimetableEntry.objects.filter(
            pk=entry.pk, week_number=session.week_number
        ).update(week_number=session.week_number + 1)
        if advanced:
            entry.refresh_from_db()
            level = _course_level(session.course_code, profile)
            entry.is_completed = (
                entry.week_number != TEST_WEEK
                and not _get_topics_for_week(session.course_code, level, entry.week_number)
            )
            entry.save(update_fields=["is_completed"])

    return redirect("quiz_result", topic_session_id=topic_session.id)   # ← was: return render(request, "core/result.html", {...})


# ─── Next topic ────────────────────────────────────────────────────────────────

@login_required
@require_POST
def next_topic_view(request):
    if not hasattr(request.user, "profile"):
        return redirect("onboarding")

    topic_session_id = request.POST.get("topic_session_id")
    next_index = int(request.POST.get("next_topic_index", 0))
    profile = request.user.profile

    topic_session = get_object_or_404(TopicSession, id=topic_session_id, session__student=profile)
    session = topic_session.session

    session.current_topic_index = next_index
    session.save()

    return redirect("session", course_code=session.course_code)


# ─── Leaderboard ───────────────────────────────────────────────────────────────

@login_required
def leaderboard_view(request):
    top_students = StudentProfile.objects.select_related("user").order_by("-xp")[:20]
    my_level = request.user.profile.level if hasattr(request.user, "profile") else None
    return render(request, "core/leaderboard.html", {"top_students": top_students, "my_level": my_level})


# ─── Timetable ─────────────────────────────────────────────────────────────────

@login_required
def timetable_view(request):
    if not hasattr(request.user, "profile"):
        return redirect("onboarding")
    profile = request.user.profile
    entries = _ensure_study_days(profile)

    if request.method == "POST":
        if request.POST.get("action") == "reset":
            profile.timetable.update(study_days=None)
            _ensure_study_days(profile)
            messages.success(request, "Timetable reset to the default.")
        else:
            for e in entries:
                days = {int(x) for x in request.POST.getlist(f"days_{e.id}") if x.isdigit()}
                e.study_days = sorted(d for d in days if 0 <= d <= 6)
                e.save(update_fields=["study_days"])
            messages.success(request, "Timetable saved.")
        return redirect("timetable")

    return render(request, "core/timetable.html", {
        "timetable": entries,
        "entries": entries,
        "profile": profile,
        "weekdays": [(0, "Mon"), (1, "Tue"), (2, "Wed"), (3, "Thu"), (4, "Fri"), (5, "Sat"), (6, "Sun")],
        "schedule_preview": _generate_course_schedule_preview(profile, num_days=14),
        "today": _today_lagos(),
    })


# ─── Reschedule ────────────────────────────────────────────────────────────────

@login_required
@require_POST
def reschedule_session(request, entry_id):
    entry = get_object_or_404(TimetableEntry, id=entry_id, student=request.user.profile)
    entry.is_missed = True
    entry.rescheduled_to = date.today() + timedelta(days=1)
    entry.save()
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return JsonResponse({"ok": True, "course_code": entry.course_code})
    messages.success(request, f"{entry.course_code} rescheduled to tomorrow.")
    return redirect("dashboard")


# ─── Profile edit ──────────────────────────────────────────────────────────────

@login_required
def profile_edit_view(request):
    if not hasattr(request.user, "profile"):
        return redirect("onboarding")

    profile = request.user.profile
    old_level = profile.level
    old_semester = profile.semester

    if request.method == "POST":
        form = ProfileEditForm(request.POST, instance=profile, user=request.user)
        if form.is_valid():
            updated_profile = form.save(commit=False)
            level_changed = updated_profile.level != old_level
            semester_changed = updated_profile.semester != old_semester
            updated_profile.save()

            if level_changed or semester_changed:
                # Clear old timetable and sessions, keep XP and streak
                TimetableEntry.objects.filter(student=profile).delete()
                Session.objects.filter(student=profile).delete()
                _generate_timetable(profile)
                messages.success(request, f"Level updated to {profile.level}L Semester {profile.semester}. Your timetable has been regenerated.")
            else:
                messages.success(request, "Profile updated successfully.")

            # Redirect to SIWES if 400L Sem 2
            if profile.level == "400" and profile.semester == "2":
                return redirect("siwes")
            return redirect("dashboard")
    else:
        form = ProfileEditForm(instance=profile, user=request.user)

    return render(request, "core/profile_edit.html", {"form": form, "profile": profile})

@login_required
def account_settings_view(request):
    user = request.user
    has_password = user.has_usable_password()   # False for Google-only accounts
    PasswordForm = PasswordChangeForm if has_password else SetPasswordForm

    details_form = AccountDetailsForm(instance=user)
    password_form = PasswordForm(user)

    if request.method == "POST":
        action = request.POST.get("action")
        if action == "details":
            details_form = AccountDetailsForm(request.POST, instance=user)
            if details_form.is_valid():
                details_form.save()
                messages.success(request, "Your details have been updated.")
                return redirect("account_settings")
        elif action == "password":
            password_form = PasswordForm(user, request.POST)
            if password_form.is_valid():
                password_form.save()
                update_session_auth_hash(request, user)  # keeps you logged in
                messages.success(request, "Your password has been saved.")
                return redirect("account_settings")

    return render(request, "core/account_settings.html", {
        "details_form": details_form,
        "password_form": password_form,
        "has_password": has_password,
    })


# ─── Review ────────────────────────────────────────────────────────────────────

@login_required
def review_view(request, topic_session_id):
    if not hasattr(request.user, "profile"):
        return redirect("onboarding")

    topic_session = get_object_or_404(TopicSession, id=topic_session_id, session__student=request.user.profile)
    lecture_html = render_lecture_markdown(topic_session.lecture_content)

    return render(request, "core/review.html", {
        "topic_session": topic_session,
        "lecture_raw": topic_session.lecture_content,
        "correct": topic_session.student_answer_index == topic_session.correct_answer_index,
    })


# ─── History ───────────────────────────────────────────────────────────────────

@login_required
def history_view(request):
    if not hasattr(request.user, "profile"):
        return redirect("onboarding")

    profile = request.user.profile
    topic_sessions = TopicSession.objects.filter(
        session__student=profile,
        is_complete=True
    ).select_related("session").order_by("-session__started_at", "topic_index")

    history_by_course = {}
    for ts in topic_sessions:
        code = ts.session.course_code
        title = ts.session.course_title
        if code not in history_by_course:
            history_by_course[code] = {"course_title": title, "topics": []}
        history_by_course[code]["topics"].append(ts)

    return render(request, "core/history.html", {
        "history_by_course": history_by_course,
    })


# ─── Restart session ───────────────────────────────────────────────────────────

@login_required
@require_POST
def restart_session_view(request, course_code, week_number):
    if not hasattr(request.user, "profile"):
        return redirect("onboarding")

    profile = request.user.profile
    entry = get_object_or_404(TimetableEntry, student=profile, course_code=course_code)
    if week_number < 1 or week_number > entry.week_number or week_number == TEST_WEEK:
        messages.error(request, "You can only restart a week you have already reached.")
        return redirect("dashboard")

    session = Session.objects.filter(
        student=profile,
        course_code=course_code,
        week_number=week_number,
    ).first()

    if session:
        profile.xp = max(0, profile.xp - session.xp_earned)
        profile.save()
        session.delete()

    entry.week_number = week_number
    entry.is_completed = False
    entry.save()

    messages.success(request, f"{course_code} Week {week_number} has been restarted from the beginning.")
    # Fixed redirect pattern mapping lookup parameter
    return redirect("session", course_code)



@login_required
def manage_courses_view(request):
    if not hasattr(request.user, "profile"):
        return redirect("onboarding")

    profile = request.user.profile

    # Show ALL courses in the student's department/school, regardless of
    # level/semester — carry-over students may need to pick up a course
    # from an earlier level alongside their current ones.
    available_courses = CourseDefinition.objects.filter(
        school=profile.school,
        department=profile.department,
    ).order_by("level", "semester", "course_code")

    active_course_codes = TimetableEntry.objects.filter(
        student=profile,
        course_code__in=available_courses.values_list('course_code', flat=True)
    ).values_list('course_code', flat=True)

    if request.method == "POST":
        selected_codes = request.POST.getlist('selected_courses')

        TimetableEntry.objects.filter(
            student=profile,
            course_code__in=available_courses.values_list('course_code', flat=True)
        ).exclude(course_code__in=selected_codes).delete()

        Session.objects.filter(
            student=profile,
            course_code__in=available_courses.values_list('course_code', flat=True)
        ).exclude(course_code__in=selected_codes).delete()

        for code in selected_codes:
            course = available_courses.get(course_code=code)
            TimetableEntry.objects.get_or_create(
                student=profile,
                course_code=course.course_code,
                course_title=course.course_title,
                defaults={
                    'day': 'Wed',
                    'time': '12:00',
                    'week_number': 1,
                    'total_weeks': 10,
                }
            )

        messages.success(request, "Your course selections have been updated successfully!")
        return redirect('dashboard')

    # Group by level for display, so the template can render level headers
    courses_by_level = {}
    for course in available_courses:
        courses_by_level.setdefault(course.level, []).append(course)

    context = {
        'courses_by_level': courses_by_level,
        'active_course_codes': list(active_course_codes),
    }
    return render(request, 'core/manage_courses.html', context)

@login_required
def siwes_view(request):
    if not hasattr(request.user, "profile"):
        return redirect("onboarding")
    profile = request.user.profile
    if not (profile.level == "400" and profile.semester == "2"):
        return redirect("dashboard")
    return render(request, "core/siwes.html", {"profile": profile})

from functools import wraps

def staff_required(view_func):
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect("login")
        if not hasattr(request.user, "profile") or not request.user.profile.is_staff_member:
            return redirect("dashboard")
        return view_func(request, *args, **kwargs)
    return wrapper

def _bypasses_restrictions(request):
    """Staff and superuser accounts skip rolling-queue and daily-cap
    restrictions entirely — those exist to pace paying subscribers, not
    to block staff testing or reviewing the platform."""
    if not request.user.is_authenticated:
        return False
    if request.user.is_superuser:
        return True
    profile = getattr(request.user, "profile", None)
    return bool(profile and profile.is_staff_member)

LONG_JOB = 7200   # seconds a slide job may run (2 hours)

def _queue(request, func_name, *args, label, timeout=None):
    """Put a job in the queue and tell staff it was queued."""
    options = {"timeout": timeout} if timeout else {}
    async_task(f"core.tasks.{func_name}", *args, task_name=label, **options)
    messages.success(request, f"Queued: {label}. See progress on the Background Jobs page.")

@staff_required
def staff_portal_view(request):
    slides = SlideDocument.objects.all().order_by("-uploaded_at")[:10]
    outlines = CourseOutline.objects.all().order_by("-uploaded_at")[:10]
    past_questions = PastQuestion.objects.all().order_by("-uploaded_at")[:10]
    courses = CourseDefinition.objects.all().order_by("level", "semester", "course_code")

    slide_split_status = {}
    for slide in slides:
        total = slide.topic_chunks.count()
        empty = slide.topic_chunks.filter(is_empty=True).count()
        slide_split_status[slide.id] = {"total": total, "empty": empty}

    cleanup_fallback_status = {}
    for slide in slides:
        fallback_count = slide.cleanup_chunks.exclude(error_message="").count()
        if fallback_count:
            cleanup_fallback_status[slide.id] = fallback_count

    return render(request, "core/staff/portal.html", {
        "slides": slides,
        "outlines": outlines,
        "past_questions": past_questions,
        "courses": courses,
        "slide_split_status": slide_split_status,
        "cleanup_fallback_status": cleanup_fallback_status,
    })
    


@staff_required
def staff_upload_slide_view(request):
    if request.method == "POST":
        form = SlideUploadForm(request.POST, request.FILES)
        if form.is_valid():
            course_code = form.cleaned_data.get("course_code")
            level = form.cleaned_data.get("level")
            existing_slide = SlideDocument.objects.filter(course_code=course_code, level=level).first()

            if existing_slide:
                # Same trick as before: save the new file under a dummy level,
                # and let the worker merge it into the existing deck.
                temp = form.save(commit=False)
                temp.level = "999"
                temp.save()
                _queue(request, "parse_slide_upload", temp.id, existing_slide.id,
                       label=f"Append slide to {course_code}", timeout=LONG_JOB)
            else:
                slide = form.save()
                _queue(request, "parse_slide_upload", slide.id,
                       label=f"Parse slide {course_code}", timeout=LONG_JOB)
            return redirect("staff_portal")
    else:
        form = SlideUploadForm()

    return render(request, "core/staff/upload_form.html", {
        "form": form,
        "title": "Upload Course Slide",
        "description": "Upload a course slide deck. If a deck already exists for this course, the new text will be continuously appended to it.",
    })

@staff_required
def staff_upload_outline_view(request):
    if request.method == "POST":
        form = CourseOutlineUploadForm(request.POST, request.FILES)
        if form.is_valid():
            outline = form.save()
            _queue(request, "parse_outline", outline.id,
                   label=f"Parse outline {outline.course_code}")
            return redirect("staff_portal")
    else:
        form = CourseOutlineUploadForm()
    return render(request, "core/staff/upload_form.html", {
        "form": form,
        "title": "Upload Course Outline",
        "description": "Upload the course outline document. It will be parsed into weekly topics automatically.",
    })


@staff_required
def staff_upload_past_questions_view(request):
    if request.method == "POST":
        form = PastQuestionUploadForm(request.POST, request.FILES)
        if form.is_valid():
            pq = form.save()
            _queue(request, "parse_past_questions", pq.id,
                   label=f"Parse past questions {pq.course_code}")
            return redirect("staff_portal")
    else:
        form = PastQuestionUploadForm()
    return render(request, "core/staff/upload_form.html", {
        "form": form,
        "title": "Upload Past Questions",
        "description": "Upload past exam or test papers. Questions will be extracted and structured automatically.",
    })


@staff_required
def staff_manage_courses_view(request):
    if request.method == "POST":
        form = CourseDefinitionForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, "Course added successfully.")
            return redirect("staff_manage_courses")
    else:
        form = CourseDefinitionForm()

    courses = CourseDefinition.objects.all().order_by("level", "semester", "course_code")
    return render(request, "core/staff/manage_courses.html", {
        "form": form,
        "courses": courses,
    })

@staff_required
def staff_edit_course_view(request, course_id):
    course = get_object_or_404(CourseDefinition, id=course_id)
    if request.method == "POST":
        form = CourseDefinitionForm(request.POST, instance=course)
        if form.is_valid():
            form.save()
            messages.success(request, f"{course.course_code} updated successfully.")
            return redirect("staff_manage_courses")
    else:
        form = CourseDefinitionForm(instance=course)

    return render(request, "core/staff/edit_course.html", {
        "form": form,
        "course": course,
    })


@staff_required
def staff_delete_course_view(request, course_id):
    course = get_object_or_404(CourseDefinition, id=course_id)
    course.delete()
    messages.success(request, f"{course.course_code} deleted.")
    return redirect("staff_manage_courses")

# ─── Staff Delete Views ────────────────────────────────────────────────────────

@staff_required
@require_POST
def delete_slide(request, slide_id):
    """Delete a course slide deck and clean up its file from disk"""
    slide = get_object_or_404(SlideDocument, id=slide_id)
    
    if slide.file and os.path.isfile(slide.file.path):
        try:
            os.remove(slide.file.path)
        except OSError:
            pass
            
    course_code = slide.course_code
    slide.delete()
    messages.success(request, f"Slide deck for {course_code} deleted successfully.")
    return redirect("staff_portal")


@staff_required
def delete_outline_view(request, outline_id):
    from .models import CourseOutline
    try:
        outline = CourseOutline.objects.get(id=outline_id)
        outline.delete()
        messages.success(request, "Outline deleted.")
    except CourseOutline.DoesNotExist:
        messages.warning(request, "Outline not found — it may have already been deleted.")
    return redirect("staff_portal")


@staff_required
@require_POST
def delete_past_question(request, question_id):
    """Delete a past question upload and clean up its file from disk"""
    pq = get_object_or_404(PastQuestion, id=question_id)
    
    if pq.file and os.path.isfile(pq.file.path):
        try:
            os.remove(pq.file.path)
        except OSError:
            pass
            
    course_code = pq.course_code
    pq.delete()
    messages.success(request, f"Past question for {course_code} deleted successfully.")
    return redirect("staff_portal")


@staff_required
@require_POST
def delete_course_definition(request, course_id):
    """Delete a defined course record"""
    course = get_object_or_404(CourseDefinition, id=course_id)
    course_code = course.course_code
    course.delete()
    messages.success(request, f"Course definition {course_code} deleted successfully.")
    return redirect("staff_portal")

@staff_required
@require_POST
def retry_slide_topics_view(request, slide_id):
    slide = get_object_or_404(SlideDocument, id=slide_id)
    if not slide.extracted_text:
        messages.warning(request, "Can't retry — no extracted text saved for this slide.")
        return redirect("staff_portal")
    _queue(request, "retry_topic_extraction", slide.id,
           label=f"Retry topics {slide.course_code}", timeout=LONG_JOB)
    return redirect("staff_portal")


@staff_required
@require_POST
def retry_slide_cleanup_view(request, slide_id):
    slide = get_object_or_404(SlideDocument, id=slide_id)
    if not slide.extracted_text:
        messages.warning(request, "Can't retry — no extracted text saved for this slide.")
        return redirect("staff_portal")
    _queue(request, "retry_cleanup", slide.id, label=f"Retry cleanup {slide.course_code}", timeout=LONG_JOB)
    return redirect("staff_portal")


@staff_required
@require_POST
def retry_slide_topic_split_view(request, slide_id):
    slide = get_object_or_404(SlideDocument, id=slide_id)
    if not slide.chunks.exists():
        messages.warning(request, "Can't split by topic — no week-level chunks saved for this slide.")
        return redirect("staff_portal")
    _queue(request, "split_weeks_by_topic", slide.id,
           label=f"Topic split (weeks) {slide.course_code}", timeout=LONG_JOB)
    return redirect("staff_portal")


@staff_required
@require_POST
def run_topic_split_view(request, slide_id):
    slide = get_object_or_404(SlideDocument, id=slide_id)
    if not slide.extracted_topics or not slide.extracted_text.strip():
        messages.warning(request, "Can't split — no extracted topics or text saved for this slide.")
        return redirect("staff_portal")
    _queue(request, "run_topic_split", slide.id,
           label=f"Run topic split {slide.course_code}", timeout=LONG_JOB)
    return redirect("staff_portal")

@staff_required
@require_POST
def retry_slide_diagrams_view(request, slide_id):
    slide = get_object_or_404(SlideDocument, id=slide_id)
    if not slide.extracted_text:
        messages.warning(request, "Can't retry — no extracted text saved for this slide.")
        return redirect("staff_portal")
    _queue(request, "retry_diagrams", slide.id,
           label=f"Retry diagrams {slide.course_code}", timeout=LONG_JOB)
    return redirect("staff_portal")

@staff_required
@require_POST
def retry_outline_topics_view(request, outline_id):
    """Retry AI topic parsing for a course outline that already has text saved."""
    outline = get_object_or_404(CourseOutline, id=outline_id)

    if not outline.extracted_text:
        messages.warning(request, "Can't retry — no extracted text saved for this outline.")
        return redirect("staff_portal")

    try:
        from .outline_parser import _parse_course_outline
        _parse_course_outline(outline)
        outline.refresh_from_db()
        if outline.topics_json:
            messages.success(request, "Outline topics parsed successfully.")
        else:
            messages.warning(request, "Retry ran but no topics were extracted — check the terminal for the underlying error.")
    except Exception as e:
        import traceback
        traceback.print_exc()
        messages.warning(request, f"Retry failed: {str(e)}")

    return redirect("staff_portal")

@login_required
@require_POST
def chat_next_topic_view(request):
    topic_session = _own_topic_session(request, request.POST.get("topic_session_id"))
    return JsonResponse({"redirect": f"/quiz/{topic_session.id}/"})

@staff_required
@require_POST
def staff_bulk_delete_courses_view(request):
    course_ids = request.POST.getlist("course_ids")
    if course_ids:
        deleted_count, _ = CourseDefinition.objects.filter(id__in=course_ids).delete()
        messages.success(request, f"{deleted_count} course(s) deleted successfully.")
    else:
        messages.warning(request, "No courses were selected.")
    return redirect("staff_manage_courses")

@staff_required
def staff_generate_battle_questions_view(request):
    course_choices = list(
        SlideTopicChunk.objects.filter(is_empty=False, slide__level__in=["300", "400"])
        .values_list("slide__course_code", flat=True).distinct().order_by("slide__course_code")
    )

    if request.method == "POST":
        form = BattleQuestionGenerationForm(request.POST, course_choices=course_choices)
        if form.is_valid():
            _queue(
                request, "generate_battle_questions",
                form.cleaned_data["course_code"] or None,
                form.cleaned_data["retry_failed"],
                form.cleaned_data["limit"],
                label="Generate battle questions",
            )
            return redirect("staff_generate_battle_questions")
    else:
        form = BattleQuestionGenerationForm(course_choices=course_choices)

    total_chunks = SlideTopicChunk.objects.filter(is_empty=False, slide__level__in=["300", "400"]).count()
    completed = BattleQuestionGenerationChunk.objects.filter(status="COMPLETED").count()
    failed = BattleQuestionGenerationChunk.objects.filter(status="FAILED").count()
    attempted = BattleQuestionGenerationChunk.objects.count()
    never_attempted = max(0, total_chunks - attempted)

    return render(request, "core/staff/generate_battle_questions.html", {
        "form": form,
        "total_chunks": total_chunks,
        "completed": completed,
        "failed": failed,
        "never_attempted": never_attempted,
    })

def _render_reference_table_lecture(topic_name, chunk_text):
    """No LLM call — the source is a bare reference table/chart, so we
    present it directly rather than risking fabricated explanation."""
    return (
        f"**{topic_name}**\n\n"
        f"This is a quick-reference topic — here's exactly what your lecturer's slide covers:\n\n"
        f"{chunk_text.strip()}"
    )

GENERATION_MODELS = ["gemini-3.6-flash", "gemini-3.7-flash", "gemini-3.8-flash"]


def _call_generation_model(contents, system_instruction, max_output_tokens, temperature, label,
                           api_client=None, attempts=3):
    """Retries transient errors, and falls back to the next model when a model's quota is gone."""
    api = api_client or generation_client
    last_error = None
    for model in GENERATION_MODELS:
        for attempt in range(attempts):
            last_try = attempt == attempts - 1
            try:
                response = api.models.generate_content(
                    model=model,
                    contents=contents,
                    config=types.GenerateContentConfig(
                        system_instruction=system_instruction,
                        max_output_tokens=max_output_tokens,
                        temperature=temperature,
                    ),
                )
                text = response.text
                if not text or not text.strip():
                    raise ValueError("Empty response")
                hit_limit = False
                try:
                    hit_limit = "MAX_TOKENS" in str(response.candidates[0].finish_reason)
                except Exception:
                    pass
                return text, hit_limit
            except Exception as e:
                last_error = e
                s = str(e)
                if "429" in s or "RESOURCE_EXHAUSTED" in s:
                    if "PerDay" in s:
                        print(f"{label}: {model} daily quota used up — trying next model.")
                        break
                    if last_try:
                        break
                    print(f"{label}: {model} 429 (attempt {attempt + 1}/{attempts}) — sleeping 20s...")
                    time.sleep(20)
                    continue
                transient = any(m in s for m in (
                    "503", "UNAVAILABLE", "timeout", "Timeout",
                    "ConnectionError", "RemoteDisconnected",
                ))
                if transient and not last_try:
                    print(f"{label}: {model} transient error (attempt {attempt + 1}/{attempts}) — retrying in 10s...")
                    time.sleep(10)
                    continue
                break
    raise RuntimeError(f"{label} failed: {last_error}")


def _generate_pregenerated_lecture(course_code, course_title, topic_name, chunk_text,
                                   fix_notes="", is_course_opening=False, course_context=""):
    opening = ""
    if is_course_opening:
        opening = (
            "COURSE OPENING (this overrides the 'no greeting' rule): this is the very first "
            f"lesson of {course_code} — {course_title}. Begin with one sentence welcoming "
            "students to the course by its title, then two sentences on what this first topic "
            "covers. Nothing has been taught in this course yet, so do not refer back to earlier "
            "material, and define every course-specific term before you use it.\n\n"
        )

    fix_block = ""
    if fix_notes:
        fix_block = (
            "\n\nA PREVIOUS DRAFT OF THIS LECTURE HAD THE FOLLOWING FIDELITY ISSUES — DO NOT "
            "REPEAT THEM. Rewrite the lecture from scratch, fixing every one of these:\n"
            f"{fix_notes}\n"
            "If the earlier draft contained a visual block (svg, mermaid or json_chart), keep it. "
            "Change only the labels or connections listed above. Do not remove a visual just to "
            "avoid a flag.\n"
        )

    contents = (
        opening +
        f"Course: {course_code} — {course_title}\n"
        f"{course_context}"
        f"Topic to teach: {topic_name}\n\n"
        f"LECTURER SLIDES:\n{chunk_text}"
        f"{fix_block}"
    )

    return _call_generation_model(
        contents=contents,
        system_instruction=LECTURE_PROMPT,
        max_output_tokens=16000,
        temperature=0.4,
        label=f"Lecture gen — {topic_name}",
    )


def _generate_verified_lecture(course_code, course_title, topic_name, chunk_text,
                               is_course_opening=False, course_context=""):
    """Returns (lecture_text, review_note, hit_limit)."""
    text, hit_limit = _generate_pregenerated_lecture(
        course_code, course_title, topic_name, chunk_text,
        is_course_opening=is_course_opening, course_context=course_context,
    )
    if hit_limit:
        return text, "the model hit its output limit, so the lecture is cut off", True
    time.sleep(3)
    issues = _lecture_issues(_verify_lecture(chunk_text, text))

    if issues == []:
        return text, "", False
    if issues is None:
        return text, "the checker gave no usable report — read this one against the slides", False

    time.sleep(3.5)
    text2, hit_limit2 = _generate_pregenerated_lecture(
        course_code, course_title, topic_name, chunk_text,
        fix_notes="\n".join(issues), is_course_opening=is_course_opening,
        course_context=course_context,
    )
    if hit_limit2:
        return text, "checker flagged: " + " ".join(issues[:5]), False
    time.sleep(3)
    issues2 = _lecture_issues(_verify_lecture(chunk_text, text2))

    if issues2 == []:
        return text2, "", False
    if issues2 is None:
        return text2, "second draft could not be checked — read it against the slides", False
    if len(issues2) <= len(issues):
        return text2, "checker still flags: " + " ".join(issues2[:5]), False
    return text, "checker still flags: " + " ".join(issues[:5]), False

_VERIFIER_LABELS = {
    "unsupported": "Not in the slides",
    "strengthened": "Stated more strongly than the slides",
    "misplaced": "Fact attached to the wrong concept",
    "missing": "Slide point never taught",
    "unexplained_terms": "Topic-specific term used without explanation",
    "inconsistent": "Two different values given for one quantity",
}

def _verify_lecture(chunk_text, lecture):
    """Returns the verifier's report as a dict, or None if it couldn't be obtained/parsed."""
    prompt = LECTURE_VERIFIER_PROMPT.replace("__SLIDE__", chunk_text).replace("__LECTURE__", lecture)
    try:
        raw, _ = _call_generation_model(prompt, None, 2000, 0.0, "Lecture verifier")
    except Exception as e:
        print(f"Lecture verifier unavailable: {e}")
        return None
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        report = json.loads(raw[start:end + 1])
    except json.JSONDecodeError:
        return None
    return report if isinstance(report, dict) else None


def _lecture_issues(report):
    """List of issue strings ([] = clean), or None if there was no usable report."""
    if report is None:
        return None
    issues = []
    for key, label in _VERIFIER_LABELS.items():
        for item in (report.get(key) or []):
            issues.append(f"- {label}: {item}")
    return issues

@staff_required
def staff_pregeneerate_lessons_view(request):
    """Staff portal — pre-generate lessons for a course and week"""
    courses = CourseDefinition.objects.all().order_by("level", "semester", "course_code")

    if request.method == "POST":
        course = get_object_or_404(CourseDefinition, id=request.POST.get("course_id"))
        week_number = int(request.POST.get("week_number", 1))

        if get_generation_source(course) is None:
            messages.error(request, NO_SOURCE_MESSAGE)
            return redirect("staff_pregenerate_lessons")

        topics = _get_topics_for_week(course.course_code, course.level, week_number)
        if not topics:
            messages.error(request, f"No topics are scheduled for {course.course_code} Week {week_number}.")
            return redirect("staff_pregenerate_lessons")


        for i, topic in enumerate(topics):
            async_task(
                "core.tasks.pregenerate_topic_lesson",
                course.id, week_number, topic, i, len(topics),
                task_name=f"Lesson {course.course_code} W{week_number}: {topic[:60]}",
            )
        messages.success(
            request,
            f"Queued {len(topics)} lesson(s) for {course.course_code} Week {week_number}. "
            f"See progress on the Background Jobs page."
        )
        return redirect("staff_pregenerate_lessons")

    filter_course_id = request.GET.get("filter_course", "")
    lessons = PreGeneratedLesson.objects.select_related("course").order_by(
        "course__level", "course__course_code", "week_number", "topic_title"
    )
    if filter_course_id:
        lessons = lessons.filter(course_id=filter_course_id)

    return render(request, "core/staff/pregenerate_lessons.html", {
        "courses": courses,
        "lessons": lessons,
        "week_range": [w for w in range(1, TOTAL_TEACHING_WEEKS + 1) if w != TEST_WEEK],
        "filter_course_id": filter_course_id,
    })


@staff_required
@require_POST
def staff_publish_lesson_view(request, lesson_id):
    """Toggle publish status of a pre-generated lesson"""
    lesson = get_object_or_404(PreGeneratedLesson, id=lesson_id)
    lesson.is_published = not lesson.is_published
    lesson.save()
    status = "published" if lesson.is_published else "unpublished"
    messages.success(request, f"'{lesson.topic_title}' {status}.")
    return redirect("staff_pregenerate_lessons")


@staff_required
@require_POST
def staff_delete_lesson_view(request, lesson_id):
    """Delete a pre-generated lesson"""
    lesson = get_object_or_404(PreGeneratedLesson, id=lesson_id)
    topic = lesson.topic_title
    lesson.delete()
    messages.success(request, f"'{topic}' deleted.")
    return redirect("staff_pregenerate_lessons")

@staff_required
@require_POST
def staff_bulk_delete_lessons_view(request):
    course_id = request.POST.get("course_id")
    course = get_object_or_404(CourseDefinition, id=course_id)
    deleted_count, _ = PreGeneratedLesson.objects.filter(course=course).delete()
    messages.success(request, f"Deleted {deleted_count} lesson(s) for {course.course_code}.")
    return redirect(f"{reverse('staff_pregenerate_lessons')}?filter_course={course_id}")


@staff_required
@require_POST
def staff_bulk_publish_lessons_view(request):
    course_id = request.POST.get("course_id")
    course = get_object_or_404(CourseDefinition, id=course_id)
    updated = PreGeneratedLesson.objects.filter(
        course=course, is_published=False,
    ).exclude(source_type="outline").update(is_published=True)
    messages.success(request, f"Published {updated} lesson(s) for {course.course_code}.")
    return redirect(f"{reverse('staff_pregenerate_lessons')}?filter_course={course_id}")


@staff_required
@require_POST
def staff_bulk_unpublish_lessons_view(request):
    course_id = request.POST.get("course_id")
    course = get_object_or_404(CourseDefinition, id=course_id)
    updated = PreGeneratedLesson.objects.filter(course=course, is_published=True).update(is_published=False)
    messages.success(request, f"Unpublished {updated} lesson(s) for {course.course_code}.")
    return redirect(f"{reverse('staff_pregenerate_lessons')}?filter_course={course_id}")

def _refresh_lesson_for_students(lesson):
    """Reset every in-progress student session on this lesson's topic so the
    next page load pulls the current lesson. Completed sessions keep their
    quiz result, but their review text is updated to the new lecture."""
    base = TopicSession.objects.filter(
        session__course_code=lesson.course.course_code,
        session__week_number=lesson.week_number,
        topic_name=lesson.topic_title,
    )
    in_progress = base.filter(is_complete=False)
    ids = list(in_progress.values_list("id", flat=True))

    ChatMessage.objects.filter(topic_session_id__in=ids).delete()
    reset = in_progress.update(
        lecture_content="", chunks=[], current_chunk_index=0,
        quiz_question="", quiz_options=[], correct_answer_index=0, quiz_explanation="",
    )
    updated = base.filter(is_complete=True).update(lecture_content=lesson.content_chunk)
    return reset, updated


@staff_required
@require_POST
def staff_refresh_lesson_view(request, lesson_id):
    lesson = get_object_or_404(PreGeneratedLesson, id=lesson_id)
    reset, updated = _refresh_lesson_for_students(lesson)
    messages.success(
        request,
        f"'{lesson.topic_title}': {reset} in-progress student session(s) reset to the current "
        f"lecture, {updated} completed session(s) updated for review."
    )
    return redirect(f"{reverse('staff_pregenerate_lessons')}?filter_course={lesson.course_id}")


@staff_required
@require_POST
def staff_refresh_course_lessons_view(request):
    course = get_object_or_404(CourseDefinition, id=request.POST.get("course_id"))
    total_reset = total_updated = 0
    for lesson in PreGeneratedLesson.objects.filter(course=course, is_published=True):
        reset, updated = _refresh_lesson_for_students(lesson)
        total_reset += reset
        total_updated += updated
    messages.success(
        request,
        f"{course.course_code}: {total_reset} in-progress session(s) reset to the current "
        f"lectures, {total_updated} completed session(s) updated for review."
    )
    return redirect(f"{reverse('staff_pregenerate_lessons')}?filter_course={course.id}")

# ─── Simulator ─────────────────────────────────────────────────────────────────

def _get_xp_and_grade(percentage):
    if percentage >= 70:
        return "A", 200
    elif percentage >= 60:
        return "B", 150
    elif percentage >= 50:
        return "C", 100
    elif percentage >= 45:
        return "D", 50
    elif percentage >= 40:
        return "E", 30
    else:
        return "F", 10


@login_required
def simulator_home_view(request):
    if not hasattr(request.user, "profile"):
        return redirect("onboarding")

    profile = request.user.profile
    entries = list(profile.timetable.order_by("course_code"))
    pending_tests, test_heads_up = _test_context(profile, entries)
    past_auto_tests = SimulatorTest.objects.filter(
        student=profile, mode="auto", status="complete",
    )[:6]

    return render(request, "core/simulator/home.html", {
        "profile": profile,
        "pending_tests": pending_tests,
        "test_heads_up": test_heads_up,
        "past_auto_tests": past_auto_tests,
    })

def _start_auto_test(request, profile):
    course_code = request.GET.get("course_code", "")
    entry = TimetableEntry.objects.filter(
        student=profile, course_code=course_code, week_number=TEST_WEEK
    ).first()
    if entry is None:
        messages.info(request, "That course isn't at its test yet.")
        return redirect("simulator_home")

    existing = SimulatorTest.objects.filter(
        student=profile, course_code=course_code, mode="auto",
        week_number=TEST_WEEK, status="in_progress",
    ).first()
    if existing:
        return redirect("simulator_test", test_id=existing.id)

    level = _course_level(course_code, profile)
    topics = _ready_test_topics(course_code, level)
    try:
        if not topics:
            raise NotEnoughQuestions("No approved questions for weeks 1–6 yet.")
        questions, fmt = draw_test(profile, course_code, level, topics, "mixed")
    except NotEnoughQuestions:
        messages.warning(
            request,
            f"The {course_code} test is still being prepared. Check back soon. "
            f"You'll move on to week 8 once you've taken it.",
        )
        return redirect("simulator_home")

    test = SimulatorTest.objects.create(
        student=profile, mode="auto", question_format=fmt, week_number=TEST_WEEK,
        course_code=entry.course_code, course_title=entry.course_title,
        topic="Weeks 1–6", questions=questions,
    )
    return redirect("simulator_test", test_id=test.id)

@login_required
def simulator_setup_view(request, mode):
    if not hasattr(request.user, "profile"):
        return redirect("onboarding")
    profile = request.user.profile
    MAX_TOPICS_FOR_TEST = 4

    if mode == "auto":
        return _start_auto_test(request, profile)

    timetable = profile.timetable.all()

    if request.method == "POST":
        course_code = request.POST.get("course_code", "").strip()
        topics = list(dict.fromkeys(t.strip() for t in request.POST.getlist("topics") if t.strip()))
        test_mode = request.POST.get("test_mode", "mixed")

        if not course_code or not topics:
            messages.error(request, "Please select a course and at least one topic.")
            return redirect("simulator_setup", mode="voluntary")
        if len(topics) > MAX_TOPICS_FOR_TEST:
            messages.error(request, f"You can select up to {MAX_TOPICS_FOR_TEST} topics for a test.")
            return redirect("simulator_setup", mode="voluntary")
        if test_mode not in TEST_MODES:
            test_mode = "mixed"

        entry = get_object_or_404(TimetableEntry, student=profile, course_code=course_code)
        level = _course_level(course_code, profile)

        ready = {a["topic"] for a in get_topic_availability(course_code, level) if a["ready"]}
        not_ready = [t for t in topics if t not in ready]
        if not_ready:
            messages.error(request, "Coming soon, no questions yet: " + ", ".join(not_ready))
            return redirect("simulator_setup", mode="voluntary")

        try:
            questions, fmt = draw_test(profile, course_code, level, topics, test_mode)
        except NotEnoughQuestions as e:
            messages.error(request, str(e))
            return redirect("simulator_setup", mode="voluntary")

        test = SimulatorTest.objects.create(
            student=profile, mode="voluntary", question_format=fmt,
            week_number=entry.week_number,
            course_code=entry.course_code, course_title=entry.course_title,
            topic=", ".join(topics)[:200],
            questions=questions,
        )
        return redirect("simulator_test", test_id=test.id)

    return render(request, "core/simulator/setup.html", {
        "mode": "voluntary",
        "timetable": timetable,
        "selected_code": request.GET.get("course_code", ""),
        "max_topics": MAX_TOPICS_FOR_TEST,
    })

def _call_simulator_model(prompt, max_output_tokens=4000, max_retries=3, temperature=None):
    """Call the simulator Gemini client with retry/backoff for transient
    errors (429 rate limit, 503 overloaded) — mirrors _generate_topic_lecture's
    retry behavior, which the simulator path was missing entirely."""
    last_error = None
    for attempt in range(max_retries):
        try:
            return simulator_client.models.generate_content(
                model="gemini-3.6-flash",
                contents=prompt,
                config=types.GenerateContentConfig(
                    max_output_tokens=max_output_tokens, temperature=temperature,
                ),
            )
        except Exception as e:
            last_error = e
            error_str = str(e)
            is_rate_limit = "429" in error_str or "RESOURCE_EXHAUSTED" in error_str
            is_overloaded = "503" in error_str or "UNAVAILABLE" in error_str

            if is_rate_limit:
                print(f"Simulator gen: 429 hit (attempt {attempt + 1}/{max_retries}) — sleeping 20s...")
                time.sleep(20)
                continue
            if is_overloaded and attempt < max_retries - 1:
                print(f"Simulator gen: 503 overloaded (attempt {attempt + 1}/{max_retries}) — sleeping 10s...")
                time.sleep(10)
                continue
            # Non-transient error, or transient but out of retries — stop.
            break

    raise RuntimeError(f"Simulator question generation failed after {max_retries} attempts: {last_error}")

def _course_level(course_code, profile):
    """A carry-over student's course can belong to an earlier level than
    their own, and the bank/slides are keyed by the course's level."""
    course = CourseDefinition.objects.filter(course_code=course_code).first()
    return course.level if course else profile.level

@login_required
def topics_for_course_view(request):
    if not hasattr(request.user, "profile"):
        return JsonResponse({"topics": []})
    profile = request.user.profile
    course_code = request.GET.get("course_code", "")
    if not course_code or not TimetableEntry.objects.filter(
        student=profile, course_code=course_code
    ).exists():
        return JsonResponse({"topics": []})
    level = _course_level(course_code, profile)
    return JsonResponse({"topics": get_topic_availability(course_code, level)})


@login_required
def simulator_test_view(request, test_id):
    if not hasattr(request.user, "profile"):
        return redirect("onboarding")

    profile = request.user.profile
    test = get_object_or_404(SimulatorTest, id=test_id, student=profile)

    if test.status == "complete":
        return redirect("simulator_result", test_id=test.id)
    if test.answers:                       
        return redirect("simulator_grade", test_id=test.id)

    if request.method == "POST":
        answers = []
        for i, q in enumerate(test.questions):
            if q.get("options"):
                val = request.POST.get(f"answer_{i}", "")
                answers.append(int(val) if val.isdigit() else -1)
            else:
                answers.append(request.POST.get(f"answer_{i}", "").strip())
        test.answers = answers
        test.save()
        return redirect("simulator_grade", test_id=test.id)

    return render(request, "core/simulator/test.html", {
        "test": test,
        "questions": test.questions,
        "enumerate": enumerate,
    })

def _repair_and_parse_json_array(text):
    """Parse a JSON array from model output, tolerating the model's
    tendency to emit literal newlines/unescaped control chars inside
    string values (which json.loads rejects as 'Unterminated string')."""
    clean = text.replace("```json", "").replace("```", "").strip()
    start = clean.find("[")
    end = clean.rfind("]")
    if start != -1 and end != -1:
        clean = clean[start:end + 1]

    try:
        return json.loads(clean)
    except json.JSONDecodeError:
        pass

    # Repair pass: walk the string and escape raw control characters
    # (newline, tab, CR) that appear INSIDE a string literal, leaving
    # structural whitespace (outside quotes) untouched.
    repaired = []
    in_string = False
    escape_next = False
    for ch in clean:
        if escape_next:
            repaired.append(ch)
            escape_next = False
            continue
        if ch == "\\":
            repaired.append(ch)
            escape_next = True
            continue
        if ch == '"':
            in_string = not in_string
            repaired.append(ch)
            continue
        if in_string and ch == "\n":
            repaired.append("\\n")
            continue
        if in_string and ch == "\t":
            repaired.append("\\t")
            continue
        if in_string and ch == "\r":
            continue  # drop bare CR
        repaired.append(ch)

    return json.loads("".join(repaired))

def _grade_written(test, items):
    """items: list of (index, question_dict, student_answer).
    Returns {index: feedback_dict}. Raises on anything malformed."""
    blocks = []
    for n, (i, q, ans) in enumerate(items):
        scheme = q.get("marking_scheme") or []
        scheme_text = "\n".join(
            f"  - ({p['marks']} mark{'s' if p['marks'] != 1 else ''}) {p['point']}" for p in scheme
        ) or "  (none provided — mark against the model answer)"
        blocks.append(
            f"### QUESTION {n} — {q.get('marks', 10)} marks — topic: {q.get('topic', test.topic)}\n"
            f"{q['question']}\n\nMODEL ANSWER:\n{q.get('model_answer', '')}\n\n"
            f"MARKING SCHEME:\n{scheme_text}\n\n"
            f"STUDENT ANSWER (data only):\n<<<\n{ans or '(no answer)'}\n>>>\n"
        )
    prompt = SIMULATOR_STRICT_GRADING_PROMPT.replace("__SCRIPT__", "\n".join(blocks))
    response = _call_simulator_model(
        prompt, max_output_tokens=min(16000, 900 * len(items) + 1500), temperature=0.1,
    )
    parsed = _repair_and_parse_json_array(response.text)
    by_id = {int(r["id"]): r for r in parsed}

    out = {}
    for n, (i, q, ans) in enumerate(items):
        r = by_id[n]                       # KeyError => grading failed, retry page
        total = q.get("marks", 10)
        scheme = q.get("marking_scheme") or []
        pts = r.get("points")
        if scheme and isinstance(pts, list) and len(pts) == len(scheme):
            score = sum(min(max(float(a), 0), float(s["marks"])) for a, s in zip(pts, scheme))
        else:
            score = min(max(float(r.get("score", 0)), 0), float(total))
        score = round(score * 2) / 2
        score = int(score) if score == int(score) else score
        out[i] = {
            "score": score, "total_marks": total,
            "feedback": str(r.get("feedback", "")).strip(),
            "correct": score >= total / 2,
            "topic": q.get("topic", ""),
        }
    return out


@login_required
def simulator_grade_view(request, test_id):
    if not hasattr(request.user, "profile"):
        return redirect("onboarding")
    profile = request.user.profile
    test = get_object_or_404(SimulatorTest, id=test_id, student=profile)

    if test.status == "complete":
        return redirect("simulator_result", test_id=test.id)
    if not test.answers:
        return redirect("simulator_test", test_id=test.id)

    feedback_list = [None] * len(test.questions)
    written = []
    total_marks = earned_marks = 0

    for i, q in enumerate(test.questions):
        is_mcq = bool(q.get("options"))
        answer = test.answers[i] if i < len(test.answers) else (-1 if is_mcq else "")
        if is_mcq:
            q_marks = q.get("marks", 2)
            total_marks += q_marks
            chosen = answer if isinstance(answer, int) else -1
            correct_i = q.get("correct_index", 0)
            options = q.get("options", [])
            ok = chosen == correct_i
            score = q_marks if ok else 0
            earned_marks += score
            chosen_text = options[chosen] if 0 <= chosen < len(options) else "No answer"
            tag = f"[{q.get('topic')}] " if q.get("topic") else ""
            feedback_list[i] = {
                "score": score, "total_marks": q_marks, "correct": ok,
                "topic": q.get("topic", ""),
                "feedback": f"{tag}You chose: {chosen_text}. Correct answer: "
                            f"{options[correct_i]}. {q.get('explanation', '')}",
            }
        else:
            total_marks += q.get("marks", 10)
            written.append((i, q, answer))

    if written:
        try:
            graded = _grade_written(test, written)
        except Exception:
            import traceback
            traceback.print_exc()
            return render(request, "core/simulator/grading_failed.html", {"test": test})
        for i, fb in graded.items():
            feedback_list[i] = fb
            earned_marks += fb["score"]

    percentage = round((earned_marks / total_marks) * 100, 1) if total_marks > 0 else 0
    grade, xp = _get_xp_and_grade(percentage)

    student_name = profile.user.first_name or profile.user.username
    try:
        overall = _call_simulator_model(
            SIMULATOR_OVERALL_FEEDBACK_PROMPT.format(
                student_name=student_name, course_code=test.course_code,
                course_title=test.course_title, topic=test.topic,
                percentage=percentage, grade=grade,
            ),
            max_output_tokens=300,
        )
        overall_feedback = overall.text.strip()
    except Exception:
        overall_feedback = f"You scored {percentage}% — Grade {grade}. Keep studying and you'll improve!"

    with transaction.atomic():
        fresh = SimulatorTest.objects.get(id=test.id)
        if fresh.status == "complete":
            return redirect("simulator_result", test_id=test.id)

        test.ai_feedback = feedback_list
        test.overall_feedback = overall_feedback
        test.percentage_score = percentage
        test.grade = grade
        test.xp_earned = xp
        test.status = "complete"
        test.completed_at = timezone.now()
        test.save()
        StudentProfile.objects.filter(pk=profile.pk).update(xp=F("xp") + xp)
        if test.mode == "auto":
            TimetableEntry.objects.filter(
                student=profile, course_code=test.course_code, week_number=TEST_WEEK,
            ).update(week_number=TEST_WEEK + 1)

    return redirect("simulator_result", test_id=test.id)

@staff_required
def staff_sim_bank_view(request):
    list_url = reverse("staff_sim_bank")

    if request.method == "POST":
        action = request.POST.get("action")
        course_code = request.POST.get("course_code", "")
        status = request.POST.get("status", "pending_review")

        if action in ("approve", "unapprove", "retire"):
            q = get_object_or_404(SimulatorQuestion, id=request.POST.get("question_id"))
            q.status = {"approve": "approved", "unapprove": "pending_review", "retire": "retired"}[action]
            q.save(update_fields=["status", "updated_at"])
        elif action == "approve_all":
            n = SimulatorQuestion.objects.filter(
                course_code=course_code, status="pending_review", verified=True,
            ).update(status="approved")
            messages.success(request, f"Approved {n} question(s) for {course_code}.")
        elif action == "generate_topic":
            topic = request.POST.get("topic", "")
            _queue(
                request, "generate_sim_topic",
                course_code, topic, request.POST.get("force") == "1",
                label=f"Sim bank {course_code}: {topic[:60]}",
            )
        return redirect(f"{list_url}?course_code={course_code}&status={status}")

    course_code = request.GET.get("course_code", "")
    status = request.GET.get("status", "pending_review")
    course_choices = list(
        CourseDefinition.objects.order_by("level", "course_code").values_list("course_code", flat=True)
    )

    topic_rows, questions = [], []
    if course_code:
        course = CourseDefinition.objects.filter(course_code=course_code).first()
        level = course.level if course else ""
        counts = {
            (r["topic_name"], r["status"]): r["n"]
            for r in SimulatorQuestion.objects.filter(course_code=course_code)
            .values("topic_name", "status").annotate(n=Count("id"))
        }
        states = {s.topic_name: s for s in SimulatorBankTopic.objects.filter(course_code=course_code)}
        for t in _get_total_topics_for_course(course_code, level):
            topic_rows.append({
                "topic": t, "state": states.get(t),
                "pending": counts.get((t, "pending_review"), 0),
                "approved": counts.get((t, "approved"), 0),
            })
        questions = SimulatorQuestion.objects.filter(
            course_code=course_code, status=status,
        ).order_by("topic_name", "question_type", "id")

    return render(request, "core/staff/review_sim_questions.html", {
        "course_choices": course_choices, "course_code": course_code, "status": status,
        "topic_rows": topic_rows, "questions": questions,
    })

@login_required
def simulator_result_view(request, test_id):
    if not hasattr(request.user, "profile"):
        return redirect("onboarding")

    profile = request.user.profile
    test = get_object_or_404(SimulatorTest, id=test_id, student=profile)

    questions_with_feedback = list(zip(test.questions, test.ai_feedback))

    return render(request, "core/simulator/result.html", {
        "test": test,
        "questions_with_feedback": questions_with_feedback,
        "profile": profile,
    })

# ─── Chunk navigation ──────────────────────────────────────────────────────────

@require_POST
def chunk_next_view(request):
    """Student clicked Got it — advance to next chunk, no Gemini call for
    text (image generation for [IMAGE:] markers, if present, still runs)."""
    topic_session_id = request.POST.get("topic_session_id")
    topic_session = get_object_or_404(TopicSession, id=topic_session_id)

    total_chunks = len(topic_session.chunks)
    if total_chunks == 0:
        return JsonResponse({"action": "reload"})
    saved_chunk_count = topic_session.chatmessage_set.filter(
        role="ai", is_pregenerated=True
    ).count()

    next_index = saved_chunk_count

    if next_index >= total_chunks:
        return JsonResponse({"action": "quiz_time"})

    raw_chunk_text = topic_session.chunks[next_index]
    is_last = (next_index == total_chunks - 1)

    ChatMessage.objects.create(
        topic_session=topic_session,
        role="ai",
        content=raw_chunk_text,
        image_url=None,
        is_pregenerated=True,
    )

    if topic_session.current_chunk_index != next_index:
        topic_session.current_chunk_index = next_index
        topic_session.save(update_fields=["current_chunk_index"])

    return JsonResponse({
        "action": "next_chunk",
        "chunk": raw_chunk_text,
        "image_url": None,
        "chunk_index": next_index,
        "total_chunks": total_chunks,
        "is_last": is_last,
        "is_last_chunk": is_last,
    })


@require_POST
def chunk_clarify_view(request):
    """Student asked a clarification question — call Gemini with chunk context."""
    topic_session_id = request.POST.get("topic_session_id")
    user_message = request.POST.get("message", "").strip()
    topic_session = get_object_or_404(TopicSession, id=topic_session_id)

    if not user_message:
        return JsonResponse({"error": "No message provided"}, status=400)

    # Cap check
    if _check_daily_message_cap(topic_session):
        return JsonResponse({
            "error": "cap_reached",
            "message": "You've reached your 10 message limit for today. Come back tomorrow — your progress is saved. 🙏"
        }, status=429)

    # Save the student's message (kept so we can remove it if Gemini fails)
    user_msg = ChatMessage.objects.create(
        topic_session=topic_session,
        role="user",
        content=user_message,
    )

    # Count remaining messages
    student = topic_session.session.student
    today_count = ChatMessage.objects.filter(
        topic_session__session__student=student,
        role="user",
        created_at__date=date.today(),
    ).count()
    remaining = max(0, 10 - today_count)

    # Current chunk as context for Gemini
    current_chunk = ""
    if topic_session.chunks and topic_session.current_chunk_index < len(topic_session.chunks):
        current_chunk = topic_session.chunks[topic_session.current_chunk_index]

    student_name = topic_session.session.student.user.first_name or topic_session.session.student.user.username
    system_instruction = f"""You are Rovea, a friendly AI lecturer helping university students understand their course slides.

You are helping {student_name}, who is studying the topic: "{topic_session.topic_name}" ({topic_session.session.course_code}).

They have just read this chunk of the lecture:
---
{current_chunk}
---

The student has a question or needs clarification. Your job:
- If they said they don't understand everything: re-explain the chunk above from a completely different angle. Use a new analogy, a Nigerian everyday example, or a step-by-step breakdown — NOT the same wording. Then ask "Better now?"
- If they pointed to something specific: re-explain ONLY that specific part. Keep it short and clear. Then ask "Does that make sense?"
- If they asked a question: answer it directly and clearly using the chunk as your reference. Then ask "Ready to continue?"

Use {student_name}'s name occasionally in your reply — not every message, that gets robotic.
Keep it conversational. Use very simple words and short sentences, like a patient coursemate. Explain any technical term in plain words before you use it. Use at most 3 short paragraphs and, where it helps, one everyday analogy. Never re-teach the whole chunk unless they said "everything".
Never say "As an AI". Stay in character as Rovea."""

    FRIENDLY_ERROR = (
        "Rovea is a bit busy right now. Please try again in a few seconds — "
        "your question wasn't counted."
    )
    TRANSIENT_MARKERS = (
        "503", "UNAVAILABLE", "429", "RESOURCE_EXHAUSTED",
        "timeout", "Timeout", "ConnectionError", "RemoteDisconnected",
    )

    def event_stream():
        full_reply = ""
        last_error = None

        for attempt in range(3):
            try:
                stream = client.models.generate_content_stream(
                    model="gemini-3.6-flash",
                    contents=[{"role": "user", "parts": [{"text": user_message}]}],
                    config=types.GenerateContentConfig(
                        system_instruction=system_instruction,
                        max_output_tokens=2048,
                    ),
                )
                for chunk in stream:
                    if chunk.text:
                        full_reply += chunk.text
                        yield f"data: {json.dumps({'chunk': chunk.text})}\n\n"
                last_error = None
                break
            except Exception as e:
                last_error = e
                print(f"Clarify stream error (attempt {attempt + 1}/3): {e}")
                if full_reply:
                    break  # text already reached the student — don't retry mid-answer
                transient = any(m in str(e) for m in TRANSIENT_MARKERS)
                if transient and attempt < 2:
                    time.sleep(2 * (attempt + 1))
                    continue
                break

        # Nothing came back at all: hand the message back and show a friendly note
        if last_error is not None and not full_reply:
            user_msg.delete()
            yield f"data: {json.dumps({'error': FRIENDLY_ERROR})}\n\n"
            return

        ChatMessage.objects.create(
            topic_session=topic_session,
            role="ai",
            content=full_reply,
        )
        yield f"data: {json.dumps({'done': True, 'remaining_messages': remaining})}\n\n"

    resp = StreamingHttpResponse(event_stream(), content_type="text/event-stream")
    resp["Cache-Control"] = "no-cache"
    resp["X-Accel-Buffering"] = "no"
    return resp

@staff_required
def staff_preview_lesson_view(request, lesson_id):
    lesson = get_object_or_404(PreGeneratedLesson, id=lesson_id)
    lecture_html = render_lecture_markdown(lesson.content_chunk)
    return render(request, "core/staff/preview_lesson.html", {
        "lesson": lesson,
        "lecture_html": lecture_html,
        "char_count": len(lesson.content_chunk),
        "is_truncated": lesson.is_truncated,
    })

@staff_required
def staff_slide_diagnostics_view(request, slide_id):
    slide = get_object_or_404(SlideDocument, id=slide_id)

    topic_chunks = sorted(slide.topic_chunks.all(), key=lambda c: len(c.chunk_text), reverse=True)
    chunk_data = [
        {"topic_name": c.topic_name, "chars": len(c.chunk_text), "is_empty": c.is_empty}
        for c in topic_chunks
    ]

    return render(request, "core/staff/slide_diagnostics.html", {
        "slide": slide,
        "extracted_text_length": len(slide.extracted_text or ""),
        "extracted_topics_count": len(slide.extracted_topics or []),
        "chunk_data": chunk_data,
        "empty_count": sum(1 for c in chunk_data if c["is_empty"]),
        "extraction_chunks": slide.extraction_chunks.all(),
        "cleanup_chunks": slide.cleanup_chunks.all(),
        "topic_split_chunks": slide.topic_split_chunks.all() if hasattr(slide, "topic_split_chunks") else [],
    })

@staff_required
def staff_course_code_check_view(request):
    from collections import defaultdict
    codes_by_source = defaultdict(set)

    for s in SlideDocument.objects.all():
        codes_by_source[s.course_code.strip().upper().replace(" ", "")].add(("SlideDocument", s.course_code))
    for o in CourseOutline.objects.all():
        codes_by_source[o.course_code.strip().upper().replace(" ", "")].add(("CourseOutline", o.course_code))
    for c in CourseDefinition.objects.all():
        codes_by_source[c.course_code.strip().upper().replace(" ", "")].add(("CourseDefinition", c.course_code))

    mismatches = {
        normalized: variants for normalized, variants in codes_by_source.items()
        if len({v[1] for v in variants}) > 1
    }

    return render(request, "core/staff/course_code_check.html", {"mismatches": mismatches})

@staff_required
@require_POST
def staff_reset_course_view(request, course_id):
    course = get_object_or_404(CourseDefinition, id=course_id)

    lessons_deleted, _ = PreGeneratedLesson.objects.filter(course=course).delete()
    sessions_deleted, _ = Session.objects.filter(course_code=course.course_code).delete()
    entries_updated = TimetableEntry.objects.filter(course_code=course.course_code).update(
        week_number=1, is_completed=False
    )

    messages.success(
        request,
        f"Reset {course.course_code}: {lessons_deleted} lesson(s) deleted, "
        f"{sessions_deleted} session(s) deleted, {entries_updated} timetable entr"
        f"{'y' if entries_updated == 1 else 'ies'} reset to week 1."
    )
    return redirect("staff_manage_courses")

def _shuffle_quiz_options(options, correct_index):
    """Re-randomize option order so the correct answer isn't always in
    the same position, regardless of any bias in the AI's output.
    Strips existing 'A. '/'B. ' prefixes and re-applies them after
    shuffling, so labels stay consistent with the new order."""
    letters = ["A", "B", "C", "D"]
    # Strip any existing letter prefix like "A. " or "B) "
    stripped = [re.sub(r"^[A-Da-d][\.\)]\s*", "", opt).strip() for opt in options]

    if not (0 <= correct_index < len(stripped)):
        correct_index = 0

    correct_text = stripped[correct_index]
    indices = list(range(len(stripped)))
    random.shuffle(indices)

    shuffled = [stripped[i] for i in indices]
    new_correct_index = shuffled.index(correct_text)

    labeled = [f"{letters[i]}. {opt}" for i, opt in enumerate(shuffled)]
    return labeled, new_correct_index

@login_required
def topic_lecture_view(request, topic_session_id):
    if not hasattr(request.user, "profile"):
        return redirect("onboarding")
    profile = request.user.profile
    topic_session = get_object_or_404(TopicSession, id=topic_session_id, session__student=profile)
    session = topic_session.session
    entry = get_object_or_404(TimetableEntry, student=profile, course_code=session.course_code)
    lecture_html = render_lecture_markdown(topic_session.lecture_content)
    return render(request, "core/session.html", {
        "entry": entry,
        "session": session,
        "topic_session": topic_session,
        "lecture": lecture_html,
        "topic_number": topic_session.topic_index + 1,
        "total_topics": len(session.topics),
        "topic_name": topic_session.topic_name,
        "show_quiz": True,
    })

@login_required
def quiz_result_view(request, topic_session_id):
    if not hasattr(request.user, "profile"):
        return redirect("onboarding")
    profile = request.user.profile
    topic_session = get_object_or_404(TopicSession, id=topic_session_id, session__student=profile)
    session = topic_session.session
    entry = get_object_or_404(TimetableEntry, student=profile, course_code=session.course_code)

    correct = topic_session.passed_quiz
    correct_option = topic_session.quiz_options[topic_session.correct_answer_index]
    feedback = (
        "Correct! Well done! 🎉" if correct
        else f"Not quite — the correct answer was {correct_option}. Keep going! 💪"
    )
    next_index = topic_session.topic_index + 1
    total_topics = len(session.topics)
    is_last_topic = next_index >= total_topics

    return render(request, "core/result.html", {
        "entry": entry,
        "session": session,
        "topic_session": topic_session,
        "correct": correct,
        "xp_earned": topic_session.xp_earned,
        "feedback": feedback,
        "explanation": topic_session.quiz_explanation,
        "next_topic_index": next_index,
        "next_topic_name": session.topics[next_index] if not is_last_topic else None,
        "is_last_topic": is_last_topic,
        "total_topics": total_topics,
        "topic_number": topic_session.topic_index + 1,
    })

@login_required
def progress_view(request):
    if not hasattr(request.user, "profile"):
        return redirect("onboarding")

    profile = request.user.profile
    timetable = profile.timetable.all()

    course_defs = {
        c.course_code: c
        for c in CourseDefinition.objects.filter(
            course_code__in=timetable.values_list("course_code", flat=True)
        )
    }
    courses = [
        {
            "code": t.course_code,
            "title": t.course_title,
            "units": course_defs[t.course_code].units if t.course_code in course_defs else 3,
            "progress_pct": _get_course_progress_pct(profile, t.course_code),
        }
        for t in timetable
    ]

    recent_sessions = profile.sessions.all()[:5]
    sessions_done = profile.sessions.count()

    return render(request, "core/progress.html", {
        "profile": profile,
        "courses": courses,
        "recent_sessions": recent_sessions,
        "sessions_done": sessions_done,
    })

@staff_required
def staff_review_battle_questions_view(request):
    if request.method == "POST":
        question_id = request.POST.get("question_id")
        action = request.POST.get("action")
        question = get_object_or_404(BattleQuestion, id=question_id)
        result_message = ""

        if action == "approve":
            question.status = "approved"
            question.save(update_fields=["status"])
            result_message = "Approved."
        elif action == "disapprove":
            question.status = "pending_review"
            question.save(update_fields=["status"])
            result_message = "Moved back to pending review."
        elif action == "retire":
            question.status = "retired"
            question.save(update_fields=["status"])
            result_message = "Retired."
        elif action == "retire_and_regenerate":
            question.status = "retired"
            question.save(update_fields=["status"])
            if question.source_chunk:
                BattleQuestionGenerationChunk.objects.filter(source_chunk=question.source_chunk).delete()
                result_message = "Retired — source chunk reset for regeneration."
            else:
                result_message = "Retired — no source chunk to regenerate."

        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return JsonResponse({"ok": True, "message": result_message})
        return redirect("staff_review_battle_questions")

    course_filter = request.GET.get("course_code", "")

    questions = BattleQuestion.objects.filter(status="pending_review").select_related(
        "source_chunk"
    ).prefetch_related("options").order_by("course_code", "-created_at")
    if course_filter:
        questions = questions.filter(course_code=course_filter)

    approved_questions = BattleQuestion.objects.filter(status="approved").select_related(
        "source_chunk"
    ).prefetch_related("options").order_by("course_code", "-created_at")
    if course_filter:
        approved_questions = approved_questions.filter(course_code=course_filter)

    flagged, normal = [], []
    for q in questions:
        combined_text = q.stem + " " + " ".join(o.text for o in q.options.all())
        (flagged if CITATION_HEURISTIC_RE.search(combined_text) else normal).append(q)

    course_choices = list(
        BattleQuestion.objects.exclude(status="retired")
        .values_list("course_code", flat=True).distinct().order_by("course_code")
    )

    return render(request, "core/staff/review_battle_questions.html", {
        "flagged_questions": flagged,
        "normal_questions": normal,
        "approved_questions": approved_questions,
        "course_choices": course_choices,
        "selected_course": course_filter,
        "pending_count": len(flagged) + len(normal),
        "approved_count": approved_questions.count(),
    })

# ── Math-safe markdown rendering ────────────────────────────────────────────
# Lecture text contains raw LaTeX ($...$, $$...$$, \(...\), \[...\]) with
# underscores for subscripts (P_i, r_w, \bar{P}_i). Python-Markdown treats
# _..._ as italics and mangles those underscores — and sometimes the
# delimiters around them — before KaTeX ever sees the text. So math spans
# are pulled out into inert placeholders, run through Markdown untouched,
# then restored verbatim afterward, right before KaTeX renders the page.
_MATH_BLOCK_RE = re.compile(r"\$\$.*?\$\$", re.DOTALL)
_MATH_BRACKET_RE = re.compile(r"\\\[.*?\\\]", re.DOTALL)
_MATH_PAREN_RE = re.compile(r"\\\(.*?\\\)", re.DOTALL)
_MATH_INLINE_RE = re.compile(r"\$[^\$\n]+?\$")


def render_lecture_markdown(text):
    """Markdown -> HTML for lecture/intro content, with LaTeX math spans
    protected from Markdown's underscore-as-italics parsing."""
    if not text:
        return ""

    placeholders = {}

    def _stash(m):
        key = f"ZZMATHPLACEHOLDERZZ{len(placeholders)}ZZ"
        placeholders[key] = m.group(0)
        return key

    protected = text
    for pattern in (_MATH_BLOCK_RE, _MATH_BRACKET_RE, _MATH_PAREN_RE, _MATH_INLINE_RE):
        protected = pattern.sub(_stash, protected)

    html = markdown.markdown(protected, extensions=["extra"])

    for key, val in placeholders.items():
        html = html.replace(key, val)

    return html

@staff_required
def staff_jobs_view(request):
    rows = [
        {
            "name": t.name,
            "ok": t.success,
            "started": t.started,
            "stopped": t.stopped,
            "result": str(t.result)[:500],
            "warn": str(t.result).startswith("WARNING"),
        }
        for t in Task.objects.order_by("-started")[:40]
    ]
    return render(request, "core/staff/jobs.html", {
        "rows": rows,
        "waiting": OrmQ.objects.count(),   # queued or currently running
    })