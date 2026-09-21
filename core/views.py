import os
import json
import profile
from urllib import request
import markdown
import random
from datetime import date, timedelta
import re
import math
import time

from django.contrib import messages
from django.contrib.auth import login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import AuthenticationForm
from django.conf import settings
from django.shortcuts import render, redirect, get_object_or_404
from django.utils import timezone
from django.views.decorators.http import require_POST
from google import genai
from google.genai import types
from django.http import StreamingHttpResponse
from django.http import JsonResponse
from django.db.models import F


from .forms import SignupForm, OnboardingForm, ProfileEditForm, ElectiveSelectionForm
from .models import (
    StudentProfile, TimetableEntry, Session, TopicSession, ChatMessage,
    SlideDocument, CourseOutline, COURSES, COURSE_OUTLINES, CourseDefinition,
    PastQuestion, SimulatorTest, PreGeneratedLesson, SlideTopicChunk,
)
from .prompt import SYSTEM_PROMPT, CHAT_SYSTEM_PROMPT, QUIZ_GENERATION_PROMPT
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
IMAGE_MARKER_RE = re.compile(r'\[IMAGE:\s*(.*?)\]', re.IGNORECASE)

MARKDOWN_IMAGE_RE = re.compile(r'!\[(.*?)\]\(https?://\S+\)')
CITATION_HEURISTIC_RE = re.compile(
    r"\b(edition|glossary|et al\.?|isbn|vol\.|published|textbook)\b", re.IGNORECASE
)
MAX_TOPIC_CONTEXT_CHARS = 24000

def _process_chunk_image(chunk_text, course_code):
    """Extract an [IMAGE: ...] marker OR a hallucinated Markdown image
    tag (![alt](url) — models sometimes fabricate real-looking URLs to
    third-party image services despite instructions not to) from
    pregenerated chunk text, generate the real image, and return
    (clean_text, image_url). image_url is None if no marker is present
    or generation fails — caller just gets clean text with no image,
    never an error."""
    image_url = None
    description = None

    match = IMAGE_MARKER_RE.search(chunk_text)
    if match:
        description = match.group(1).strip()
        chunk_text = IMAGE_MARKER_RE.sub("", chunk_text).strip()
    else:
        md_match = MARKDOWN_IMAGE_RE.search(chunk_text)
        if md_match:
            # Salvage the alt text as a description, discard the
            # fabricated URL entirely — it will never resolve to a
            # real image, so it must never reach the student as text.
            description = md_match.group(1).strip()
            chunk_text = MARKDOWN_IMAGE_RE.sub("", chunk_text).strip()

    if description:
        try:
            from .lecture_images import generate_topic_image
            image_path = generate_topic_image(description, course_code)
            if image_path:
                image_url = settings.MEDIA_URL + image_path
        except Exception:
            import traceback
            traceback.print_exc()

    return chunk_text, image_url


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

def chat_message_view(request):
    topic_session_id = request.POST.get("topic_session_id")
    user_message = request.POST.get("message")
    is_retry = request.POST.get("retry") == "true"
    topic_session = get_object_or_404(TopicSession, id=topic_session_id)
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

    # ── Normal live Gemini path ───────────────────────────────────────────────

    slide_context = _find_slide_content_for_topic(
        topic_session.session.course_code,
        topic_session.session.student.level,
        topic_session.topic_name,
    )   

    system_instruction = CHAT_SYSTEM_PROMPT.format(
        student_name=topic_session.session.student.user.first_name or topic_session.session.student.user.username,
        topic_name=topic_session.topic_name,
        course_code=topic_session.session.course_code,
        slide_context=slide_context,
    )

    history = [] if is_start_trigger else _build_history(topic_session)

    message_to_send = (
        "Begin the session now — greet the student and introduce the topic."
        if is_start_trigger else user_message
    )

    messages_to_send = history + [{"role": "user", "parts": [{"text": message_to_send}]}]

    def event_stream():
        full_reply = ""
        try:
            stream = client.models.generate_content_stream(
                model="gemini-3.6-flash",
                contents=messages_to_send,
                config=types.GenerateContentConfig(
                    system_instruction=system_instruction,
                    max_output_tokens=2048,
                ),
            )
            for chunk in stream:
                if chunk.text:
                    full_reply += chunk.text
                    yield f"data: {json.dumps({'chunk': chunk.text})}\n\n"
        except Exception as e:
            import traceback
            traceback.print_exc()
            yield f"data: {json.dumps({'error': str(e)})}\n\n"
            return

        image_url = None
        image_match = IMAGE_MARKER_RE.search(full_reply)
        if image_match:
            description = image_match.group(1).strip()
            full_reply = IMAGE_MARKER_RE.sub("", full_reply)
            if description:
                try:
                    from .lecture_images import generate_topic_image
                    image_path = generate_topic_image(
                        description, topic_session.session.course_code
                    )
                    if image_path:
                        image_url = settings.MEDIA_URL + image_path
                except Exception:
                    import traceback
                    traceback.print_exc()

        is_complete = "TOPIC_COMPLETE" in full_reply
        clean_reply = full_reply.replace("TOPIC_COMPLETE", "").strip()
        ChatMessage.objects.create(
            topic_session=topic_session,
            role="ai",
            content=clean_reply,
            image_url=image_url,
        )
        yield f"data: {json.dumps({'done': True, 'topic_complete': is_complete, 'image_url': image_url, 'remaining_messages': remaining})}\n\n"

    resp = StreamingHttpResponse(event_stream(), content_type="text/event-stream")
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

    # Check if there are any electives available for this level/semester
    available_electives = CourseDefinition.objects.filter(
        level=profile.level,
        semester=profile.semester,
        school=profile.school,
        department=profile.department,
        is_elective=True,
    )

    # If no electives defined yet, skip straight to dashboard
    if not available_electives.exists():
        _generate_timetable(profile)
        return redirect("dashboard")

    if request.method == "POST":
        form = ElectiveSelectionForm(
            request.POST,
            level=profile.level,
            semester=profile.semester,
            school=profile.school,
            department=profile.department,
        )
        if form.is_valid():
            profile.elective_courses.set(form.cleaned_data["electives"])
            profile.save()
            _generate_timetable(profile)
            return redirect("dashboard")
    else:
        form = ElectiveSelectionForm(
            level=profile.level,
            semester=profile.semester,
            school=profile.school,
            department=profile.department,
            initial={"electives": profile.elective_courses.all()},
        )

    return render(request, "core/elective_selection.html", {
        "form": form,
        "profile": profile,
    })


def login_view(request):
    if request.user.is_authenticated:
        return redirect("dashboard")
    if request.method == "POST":
        form = AuthenticationForm(data=request.POST)
        if form.is_valid():
            user = form.get_user()
            login(request, user, backend="django.contrib.auth.backends.ModelBackend")
            return redirect("dashboard")
    else:
        form = AuthenticationForm()
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

    # Fall back to hardcoded COURSES if no CourseDefinitions exist yet
    if not all_courses:
        courses = COURSES.get(profile.level, {}).get(profile.semester, [])
        entries = []
        for i, course in enumerate(courses):
            day = days[i % len(days)]
            time = "09:00" if i < len(days) else "11:00"
            entries.append(TimetableEntry(
                student=profile,
                course_code=course["code"],
                course_title=course["title"],
                day=day,
                time=time,
                week_number=1,
                total_weeks=10,
            ))
        TimetableEntry.objects.bulk_create(entries)
        return

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

def _get_active_course_entry(profile):
    """Today's course in the rolling queue — purely date-driven, so every
    student with the same enrolled course list gets the same course on
    the same calendar day."""
    courses = list(profile.timetable.order_by("course_code"))
    if not courses:
        return None

    idx = _teaching_day_index(date.today())
    if idx is None:
        return None  # Sunday — rest day

    return courses[idx % len(courses)]

def _generate_course_schedule_preview(profile, num_days=14):
    """Project which course lands on each of the next `num_days` calendar
    days, using the same pure date→course mapping as
    _get_active_course_entry, so the preview always matches reality."""
    courses = list(profile.timetable.order_by("course_code"))
    if not courses:
        return []

    today = date.today()
    schedule = []
    for i in range(num_days):
        d = today + timedelta(days=i)
        idx = _teaching_day_index(d)
        if idx is None:
            schedule.append({"date": d, "course": None, "is_rest": True})
        else:
            schedule.append({"date": d, "course": courses[idx % len(courses)], "is_rest": False})
    return schedule


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
        }
        for t in timetable
    ]
    recent_sessions = profile.sessions.all()[:5]
    leaderboard = StudentProfile.objects.select_related("user").order_by("-xp")[:10]
    sessions_done = profile.sessions.count()

    active_entry = _get_active_course_entry(profile)
    if active_entry:
        todays_courses = [active_entry] if not active_entry.is_completed else []
        today = active_entry.course_code
        is_rest_day = False
    else:
        todays_courses = []
        today = None
        is_rest_day = True

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
    })


# ─── Helpers ───────────────────--------------------------------───────────────
# Fixed Monday anchor for the rolling course queue. Every student's
# rotation is computed purely from today's date relative to this epoch —
# never from individual login/signup history — so students with the same
# course list always land on the same course on the same calendar day.
TIMETABLE_EPOCH = date.fromisocalendar(2026, 1, 1)  # guaranteed Monday


def _teaching_day_index(target_date):
    """Zero-based teaching-day count (Mon–Sat) since TIMETABLE_EPOCH.
    Sunday returns None (rest day). Pure function of the date — no
    per-student state involved."""
    if target_date.weekday() == 6:
        return None
    delta_days = (target_date - TIMETABLE_EPOCH).days
    full_weeks, remainder = divmod(delta_days, 7)
    return full_weeks * 6 + remainder

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

    flat = []
    for week in sorted(COURSE_OUTLINES.get(course_code, {}).keys()):
        flat.extend(COURSE_OUTLINES[course_code][week])
    if flat:
        return flat

    return ["Core Concepts", "Key Applications", "Problem Solving"]


def _topic_round_sizes(total_topics, total_weeks=TOTAL_TEACHING_WEEKS):
    """How many topics land in each of the 15 rounds, preserving order.
    total_topics <= 15 → 1 per round, course finishes early once topics
    run out. total_topics > 15 → base = total_topics // 15 per round,
    remainder distributed one-per-round from round 1 — so a multi-topic
    round is always a CONTIGUOUS slice (Lecture 1 & 2, never Lecture 1 & 7)."""
    if total_topics <= 0:
        return [0] * total_weeks
    base, remainder = divmod(total_topics, total_weeks)
    return [base + 1 if i < remainder else base for i in range(total_weeks)]


def _get_topics_for_week(course_code, level, week_number):
    """Ordered, contiguous slice of topics for this teaching round.
    Empty list means the course has finished all its topics."""
    all_topics = _get_total_topics_for_course(course_code, level)
    sizes = _topic_round_sizes(len(all_topics))

    if week_number < 1 or week_number > len(sizes):
        return []

    start = sum(sizes[:week_number - 1])
    size = sizes[week_number - 1]
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

    user_message = (
        f"Student name: {student_name}\n"
        f"Level: {level}L\n"
        f"Course: {course_code} — {course_title}\n"
        f"Topic to teach: {topic_name}\n"
        f"Topic number: {topic_index + 1} of {total_topics_in_round} in this session\n"
        f"Week: {week} of {TOTAL_TEACHING_WEEKS}\n"
        f"STRICT INSTRUCTION: Teach ONLY '{topic_name}'. Do not teach any other topic. "
        f"Follow the course outline strictly. This is the exact topic scheduled for this session."
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


def _split_into_pages(text, target_chars=1800):
    """Break a long pre-generated lecture into digestible pages. Tries
    paragraph boundaries first; if the text has no blank-line breaks at
    all (single-newline formatting), falls back to sentence boundaries
    so pagination always works regardless of how Gemini formatted it."""
    text = text.strip()
    paragraphs = [p for p in re.split(r"\n\s*\n", text) if p.strip()]

    if len(paragraphs) <= 1:
        # No real paragraph breaks found — split on sentences instead.
        sentences = re.split(r"(?<=[.!?])\s+", text)
        paragraphs = [s for s in sentences if s.strip()]

    pages = []
    current = ""
    for para in paragraphs:
        if current and len(current) + len(para) > target_chars:
            pages.append(current.strip())
            current = para
        else:
            current = f"{current}\n\n{para}" if current else para
    if current.strip():
        pages.append(current.strip())
    return pages if pages else [text]

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

@login_required
def session_view(request, course_code):
    if not hasattr(request.user, "profile"):
        return redirect("onboarding")

    profile = request.user.profile
    entry = get_object_or_404(TimetableEntry, student=profile, course_code=course_code)

        # ── Rolling queue gate ──────────────────────────────────────────────────
    if not _bypasses_restrictions(request):
        active_entry = _get_active_course_entry(profile)
        if active_entry is None:
            messages.info(request, "It's Sunday — rest day! Head to the Simulator to practice topics you've already covered.")
            return redirect("simulator_home")
        if active_entry.course_code != course_code:
            messages.info(request, f"It's not {entry.course_code}'s turn yet — today's course is {active_entry.course_code}.")
            return redirect("dashboard")

    if request.method == "GET":
        existing_session = Session.objects.filter(
            student=profile, course_code=course_code,
            week_number=entry.week_number, is_complete=False,
        ).first()
        if existing_session:
            return _render_chat_session(request, entry, profile, existing_session)

        topics = _get_topics_for_week(course_code, profile.level, entry.week_number)
        if not topics:
            entry.is_completed = True
            entry.save(update_fields=["is_completed"])
            messages.success(request, f"You've completed all topics for {entry.course_code}! 🎉")
            return redirect("dashboard")

        return render(request, "core/session.html", {
            "entry": entry,
            "chat_mode": False,
            "upcoming_topics": topics,
        })

    action = request.POST.get("action")

    if action == "start":
        existing_session = Session.objects.filter(
            student=profile, course_code=course_code,
            week_number=entry.week_number, is_complete=False,
        ).first()
        if not existing_session:
            topics = _get_topics_for_week(course_code, profile.level, entry.week_number)
            existing_session = Session.objects.create(
                student=profile, course_code=course_code, course_title=entry.course_title,
                week_number=entry.week_number, topics=topics, current_topic_index=0,
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

    week_topics = COURSE_OUTLINES.get(course_code, {})

    sessions = Session.objects.filter(student=profile, course_code=course_code)
    session_by_week = {s.week_number: s for s in sessions}

    weeks_display = []
    for week_number in sorted(week_topics.keys()):
        topics = week_topics[week_number]
        session = session_by_week.get(week_number)
        completed_indices = set()
        if session:
            completed_indices = set(
                session.topic_sessions.filter(is_complete=True).values_list("topic_index", flat=True)
            )
        topics_display = [
            {"name": name, "is_complete": i in completed_indices}
            for i, name in enumerate(topics)
        ]
        weeks_display.append({
            "week_number": week_number,
            "topics": topics_display,
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

    intro_html = markdown.markdown(parsed["intro"], extensions=["extra"])

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
    is_pregenerated = False

    try:
            lesson = PreGeneratedLesson.objects.get(
                course__course_code=session.course_code,
                week_number=session.week_number,
                topic_title=topic_name,
                is_published=True,
            )
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

            # ── Source of truth for progress is the actual saved ChatMessage
            #    rows, NOT a separately-tracked integer. This makes position
            #    immune to ever drifting out of sync — even if
            #    current_chunk_index was wrong for any reason, this recomputes
            #    it fresh from what's actually been delivered and persisted. ──
            saved_chunk_count = topic_session.chatmessage_set.filter(
                role="ai", is_pregenerated=True
            ).count()

            if saved_chunk_count == 0 and topic_session.chunks:
                clean_text, image_url = _process_chunk_image(
                    topic_session.chunks[0], session.course_code
                )
                ChatMessage.objects.create(
                    topic_session=topic_session,
                    role="ai",
                    content=clean_text,
                    image_url=image_url,
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

    except PreGeneratedLesson.DoesNotExist:
            pass

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

@login_required
def quiz_view(request, topic_session_id):
    if not hasattr(request.user, "profile"):
        return redirect("onboarding")

    topic_session = get_object_or_404(TopicSession, id=topic_session_id, session__student=request.user.profile)
    session = topic_session.session
    entry = get_object_or_404(TimetableEntry, student=request.user.profile, course_code=session.course_code)

    return render(request, "core/quiz.html", {
        "entry": entry,
        "session": session,
        "topic_session": topic_session,
        "question": topic_session.quiz_question,
        "options": topic_session.quiz_options,
        "topic_number": topic_session.topic_index + 1,
        "total_topics": len(session.topics),
        "topic_name": topic_session.topic_name,
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

    profile.xp += xp
    today = date.today()
    if profile.last_session_date == today - timedelta(days=1):
        profile.streak += 1
    elif profile.last_session_date != today:
        profile.streak = 1
    profile.last_session_date = today
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
        entry.is_completed = True
        entry.week_number += 1
        entry.save()

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
    return render(request, "core/leaderboard.html", {"top_students": top_students})


# ─── Timetable ─────────────────────────────────────────────────────────────────

@login_required
def timetable_view(request):
    if not hasattr(request.user, "profile"):
        return redirect("onboarding")
    profile = request.user.profile
    timetable = profile.timetable.all()
    schedule_preview = _generate_course_schedule_preview(profile, num_days=14)
    return render(request, "core/timetable.html", {
        "timetable": timetable,
        "profile": profile,
        "schedule_preview": schedule_preview,
        "today": date.today(),
    })


# ─── Reschedule ────────────────────────────────────────────────────────────────

@login_required
@require_POST
def reschedule_session(request, entry_id):
    entry = get_object_or_404(TimetableEntry, id=entry_id, student=request.user.profile)
    entry.is_missed = True
    entry.rescheduled_to = date.today() + timedelta(days=1)
    entry.save()
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


# ─── Review ────────────────────────────────────────────────────────────────────

@login_required
def review_view(request, topic_session_id):
    if not hasattr(request.user, "profile"):
        return redirect("onboarding")

    topic_session = get_object_or_404(TopicSession, id=topic_session_id, session__student=request.user.profile)
    lecture_html = markdown.markdown(topic_session.lecture_content, extensions=["extra"])

    return render(request, "core/review.html", {
        "topic_session": topic_session,
        "lecture_html": lecture_html,
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

    return render(request, "core/staff/portal.html", {
        "slides": slides,
        "outlines": outlines,
        "past_questions": past_questions,
        "courses": courses,
        "slide_split_status": slide_split_status,
    })


@staff_required
def staff_upload_slide_view(request):
    if request.method == "POST":
        print("POST received")
        form = SlideUploadForm(request.POST, request.FILES)
        print(f"Form valid: {form.is_valid()}")
        print(f"Form errors: {form.errors}")
        
        if form.is_valid():
            course_code = form.cleaned_data.get('course_code')
            level = form.cleaned_data.get('level')
            
            # Check if a document already exists for this course
            existing_slide = SlideDocument.objects.filter(course_code=course_code, level=level).first()
            
            if existing_slide:
                # We have an existing slide deck. Let's process the new file temporarily.
                new_slide_temp = form.save(commit=False)
                
                # THE TRICK: Change the level to a dummy value so the database doesn't block the temporary save
                new_slide_temp.level = "999"
                new_slide_temp.save() 
                
                # Keep track of the old text before we extract the new stuff
                old_text = existing_slide.extracted_text
                
                try:
                    from .slide_topic_extractor import _parse_slide_document
                    _parse_slide_document(new_slide_temp)
                    
                    # Append the newly extracted text to the existing text
                    existing_slide.extracted_text = f"{old_text}\n\n--- [Slide Continuation] ---\n\n{new_slide_temp.extracted_text}"
                    
                    # Merge topics without duplicates
                    existing_topics = set(existing_slide.extracted_topics)
                    new_topics = [t for t in new_slide_temp.extracted_topics if t not in existing_topics]
                    existing_slide.extracted_topics.extend(new_topics)
                    
                    existing_slide.append_count += 1
                    existing_slide.save()
                    
                    messages.success(request, f"Slide appended to existing {course_code} deck. {len(new_topics)} new topics added.")
                
                except Exception as e:
                    messages.warning(request, f"Failed to append slide: {str(e)}")
                    
                finally:
                    # ALWAYS clean up the temporary record and file, even if parsing succeeds or crashes
                    if new_slide_temp.file and os.path.isfile(new_slide_temp.file.path):
                        try:
                            os.remove(new_slide_temp.file.path)
                        except OSError:
                            pass
                    new_slide_temp.delete()
                        
            else:
                # No existing slide, just save normally
                slide = form.save()
                try:
                    from .slide_topic_extractor import _parse_slide_document
                    _parse_slide_document(slide)
                    messages.success(request, f"First slide for {course_code} uploaded and {len(slide.extracted_topics)} topics extracted.")
                except Exception as e:
                    messages.warning(request, f"Slide saved but topic extraction failed: {str(e)}")
            
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
            try:
                from .outline_parser import _parse_course_outline
                _parse_course_outline(outline)
                messages.success(request, "Course outline uploaded and parsed successfully.")
            except Exception as e:
                messages.warning(request, f"Outline saved but parsing failed: {str(e)}")
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
            try:
                from .past_question_parser import _parse_past_question_file
                _parse_past_question_file(pq)
                messages.success(request, f"Past questions uploaded. {len(pq.parsed_questions)} questions extracted.")
            except Exception as e:
                messages.warning(request, f"File saved but parsing failed: {str(e)}")
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
    """Retry AI topic extraction for a slide — only reprocesses chunks that
    previously failed or are still pending; already-completed chunks are
    skipped, and their topics are preserved rather than overwritten."""
    slide = get_object_or_404(SlideDocument, id=slide_id)

    if not slide.extracted_text:
        messages.warning(request, "Can't retry — no extracted text saved for this slide.")
        return redirect("staff_portal")

    try:
        from .slide_topic_extractor import extract_topics_for_slide_resumable
        topics, incomplete = extract_topics_for_slide_resumable(slide)
        if incomplete:
            messages.warning(request, f"Retried — {len(topics)} topics found so far, but some chunks still failed. You may need to retry again.")
        else:
            messages.success(request, f"Topics extracted successfully — {len(topics)} topics found.")
    except Exception as e:
        import traceback
        traceback.print_exc()
        messages.warning(request, f"Retry failed: {str(e)}")

    return redirect("staff_portal")

@staff_required
@require_POST
def retry_slide_topic_split_view(request, slide_id):
    slide = get_object_or_404(SlideDocument, id=slide_id)
    if not slide.chunks.exists():
        messages.warning(request, "Can't split by topic — no week-level chunks saved for this slide.")
        return redirect("staff_portal")

    from .slide_topic_extractor import split_all_weeks_by_topic
    try:
        split_all_weeks_by_topic(slide)
        count = slide.topic_chunks.exclude(is_empty=True).count()
        messages.success(request, f"Topic-level split complete — {count} non-empty topic chunks saved.")
    except Exception as e:
        import traceback
        traceback.print_exc()
        messages.warning(request, f"Topic split retry failed: {str(e)}")

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

@require_POST
def chat_next_topic_view(request):
    topic_session_id = request.POST.get("topic_session_id")
    topic_session = get_object_or_404(TopicSession, id=topic_session_id)

    if not topic_session.quiz_question:
        transcript = "\n".join(
            f"{'Student' if m.role == 'user' else 'Rovea'}: {m.content}"
            for m in topic_session.chatmessage_set.order_by("created_at")
        )
        try:
            response = client.models.generate_content(
                model="gemini-3.6-flash",
                contents=f"Topic taught: {topic_session.topic_name}\n\nCONVERSATION TRANSCRIPT:\n{transcript}",
                config=types.GenerateContentConfig(
                    system_instruction=QUIZ_GENERATION_PROMPT,
                    max_output_tokens=1000,
                ),
            )
            quiz_data = _parse_quiz_json(response.text)
            topic_session.quiz_question = quiz_data["question"]
            topic_session.quiz_options = quiz_data["options"]
            topic_session.correct_answer_index = quiz_data["correct_index"]
            topic_session.quiz_explanation = quiz_data["explanation"]
        except Exception:
            import traceback
            traceback.print_exc()
            # Fallback so the quiz page is never blank, even if generation fails
            topic_session.quiz_question = f"Which of the following best describes a key concept from '{topic_session.topic_name}'?"
            topic_session.quiz_options = [
                "A. The concept applies only in theory",
                "B. The concept has direct practical applications",
                "C. The concept is unrelated to engineering",
                "D. The concept was recently discovered",
            ]
            topic_session.correct_answer_index = 1
            topic_session.quiz_explanation = ""
        topic_session.save()

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
            logs = []
            result = run_battle_question_generation(
                per_chunk=8,
                sleep_seconds=3.5,
                course_filter=form.cleaned_data["course_code"] or None,
                retry_failed=form.cleaned_data["retry_failed"],
                limit=form.cleaned_data["limit"],
                log=lambda msg: logs.append(msg),
            )
            if result["chunks_processed"] == 0:
                messages.info(request, "No eligible chunks left to process — everything is already generated (or previously failed; check 'retry failed' to try those again).")
            else:
                messages.success(
                    request,
                    f"Generated {result['questions_generated']} question(s) across "
                    f"{result['chunks_processed'] - result['chunks_failed']} chunk(s). "
                    f"All saved as pending_review — approve them in the admin before they're servable."
                )
            for failure in result["failures"]:
                messages.warning(request, f"Failed: {failure}")
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

@staff_required
def staff_pregeneerate_lessons_view(request):
    """Staff portal — pre-generate lessons for a course and week"""
    courses = CourseDefinition.objects.all().order_by("level", "semester", "course_code")

    if request.method == "POST":
        course_id = request.POST.get("course_id")
        week_number = int(request.POST.get("week_number", 1))
        course = get_object_or_404(CourseDefinition, id=course_id)

        # Get topics for this course and week
        topics = _get_topics_for_week(course.course_code, course.level, week_number)

        if not topics:
            messages.error(request, f"No topics found for {course.course_code} Week {week_number}. Upload a course outline first.")
            return redirect("staff_pregenerate_lessons")

        # Get outline text for context
        outline_text = ""
        try:
            outline = CourseOutline.objects.get(
                course_code=course.course_code,
                level=course.level,
                parsed=True,
            )
            if outline.extracted_text:
                outline_text = f"\n\nCOURSE OUTLINE REFERENCE:\n{outline.extracted_text[:2000]}"
        except CourseOutline.DoesNotExist:
            pass

        generated_count = 0
        errors = []

        for i, topic in enumerate(topics):
            chunk_text = _find_slide_content_for_topic(course.course_code, course.level, topic)
            slide_text = (
                f"\n\nLECTURER SLIDES (focus only on content relevant to this topic):\n{chunk_text}"
                if chunk_text else ""
            )
            existing = PreGeneratedLesson.objects.filter(
                course=course, week_number=week_number, topic_title=topic,
            ).first()

            if existing and existing.content_chunk.strip() and not existing.is_truncated:
                continue

            # NEW — reference-table topics skip LLM generation entirely.
            if chunk_text and is_reference_table_content(chunk_text):
                PreGeneratedLesson.objects.update_or_create(
                    course=course, week_number=week_number, topic_title=topic,
                    defaults={
                        "content_chunk": _render_reference_table_lecture(topic, chunk_text),
                        "is_published": False,
                        "is_truncated": False,
                        "continuation_attempts": 0,
                    }
                )
                generated_count += 1
                continue

            if existing and existing.is_truncated and existing.content_chunk.strip():
                if existing.continuation_attempts >= MAX_CONTINUATION_ATTEMPTS:
                    errors.append(
                        f"{topic}: still incomplete after {MAX_CONTINUATION_ATTEMPTS} "
                        f"continuation attempts — needs manual review/edit in admin."
                    )
                    continue

                try:
                    continuation = continue_truncated_lecture(
                        generation_client, course.course_code, course.course_title,
                        topic, week_number, course.level,
                        student_name="Student", topic_index=i,
                        previous_content=existing.content_chunk,
                        slide_text=slide_text + outline_text,
                        total_topics_in_round=len(topics),
                    )

                    if detect_likely_duplicate_reteach(existing.content_chunk, continuation):
                        errors.append(
                            f"{topic}: continuation looked like it was re-teaching an "
                            f"earlier section — discarded, not saved. Needs manual review "
                            f"or a fresh regeneration instead of continuation."
                        )
                        time.sleep(3.5)
                        continue  # don't merge/save it — leave existing.content_chunk untouched

                    merged = existing.content_chunk.rstrip() + "\n\n" + continuation.strip()
                    still_truncated = is_lecture_truncated(continuation)
                    final_content = merged if not still_truncated else find_safe_cutoff(merged)

                    existing.content_chunk = final_content
                    existing.is_truncated = still_truncated
                    existing.continuation_attempts += 1
                    existing.save(update_fields=["content_chunk", "is_truncated", "continuation_attempts"])

                    if still_truncated:
                        remaining = MAX_CONTINUATION_ATTEMPTS - existing.continuation_attempts
                        errors.append(
                            f"{topic}: still truncated after continuation pass "
                            f"({remaining} attempt(s) left) — will retry on next run."
                        )
                    else:
                        generated_count += 1

                    time.sleep(3.5)

                except Exception as e:
                    errors.append(f"{topic}: continuation failed — {str(e)}")
                    time.sleep(3)

                continue  # move to next topic — don't fall through to fresh generation

            # No existing content at all — fresh generation.
            try:
                full_text = _generate_topic_lecture(
                    course.course_code, course.course_title, topic, week_number,
                    course.level, student_name="Student", topic_index=i,
                    slide_text=slide_text + outline_text,
                    total_topics_in_round=len(topics),
                )

                if not full_text or not full_text.strip():
                    errors.append(f"{topic}: AI returned empty response — skipping.")
                    continue

                truncated = is_lecture_truncated(full_text)
                parsed = _parse_lecture(full_text)
                content = parsed["lecture"] if parsed.get("lecture") else full_text
                safe_content = content if not truncated else find_safe_cutoff(content)

                if not safe_content or not safe_content.strip():
                    errors.append(f"{topic}: Parsed content was empty — skipping.")
                    continue

                PreGeneratedLesson.objects.update_or_create(
                    course=course, week_number=week_number, topic_title=topic,
                    defaults={
                        "content_chunk": safe_content,
                        "is_published": False,
                        "is_truncated": truncated,
                        "continuation_attempts": 0,
                    }
                )
                generated_count += 1
                if truncated:
                    errors.append(f"{topic}: generated but truncated — will continue on next run.")

                time.sleep(3.5)

            except Exception as e:
                errors.append(f"{topic}: {str(e)}")
                time.sleep(3)
                continue

        if generated_count:
            messages.success(request, f"Generated {generated_count} lesson(s) for {course.course_code} Week {week_number}. Review and publish them in the admin panel.")
        if errors:
            for error in errors:
                messages.warning(request, f"Failed: {error}")

        return redirect("staff_pregenerate_lessons")

        # GET — show existing pre-generated lessons
    filter_course_id = request.GET.get("filter_course", "")
    lessons = PreGeneratedLesson.objects.select_related("course").order_by(
        "course__level", "course__course_code", "week_number", "topic_title"
    )
    if filter_course_id:
        lessons = lessons.filter(course_id=filter_course_id)

    return render(request, "core/staff/pregenerate_lessons.html", {
        "courses": courses,
        "lessons": lessons,
        "week_range": range(1, TOTAL_TEACHING_WEEKS + 1),
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
    updated = PreGeneratedLesson.objects.filter(course=course, is_published=False).update(is_published=True)
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


def _detect_question_format(course_code, level):
    """Detect question format from past questions — fall back to theory"""
    from .models import PastQuestion
    pqs = PastQuestion.objects.filter(course_code=course_code, level=level, parsed=True).first()
    if not pqs or not pqs.parsed_questions:
        return "theory"
    sample = pqs.parsed_questions[0]
    if "options" in sample and sample["options"]:
        return "mcq"
    if "model_answer" in sample:
        answer = sample["model_answer"].lower()
        has_calc = any(word in answer for word in ["=", "calculate", "formula", "equation", "kg", "m/s", "pa", "kpa", "mpa"])
        has_theory = any(word in answer for word in ["define", "explain", "describe", "state", "discuss"])
        if has_calc and has_theory:
            return "mixed"
        if has_calc:
            return "theory"
    return "theory"


def _get_past_q_reference(course_code, level, topic, limit=3):
    """Pull sample past questions to use as style reference for generation"""
    from .models import PastQuestion
    relevant = []
    pqs = PastQuestion.objects.filter(course_code=course_code, level=level, parsed=True)
    for pq in pqs:
        for q in (pq.parsed_questions or []):
            hint = (q.get("topic_hint") or "").lower()
            if any(word.lower() in hint for word in topic.split()):
                relevant.append(q)
    if not relevant:
        all_q = []
        for pq in pqs:
            all_q.extend(pq.parsed_questions or [])
        relevant = all_q
    random.shuffle(relevant)
    sample = relevant[:limit]
    if not sample:
        return "No past questions available — generate at appropriate university level difficulty."
    lines = []
    for q in sample:
        lines.append(f"- {q.get('question', '')}")
    return "\n".join(lines)


def _generate_test_questions(course_code, course_title, topic, level, weeks_covered, question_format):
    num_questions = random.randint(15, 20) if question_format == "mcq" else random.randint(2, 3)
    past_ref = _get_past_q_reference(course_code, level, topic)
    slide_context = _find_slide_content_for_topic(course_code, level, topic)  # ← was missing

    prompt = SIMULATOR_QUESTION_PROMPT.format(
        course_code=course_code,
        course_title=course_title,
        topic=topic,
        question_format=question_format,
        weeks_covered=weeks_covered,
        num_questions=num_questions,
        past_q_reference=past_ref,
        coverage_manifest="(none extracted — rely on slide content below)",
        slide_context=slide_context or "(no slide content found for this topic)",
    )

    response = _call_simulator_model(prompt, max_output_tokens=4000)

    clean = response.text.replace("```json", "").replace("```", "").strip()
    start = clean.find("[")
    end = clean.rfind("]")
    if start != -1 and end != -1:
        clean = clean[start:end + 1]
    return json.loads(clean)

def _generate_multi_topic_test_questions(selections, level, question_format):
    """
    selections: list of dicts, each {course_code, course_title, topic, weeks_covered}
    Max 4 for question_format in ("theory", "mixed"); MCQ can take more since
    there's no lettered-part mixing to manage.
    """
    if question_format == "mcq":
        num_questions = random.randint(15, 20)
        per_topic = max(1, num_questions // len(selections))
    else:
        num_questions = random.randint(2, 3)
        per_topic = None  # mixing happens inside the prompt, not by pre-splitting

    topic_blocks = []
    for i, sel in enumerate(selections, start=1):
        slide_context = _find_slide_content_for_topic(
            sel["course_code"], level, sel["topic"]
        )
        past_ref = _get_past_q_reference(sel["course_code"], level, sel["topic"])
        topic_blocks.append(
            TOPIC_BLOCK_TEMPLATE.format(
                index=i,
                topic=sel["topic"],
                course_code=sel["course_code"],
                course_title=sel["course_title"],
                coverage_manifest="(none extracted — rely on slide content below)",
                slide_context=slide_context or "(no slide content found for this topic)",
                past_q_reference=past_ref,
            )
        )

    if question_format == "mcq":
        prompt = SIMULATOR_QUESTION_PROMPT_MULTI_TOPIC_MCQ.format(
            num_topics=len(selections),
            topic_blocks="\n".join(topic_blocks),
            num_questions=num_questions,
            per_topic=per_topic,
        )
    else:
        prompt = SIMULATOR_QUESTION_PROMPT_MULTI_TOPIC.format(
            num_topics=len(selections),
            topic_blocks="\n".join(topic_blocks),
            question_format=question_format,
            num_questions=num_questions,
        )

    response = _call_simulator_model(prompt, max_output_tokens=4000)

    clean = response.text.replace("```json", "").replace("```", "").strip()
    start = clean.find("[")
    end = clean.rfind("]")
    if start != -1 and end != -1:
        clean = clean[start:end + 1]
    return json.loads(clean)


@login_required
def simulator_home_view(request):
    if not hasattr(request.user, "profile"):
        return redirect("onboarding")

    profile = request.user.profile
    timetable = profile.timetable.all()

    # Check which courses have a pending auto test (week 7, not yet done)
    auto_test_courses = []
    for entry in timetable:
        if entry.week_number == 7:
            already_done = SimulatorTest.objects.filter(
                student=profile,
                course_code=entry.course_code,
                mode="auto",
                week_number=7,
                status="complete",
            ).exists()
            auto_test_courses.append({
                "entry": entry,
                "done": already_done,
            })

    return render(request, "core/simulator/home.html", {
        "profile": profile,
        "auto_test_courses": auto_test_courses,
    })


@login_required
def simulator_setup_view(request, mode):
    """Setup page — pick course and topic before starting"""
    if not hasattr(request.user, "profile"):
        return redirect("onboarding")

    profile = request.user.profile
    MAX_TOPICS_FOR_TEST = 4          

    if mode == "auto":
        course_code = request.GET.get("course_code") or request.POST.get("course_code")
        entry = get_object_or_404(TimetableEntry, student=profile, course_code=course_code)

        already_done = SimulatorTest.objects.filter(
            student=profile,
            course_code=course_code,
            mode="auto",
            week_number=7,
            status="complete",
        ).exists()
        if already_done:
            messages.info(request, f"You have already completed the test week for {course_code}.")
            return redirect("simulator_home")

        if request.method == "POST":
            selections = [{
                "course_code": entry.course_code,
                "course_title": entry.course_title,
                "topic": "Weeks 1-6 Review",
                "weeks_covered": min(entry.week_number - 1, 6),
            }]
            return _start_simulator_test(request, profile, selections, mode="auto")

        return render(request, "core/simulator/setup.html", {
            "mode": "voluntary",
            "timetable": timetable,
            "max_topics": MAX_TOPICS_FOR_TEST,
        })

    else:
        timetable = profile.timetable.all()

        if request.method == "POST":
            course_code = request.POST.get("course_code", "").strip()
            topics = [t.strip() for t in request.POST.getlist("topics") if t.strip()]

            if not course_code or not topics:
                messages.error(request, "Please select a course and at least one topic.")
                return redirect("simulator_setup", mode="voluntary")

            if len(topics) > MAX_TOPICS_FOR_TEST:
                messages.error(request, f"You can select up to {MAX_TOPICS_FOR_TEST} topics for a test.")
                return redirect("simulator_setup", mode="voluntary")

            entry = get_object_or_404(TimetableEntry, student=profile, course_code=course_code)

            selections = [
                {
                    "course_code": entry.course_code,
                    "course_title": entry.course_title,
                    "topic": topic,
                    "weeks_covered": entry.week_number - 1,
                }
                for topic in topics
            ]

            try:
                return _start_simulator_test(request, profile, selections, mode="voluntary")
            except Exception as e:
                import traceback
                traceback.print_exc()
                messages.error(
                    request,
                    "The test generator is under heavy load right now — please try again in a minute or two."
                )
                return redirect("simulator_setup", mode="voluntary")


        selected_code = request.GET.get("course_code", "")
        available_topics = []

        if selected_code:
            try:
                outline = CourseOutline.objects.get(
                    course_code=selected_code,
                    level=profile.level,
                    parsed=True
                )
                if outline.topics_json:
                    for week_topics in outline.topics_json.values():
                        if isinstance(week_topics, list):
                            available_topics.extend(week_topics)
            except CourseOutline.DoesNotExist:
                pass

            if not available_topics:
                try:
                    slide = SlideDocument.objects.get(
                        course_code=selected_code,
                        level=profile.level,
                        parsed=True
                    )
                    if slide.extracted_topics:
                        available_topics.extend(slide.extracted_topics)
                except SlideDocument.DoesNotExist:
                    pass

            if not available_topics:
                for week_topics in COURSE_OUTLINES.get(selected_code, {}).values():
                    available_topics.extend(week_topics)

            seen = set()
            unique_topics = []
            for t in available_topics:
                if t not in seen:
                    seen.add(t)
                    unique_topics.append(t)
            available_topics = unique_topics

        # This return is at the else block level — NOT inside if selected_code
        return render(request, "core/simulator/setup.html", {
            "mode": "voluntary",
            "timetable": timetable,
            "selected_code": selected_code,
            "available_topics": available_topics,
        })

def _call_simulator_model(prompt, max_output_tokens=4000, max_retries=3):
    """Call the simulator Gemini client with retry/backoff for transient
    errors (429 rate limit, 503 overloaded) — mirrors _generate_topic_lecture's
    retry behavior, which the simulator path was missing entirely."""
    last_error = None
    for attempt in range(max_retries):
        try:
            return simulator_client.models.generate_content(
                model="gemini-3.6-flash",
                contents=prompt,
                config=types.GenerateContentConfig(max_output_tokens=max_output_tokens),
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

def _start_simulator_test(request, profile, selections, mode, auto_entry=None):
    if mode == "auto":
        entry = auto_entry
        question_format = _detect_question_format(entry.course_code, profile.level)
        weeks_covered = min(entry.week_number - 1, 6)
        questions = _generate_test_questions(
            entry.course_code, entry.course_title, "Weeks 1-6 Review",
            profile.level, weeks_covered, question_format,
        )
        test_kwargs = dict(
            course_code=entry.course_code, course_title=entry.course_title,
            topic="Weeks 1-6 Review",
        )
    else:
        # Detect format per selected course; if they disagree, fall back to "mixed"
        # rather than silently picking one course's format for all of them.
        formats = {_detect_question_format(s["course_code"], profile.level) for s in selections}
        question_format = formats.pop() if len(formats) == 1 else "mixed"

        if len(selections) == 1:
            questions = _generate_test_questions(
                selections[0]["course_code"], selections[0]["course_title"],
                selections[0]["topic"], profile.level,
                selections[0]["weeks_covered"], question_format,
            )
        else:
            questions = _generate_multi_topic_test_questions(
                selections, profile.level, question_format,
            )

        test_kwargs = dict(
            course_code=selections[0]["course_code"] if len(selections) == 1 else "MIXED",
            course_title=selections[0]["course_title"] if len(selections) == 1 else ", ".join(s["course_code"] for s in selections),
            topic=selections[0]["topic"] if len(selections) == 1 else ", ".join(s["topic"] for s in selections),
        )

    test = SimulatorTest.objects.create(
        student=profile,
        mode=mode,
        question_format=question_format,
        week_number=selections[0].get("weeks_covered", 0) + 1 if selections else auto_entry.week_number,
        questions=questions,
        **test_kwargs,
    )
    return redirect("simulator_test", test_id=test.id)

@login_required
def topics_for_course_view(request):
    """Returns available topics for a course, as JSON — used by the
    voluntary setup page's per-row topic dropdowns."""
    if not hasattr(request.user, "profile"):
        return JsonResponse({"topics": []})

    profile = request.user.profile
    course_code = request.GET.get("course_code", "")
    available_topics = []

    if course_code:
        try:
            outline = CourseOutline.objects.get(
                course_code=course_code, level=profile.level, parsed=True
            )
            if outline.topics_json:
                for week_topics in outline.topics_json.values():
                    if isinstance(week_topics, list):
                        available_topics.extend(week_topics)
        except CourseOutline.DoesNotExist:
            pass

        if not available_topics:
            try:
                slide = SlideDocument.objects.get(
                    course_code=course_code, level=profile.level, parsed=True
                )
                if slide.extracted_topics:
                    available_topics.extend(slide.extracted_topics)
            except SlideDocument.DoesNotExist:
                pass

        if not available_topics:
            for week_topics in COURSE_OUTLINES.get(course_code, {}).values():
                available_topics.extend(week_topics)

        seen = set()
        unique_topics = []
        for t in available_topics:
            if t not in seen:
                seen.add(t)
                unique_topics.append(t)
        available_topics = unique_topics

    return JsonResponse({"topics": available_topics})


@login_required
def simulator_test_view(request, test_id):
    if not hasattr(request.user, "profile"):
        return redirect("onboarding")

    profile = request.user.profile
    test = get_object_or_404(SimulatorTest, id=test_id, student=profile)

    if test.status == "complete":
        return redirect("simulator_result", test_id=test.id)

    if request.method == "POST":
        # Collect all answers from the form
        answers = []
        for i in range(len(test.questions)):
            if test.question_format == "mcq":
                val = request.POST.get(f"answer_{i}", "")
                answers.append(int(val) if val != "" else -1)
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

    # Build grading input
    if test.question_format == "mcq":
        feedback_list = []
        total_marks = 0
        earned_marks = 0
        for i, q in enumerate(test.questions):
            q_marks = q.get("marks", 2)
            total_marks += q_marks
            student_answer = test.answers[i] if i < len(test.answers) else -1
            correct = student_answer == q.get("correct_index", 0)
            score = q_marks if correct else 0
            earned_marks += score
            chosen_text = q["options"][student_answer] if 0 <= student_answer < len(q["options"]) else "No answer"
            correct_text = q["options"][q.get("correct_index", 0)]
            topic_tag = f"[{q.get('topic')}] " if q.get("topic") else ""
            feedback_list.append({
                "score": score,
                "total_marks": q_marks,
                "feedback": f"{topic_tag}You chose: {chosen_text}. Correct answer: {correct_text}. {q.get('explanation', '')}",
                "correct": correct,
            })
    else:
        # Theory/calc — use AI to grade
        qa_text = ""
        for i, q in enumerate(test.questions):
            student_ans = test.answers[i] if i < len(test.answers) else "No answer provided"
            qa_text += f"\nQuestion {i+1} ({q.get('marks', 10)} marks) — {q.get('course_code', test.course_code)} / {q.get('topic', test.topic)}:\n{q['question']}\n"
            qa_text += f"Model Answer:\n{q.get('model_answer', '')}\n"
            qa_text += f"Student Answer:\n{student_ans}\n"
            qa_text += "---\n"

        grading_prompt = SIMULATOR_GRADING_PROMPT.format(questions_and_answers=qa_text,
            course_code=test.course_code,
            course_title=test.course_title,
            topic=test.topic,
        )

        try:
            grade_response = _call_simulator_model(grading_prompt, max_output_tokens=2000)
            feedback_list = _repair_and_parse_json_array(grade_response.text)
        except Exception as e:
            import traceback
            traceback.print_exc()
            messages.error(
                request,
                "Grading is taking longer than expected due to high AI demand — your answers are saved. Click below to retry."
            )
            return redirect("simulator_grade", test_id=test.id)

        total_marks = sum(q.get("marks", 10) for q in test.questions)
        earned_marks = sum(f.get("score", 0) for f in feedback_list)

    # Calculate percentage and grade
    percentage = round((earned_marks / total_marks) * 100, 1) if total_marks > 0 else 0
    grade, xp = _get_xp_and_grade(percentage)

    # Get overall feedback from AI
    student_name = profile.user.first_name or profile.user.username
    try:
        overall_response = _call_simulator_model(
            SIMULATOR_OVERALL_FEEDBACK_PROMPT.format(
                student_name=student_name,
                course_code=test.course_code,
                course_title=test.course_title,
                topic=test.topic,
                percentage=percentage,
                grade=grade,
            ),
            max_output_tokens=300,
        )
        overall_feedback = overall_response.text.strip()
    except Exception:
        overall_feedback = f"You scored {percentage}% — Grade {grade}. Keep studying and you'll improve!"

    # Save everything
    test.ai_feedback = feedback_list
    test.overall_feedback = overall_feedback
    test.percentage_score = percentage
    test.grade = grade
    test.xp_earned = xp
    test.status = "complete"
    test.completed_at = timezone.now()
    test.save()

    # Award XP
    profile.xp += xp
    profile.save()

    return redirect("simulator_result", test_id=test.id)


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
    saved_chunk_count = topic_session.chatmessage_set.filter(
        role="ai", is_pregenerated=True
    ).count()

    next_index = saved_chunk_count

    if next_index >= total_chunks:
        return JsonResponse({"action": "quiz_time"})

    raw_chunk_text = topic_session.chunks[next_index]
    is_last = (next_index == total_chunks - 1)

    clean_text, image_url = _process_chunk_image(
        raw_chunk_text, topic_session.session.course_code
    )

    ChatMessage.objects.create(
        topic_session=topic_session,
        role="ai",
        content=clean_text,
        image_url=image_url,
        is_pregenerated=True,
    )

    if topic_session.current_chunk_index != next_index:
        topic_session.current_chunk_index = next_index
        topic_session.save(update_fields=["current_chunk_index"])

    return JsonResponse({
        "action": "next_chunk",
        "chunk": clean_text,
        "image_url": image_url,
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

    # Save the student's message
    ChatMessage.objects.create(
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
    system_instruction = f"""You are Rovea, a brilliant AI lecturer for Petroleum and Gas Engineering students at UNILAG.

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
Keep it conversational. Max 5-6 paragraphs. Never re-teach the whole chunk unless they said "everything".
Never say "As an AI". Stay in character as Rovea."""

    def event_stream():
        full_reply = ""
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
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"
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
@require_POST
def run_topic_split_view(request, slide_id):
    """Runs (or resumes) the resumable topic split for a slide, then
    automatically backfills any topics that came out empty. Safe to
    click multiple times — it's resumable and idempotent."""
    slide = get_object_or_404(SlideDocument, id=slide_id)

    if not slide.extracted_topics or not slide.extracted_text.strip():
        messages.warning(request, "Can't split — no extracted topics or text saved for this slide.")
        return redirect("staff_portal")

    from core.slide_topic_extractor import split_slide_by_extracted_topics, retry_missing_topic_splits

    try:
        success, count = split_slide_by_extracted_topics(slide)
    except Exception as e:
        import traceback
        traceback.print_exc()
        messages.warning(request, f"Topic split failed: {str(e)} — any progress made was saved, click again to resume.")
        return redirect("staff_portal")

    empty_topics = list(slide.topic_chunks.filter(is_empty=True).values_list("topic_name", flat=True))

    if empty_topics:
        try:
            filled, filled_count = retry_missing_topic_splits(slide, topic_names=empty_topics)
            still_empty = list(slide.topic_chunks.filter(is_empty=True).values_list("topic_name", flat=True))
        except Exception as e:
            import traceback
            traceback.print_exc()
            messages.warning(request, f"Split completed ({count} topics) but backfill failed: {str(e)}")
            return redirect("staff_portal")

        if still_empty:
            messages.warning(
                request,
                f"Split complete — {count} topics saved, but {len(still_empty)} still have no "
                f"content after backfill: {', '.join(still_empty[:5])}"
                f"{'...' if len(still_empty) > 5 else ''}. Click 'Run Topic Split' again to retry, "
                f"or check the source slides for these topics manually."
            )
        else:
            messages.success(request, f"Split complete — all {count} topics have content. Ready to generate lectures.")
    else:
        messages.success(request, f"Split complete — all {count} topics have content. Ready to generate lectures.")

    return redirect("staff_portal")

@staff_required
def staff_preview_lesson_view(request, lesson_id):
    lesson = get_object_or_404(PreGeneratedLesson, id=lesson_id)
    lecture_html = markdown.markdown(lesson.content_chunk, extensions=["extra"])
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
    lecture_html = markdown.markdown(topic_session.lecture_content, extensions=["extra"])
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