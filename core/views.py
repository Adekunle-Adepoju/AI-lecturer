import os
import json
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
    PastQuestion, SimulatorTest, PreGeneratedLesson,   
)
from .prompt import SYSTEM_PROMPT, CHAT_SYSTEM_PROMPT, QUIZ_GENERATION_PROMPT
from functools import wraps
from .staff_forms import SlideUploadForm, CourseOutlineUploadForm, PastQuestionUploadForm, CourseDefinitionForm
from .prompt import SIMULATOR_QUESTION_PROMPT, SIMULATOR_GRADING_PROMPT, SIMULATOR_OVERALL_FEEDBACK_PROMPT



# ─── Gemini client ───────────────────────────────────────────────────────────────

client = genai.Client(api_key=settings.GEMINI_API_KEY_CHAT)
IMAGE_MARKER_RE = re.compile(r'\[IMAGE:\s*(.*?)\]', re.IGNORECASE)
simulator_client = genai.Client(api_key=settings.GEMINI_API_KEY_SIMULATOR)

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
                login(request, user)
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
            login(request, user)
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
    """Today's course in the rolling queue. Advances one position per
    Mon–Sat day elapsed; Sunday never advances and has no active course."""
    courses = list(profile.timetable.order_by("course_code"))
    if not courses:
        return None

    today = date.today()
    if profile.queue_date != today:
        if profile.queue_date is not None:
            days_advanced = 0
            d = profile.queue_date
            while d < today:
                d += timedelta(days=1)
                if d.weekday() != 6:  # Sunday = 6, doesn't count
                    days_advanced += 1
            profile.queue_position = (profile.queue_position + days_advanced) % len(courses)
        profile.queue_date = today
        profile.save(update_fields=["queue_position", "queue_date"])

    if today.weekday() == 6:  # Sunday — rest day, no course
        return None

    return courses[profile.queue_position % len(courses)]


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

TEACHING_ROUNDS_TARGET = 9   # target number of rounds to finish all topics — round 5 starts tests, round 9 is the teaching deadline

def _get_total_topics_for_course(course_code, level):
    """Full ordered topic list for a course. CourseOutline (the official
    syllabus, if uploaded) determines ordering first — slide decks aren't
    always arranged in teaching order. Slide-extracted topics are only a
    fallback for courses with no outline uploaded yet."""
    try:
        outline = CourseOutline.objects.get(course_code=course_code, level=level, parsed=True)
        if outline.topics_json:
            flat = []
            for week in sorted(outline.topics_json.keys(), key=lambda w: int(w)):
                flat.extend(outline.topics_json[week])
            if flat:
                return flat
    except (CourseOutline.DoesNotExist, ValueError):
        pass

    try:
        slide = SlideDocument.objects.get(course_code=course_code, level=level, parsed=True)
        if slide.extracted_topics:
            return list(slide.extracted_topics)
    except SlideDocument.DoesNotExist:
        pass

    flat = []
    for week in sorted(COURSE_OUTLINES.get(course_code, {}).keys()):
        flat.extend(COURSE_OUTLINES[course_code][week])
    if flat:
        return flat

    return ["Core Concepts", "Key Applications", "Problem Solving"]

def _get_topic_source_week(course_code, level, topic_name):
    """Find which CourseOutline week a topic belongs to, so we can fetch
    the matching SlideChunk — needed since Session.week_number now tracks
    teaching rounds, not literal outline weeks."""
    try:
        outline = CourseOutline.objects.get(course_code=course_code, level=level, parsed=True)
        for week_str, topics in outline.topics_json.items():
            if topic_name in topics:
                try:
                    return int(week_str)
                except ValueError:
                    continue
    except CourseOutline.DoesNotExist:
        pass
    return None


def _find_slide_content_for_topic(course_code, level, topic_name):
    """Targeted retrieval: find the specific week's SlideChunk this topic
    belongs to (small, ~15-20 pages) rather than searching the full deck
    on every call — keeps payload size and API cost down."""
    try:
        slide = SlideDocument.objects.get(course_code=course_code, level=level, parsed=True)
    except SlideDocument.DoesNotExist:
        return ""

    source_week = _get_topic_source_week(course_code, level, topic_name)
    if source_week is not None:
        chunk = slide.chunks.filter(week_number=source_week).first()
        if chunk and chunk.chunk_text:
            return chunk.chunk_text[:6000]

    # Fallback only — topic isn't mapped to a chunked week (no outline, or
    # chunking hasn't run yet). Small bounded search, not the whole deck.
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

def _find_slide_content_for_topic(course_code, level, topic_name, window=3000):
    """Search the slide deck's full transcript for content relevant to a
    specific topic, wherever it actually sits in the deck — decks aren't
    always ordered to match the official course outline, so we search by
    content rather than trusting position/week number."""
    try:
        slide = SlideDocument.objects.get(course_code=course_code, level=level, parsed=True)
    except SlideDocument.DoesNotExist:
        return ""

    text = slide.extracted_text
    if not text:
        return ""

    keywords = [w for w in re.findall(r"[A-Za-z]{4,}", topic_name)]
    if not keywords:
        return ""

    lower_text = text.lower()
    match_pos = None
    for kw in keywords:
        idx = lower_text.find(kw.lower())
        if idx != -1:
            match_pos = idx
            break

    if match_pos is None:
        return ""  # topic not found in the deck — AI teaches from its own knowledge instead

    start = max(0, match_pos - 500)
    end = min(len(text), match_pos + window)
    return text[start:end]


def _topics_per_turn_for_course(course_code, level):
    total_topics = len(_get_total_topics_for_course(course_code, level))
    return min(3, max(1, math.ceil(total_topics / TEACHING_ROUNDS_TARGET)))


def _get_topics_for_week(course_code, level, week_number):
    """'week_number' here tracks turn count for this course, not a literal
    calendar week — how many topic groups it's completed so far. Group size
    scales to the course's total topic count so light courses move at
    1/turn and bulky ones at up to 3/turn, aiming to finish around the
    same number of turns regardless of bulk."""
    all_topics = _get_total_topics_for_course(course_code, level)
    per_turn = _topics_per_turn_for_course(course_code, level)

    start = (week_number - 1) * per_turn
    end = start + per_turn
    topics = all_topics[start:end]
    if not topics and all_topics:
        topics = all_topics[-per_turn:]
    if not topics:
        topics = ["Core Concepts", "Key Applications", "Problem Solving"]
    return topics


def _get_past_questions_for_topic(course_code, level, topic_name, limit=3):
    """Fetch past questions relevant to this topic, for blending into quizzes"""
    from .models import PastQuestion
    relevant = []
    past_qs = PastQuestion.objects.filter(course_code=course_code, level=level, parsed=True)
    
    for pq in past_qs:
        for q in pq.parsed_questions:
            hint = q.get("topic_hint", "").lower()
            if any(word.lower() in hint for word in topic_name.split()):
                relevant.append(q)
                
    # If no topic match, just grab random ones from the course as general style reference
    if not relevant:
        all_questions = []
        for pq in past_qs:
            all_questions.extend(pq.parsed_questions)
        relevant = all_questions
        
    random.shuffle(relevant)
    return relevant[:limit]


def _generate_topic_lecture(course_code, course_title, topic_name, week, level, student_name, topic_index=0, slide_text=""):
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
        f"Topic number: {topic_index + 1} of 3 in this session\n"
        f"Week: {week} of 10\n"
        f"STRICT INSTRUCTION: Teach ONLY '{topic_name}'. Do not teach any other topic. "
        f"Follow the course outline strictly. This is the exact topic scheduled for this session."
        f"{slide_text}"
        f"{past_q_text}"
    )

    last_error = None
    for model in ["gemini-3.7-flash", "gemini-3.6-flash"]:
        for attempt in range(3):
            try:
                response = client.models.generate_content(
                    model=model,
                    contents=user_message,
                    config=types.GenerateContentConfig(
                        system_instruction=SYSTEM_PROMPT,
                        max_output_tokens=8000,
                    ),
                )
                return response.text
            except Exception as e:
                last_error = e
                error_str = str(e)
                is_transient = any(marker in error_str for marker in [
                    "503", "429", "UNAVAILABLE",
                    "disconnected", "Disconnected",
                    "timeout", "Timeout", "DEADLINE_EXCEEDED",
                    "ConnectionError", "RemoteDisconnected",
                ])

                if is_transient and attempt < 2:
                    print(f"[{course_code}] {model} hit a transient error (attempt {attempt + 1}/3) — retrying in 10s...")
                    time.sleep(10)
                    continue

                print(f"[{course_code}] {model} failed: {e}. {'Trying next model...' if model != 'gemini-3.6-flash' else ''}")
                break

    raise RuntimeError(f"All models and retries failed for lecture generation on {course_code} — {topic_name}: {last_error}")


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
        start = clean.find("{")
        end = clean.rfind("}")
        if start != -1 and end != -1 and end > start:
            clean = clean[start:end + 1]

        try:
            quiz_data = json.loads(clean)
            question = quiz_data.get("question", "")
            options = quiz_data.get("options", options)
            correct_index = quiz_data.get("correct_index", 0)
            explanation = quiz_data.get("explanation", "")
        except json.JSONDecodeError:
            # Fallback: try regex to pull out the question and options manually
            q_match = re.search(r'"question"\s*:\s*"(.*?)"\s*,\s*"options"', clean, re.DOTALL)
            if q_match:
                question = q_match.group(1).strip()

            opt_match = re.search(r'"options"\s*:\s*\[(.*?)\]', clean, re.DOTALL)
            if opt_match:
                raw_opts = opt_match.group(1)
                found_opts = re.findall(r'"(.*?)"', raw_opts)
                if found_opts:
                    options = found_opts

            idx_match = re.search(r'"correct_index"\s*:\s*(\d+)', clean)
            if idx_match:
                correct_index = int(idx_match.group(1))

            exp_match = re.search(r'"explanation"\s*:\s*"(.*?)"\s*\}', clean, re.DOTALL)
            if exp_match:
                explanation = exp_match.group(1).strip()

    return {
        "intro": intro_raw,
        "lecture": lecture_raw,
        "question": question,
        "options": options,
        "correct_index": correct_index,
        "explanation": explanation,
    }

def _split_into_pages(text, target_chars=700):
    """Break a long pre-generated lecture into digestible pages, splitting
    on paragraph boundaries only — never mid-sentence. Content itself is
    completely unchanged, just grouped for sequential display."""
    paragraphs = re.split(r"\n\s*\n", text.strip())
    pages = []
    current = ""
    for para in paragraphs:
        if not para.strip():
            continue
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
        correct_index = quiz_data.get("correct_index", 0)
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
        idx_match = re.search(r'"correct_index"\s*:\s*(\d+)', clean)
        if idx_match:
            correct_index = int(idx_match.group(1))
        exp_match = re.search(r'"explanation"\s*:\s*"(.*?)"\s*\}', clean, re.DOTALL)
        if exp_match:
            explanation = exp_match.group(1).strip()

    if not question:
        raise ValueError("No question could be parsed from quiz response")

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
        return _render_chat_session(request, entry, profile, existing_session)

    if action == "show_lecture":
        topic_session_id = request.POST.get("topic_session_id")
        topic_session = get_object_or_404(TopicSession, id=topic_session_id, session__student=profile)
        session = topic_session.session
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

    return redirect("session_view", course_code=course_code)

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
            slide_text + outline_text
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
                slide_text + outline_text
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
    """Render the live chat page for the current topic.
    Checks for pre-generated content first — serves from DB instantly if available.
    Falls back to live Gemini only if no published lesson exists."""

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
    pregenerated_intro = None
    pregenerated_content = None
    is_pregenerated = False

    try:
        lesson = PreGeneratedLesson.objects.get(
            course__course_code=session.course_code,
            week_number=session.week_number,
            topic_title=topic_name,
            is_published=True,
        )
        pregenerated_content = lesson.content_chunk
        is_pregenerated = True

        # If topic session has no stored content yet, save the pre-generated
        # content into it so review, history, and quiz generation all work
        if not topic_session.lecture_content:
            topic_session.lecture_content = pregenerated_content
            topic_session.save()

    except PreGeneratedLesson.DoesNotExist:
        pass

    # ── Build existing chat history ───────────────────────────────────────────
    has_started_chat = topic_session.chatmessage_set.filter(role="ai").exists()
    existing_messages = list(
        ChatMessage.objects.filter(topic_session__session=session)
        .order_by("topic_session__topic_index", "created_at")
        .values("role", "content", "image_url", "is_pregenerated", topic_name=F("topic_session__topic_name"))
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
        "pregenerated_content": pregenerated_content,
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

    return render(request, "core/result.html", {
        "entry": entry,
        "session": session,
        "topic_session": topic_session,
        "correct": correct,
        "xp_earned": xp,
        "feedback": feedback,
        "explanation": topic_session.quiz_explanation,
        "next_topic_index": next_index,
        "next_topic_name": session.topics[next_index] if not is_last_topic else None,
        "is_last_topic": is_last_topic,
        "total_topics": total_topics,
        "topic_number": topic_session.topic_index + 1,
    })


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
    return render(request, "core/timetable.html", {"timetable": timetable, "profile": profile})


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

    available_courses = CourseDefinition.objects.filter(
        level=profile.level,
        semester=profile.semester,
        school=profile.school,
        department=profile.department,
    )

    active_course_codes = TimetableEntry.objects.filter(
        student=profile,
        course_code__in=available_courses.values_list('course_code', flat=True)
    ).values_list('course_code', flat=True)

    if request.method == "POST":
        selected_codes = request.POST.getlist('selected_courses')

        # Remove timetable/session rows for anything unchecked
        TimetableEntry.objects.filter(
            student=profile,
            course_code__in=available_courses.values_list('course_code', flat=True)
        ).exclude(course_code__in=selected_codes).delete()

        Session.objects.filter(
            student=profile,
            course_code__in=available_courses.values_list('course_code', flat=True)
        ).exclude(course_code__in=selected_codes).delete()

        # Add timetable rows for anything newly checked
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

    context = {
        'courses': available_courses,
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
    """Staff portal home — overview of uploaded materials"""
    slides = SlideDocument.objects.all().order_by("-uploaded_at")[:10]
    outlines = CourseOutline.objects.all().order_by("-uploaded_at")[:10]
    past_questions = PastQuestion.objects.all().order_by("-uploaded_at")[:10]
    courses = CourseDefinition.objects.all().order_by("level", "semester", "course_code")

    return render(request, "core/staff/portal.html", {
        "slides": slides,
        "outlines": outlines,
        "past_questions": past_questions,
        "courses": courses,
    })


@staff_required
def staff_upload_slide_view(request):
    if request.method == "POST":
        print("POST received")
        form = SlideUploadForm(request.POST, request.FILES)
        print(f"Form valid: {form.is_valid()}")
        print(f"Form errors: {form.errors}")
        if form.is_valid():
            slide = form.save()
            try:
                from .slide_topic_extractor import _parse_slide_document
                _parse_slide_document(slide)
                messages.success(request, f"Slide uploaded and {len(slide.extracted_topics)} topics extracted.")
            except Exception as e:
                messages.warning(request, f"Slide saved but topic extraction failed: {str(e)}")
            return redirect("staff_portal")
    else:
        form = SlideUploadForm()
    return render(request, "core/staff/upload_form.html", {
        "form": form,
        "title": "Upload Course Slide",
        "description": "Upload the full course slide deck (PDF, DOCX or PPTX). Topics will be extracted automatically.",
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
    """Retry AI topic extraction for a slide that already has text saved."""
    slide = get_object_or_404(SlideDocument, id=slide_id)

    if not slide.extracted_text:
        messages.warning(request, "Can't retry — no extracted text saved for this slide.")
        return redirect("staff_portal")

    try:
        from .slide_topic_extractor import extract_topics_from_slide
        topics = extract_topics_from_slide(slide.course_code, slide.course_title, slide.extracted_text)
        slide.extracted_topics = topics
        slide.save()
        messages.success(request, f"Topics extracted successfully — {len(topics)} topics found.")
    except Exception as e:
        import traceback
        traceback.print_exc()
        messages.warning(request, f"Retry failed: {str(e)}")

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
                model="gemini-3.7-flash",
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

        # Get slide text for context
        slide_text = ""
        try:
            slide_doc = SlideDocument.objects.get(
                course_code=course.course_code,
                level=course.level,
            )
            if slide_doc.extracted_text:
                slide_text = f"\n\nLECTURER SLIDES (focus only on content relevant to this topic):\n{slide_doc.extracted_text[:6000]}"
        except SlideDocument.DoesNotExist:
            pass

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
            # Skip if content already exists (published or draft) — don't
            # burn a second API call regenerating something we already have.
            existing = PreGeneratedLesson.objects.filter(
                course=course,
                week_number=week_number,
                topic_title=topic,
            ).first()

            if existing and existing.content_chunk.strip():
                continue

            try:
                full_text = _generate_topic_lecture(
                    course.course_code,
                    course.course_title,
                    topic,
                    week_number,
                    course.level,
                    student_name="Student",
                    topic_index=i,
                    slide_text=slide_text + outline_text,
                )

                parsed = _parse_lecture(full_text)
                content = parsed["lecture"] if parsed["lecture"] else full_text

                PreGeneratedLesson.objects.update_or_create(
                    course=course,
                    week_number=week_number,
                    topic_title=topic,
                    defaults={
                        "content_chunk": content,
                        "is_published": False,
                    }
                )
                generated_count += 1

            except Exception as e:
                errors.append(f"{topic}: {str(e)}")

        if generated_count:
            messages.success(request, f"Generated {generated_count} lesson(s) for {course.course_code} Week {week_number}. Review and publish them in the admin panel.")
        if errors:
            for error in errors:
                messages.warning(request, f"Failed: {error}")

        return redirect("staff_pregenerate_lessons")

    # GET — show existing pre-generated lessons
    lessons = PreGeneratedLesson.objects.select_related("course").order_by(
        "course__level", "course__course_code", "week_number", "topic_title"
    )

    return render(request, "core/staff/pregenerate_lessons.html", {
        "courses": courses,
        "lessons": lessons,
        "week_range": range(1, 11),  # Assuming 10 weeks max
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

# ─── Simulator ─────────────────────────────────────────────────────────────────

def _get_xp_and_grade(percentage):
    if percentage >= 70:
        return "A", 150
    elif percentage >= 50:
        return "B", 100
    elif percentage >= 40:
        return "C", 50
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
        for q in pq.parsed_questions:
            hint = q.get("topic_hint", "").lower()
            if any(word.lower() in hint for word in topic.split()):
                relevant.append(q)
    if not relevant:
        all_q = []
        for pq in pqs:
            all_q.extend(pq.parsed_questions)
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

    prompt = SIMULATOR_QUESTION_PROMPT.format(
        course_code=course_code,
        course_title=course_title,
        topic=topic,
        question_format=question_format,
        weeks_covered=weeks_covered,
        num_questions=num_questions,
        past_q_reference=past_ref,
    )

    response = simulator_client.models.generate_content(
        model="gemini-3.7-flash",
        contents=prompt,
        config=types.GenerateContentConfig(max_output_tokens=4000),
    )

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

    if mode == "auto":
        # For auto mode, course is determined by which courses are at week 7
        course_code = request.GET.get("course_code") or request.POST.get("course_code")
        entry = get_object_or_404(TimetableEntry, student=profile, course_code=course_code)

        # Check not already completed
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
            return _start_simulator_test(request, profile, entry, mode="auto", topic="Weeks 1-6 Review")

        return render(request, "core/simulator/setup.html", {
            "mode": "auto",
            "entry": entry,
            "topic": "All topics from Weeks 1 to 6",
        })

    else:
        timetable = profile.timetable.all()

        if request.method == "POST":
            course_code = request.POST.get("course_code")
            topic = request.POST.get("topic", "").strip()
            if not course_code or not topic:
                messages.error(request, "Please select a course and enter a topic.")
                return redirect("simulator_setup", mode="voluntary")
            entry = get_object_or_404(TimetableEntry, student=profile, course_code=course_code)
            return _start_simulator_test(request, profile, entry, mode="voluntary", topic=topic)

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


def _start_simulator_test(request, profile, entry, mode, topic):
    question_format = _detect_question_format(entry.course_code, profile.level)
    weeks_covered = min(entry.week_number - 1, 6) if mode == "auto" else entry.week_number - 1

    try:
        questions = _generate_test_questions(
            entry.course_code, entry.course_title,
            topic, profile.level,
            weeks_covered, question_format,
        )
    except Exception as e:
        error_str = str(e)
        if "503" in error_str or "UNAVAILABLE" in error_str:
            messages.error(request, "The AI is currently busy — this is temporary. Please wait a moment and try again.")
        elif "429" in error_str or "quota" in error_str.lower():
            messages.error(request, "Daily AI limit reached. Please try again later.")
        else:
            messages.error(request, f"Could not generate test questions: {error_str}")
        return redirect("simulator_home")

    test = SimulatorTest.objects.create(
        student=profile,
        course_code=entry.course_code,
        course_title=entry.course_title,
        topic=topic,
        mode=mode,
        question_format=question_format,
        week_number=entry.week_number,
        questions=questions,
    )

    return redirect("simulator_test", test_id=test.id)


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
            feedback_list.append({
                "score": score,
                "total_marks": q_marks,
                "feedback": f"You chose: {chosen_text}. Correct answer: {correct_text}. {q.get('explanation', '')}",
                "correct": correct,
            })
    else:
        # Theory/calc — use AI to grade
        qa_text = ""
        for i, q in enumerate(test.questions):
            student_ans = test.answers[i] if i < len(test.answers) else "No answer provided"
            qa_text += f"\nQuestion {i+1} ({q.get('marks', 10)} marks):\n{q['question']}\n"
            qa_text += f"Model Answer:\n{q.get('model_answer', '')}\n"
            qa_text += f"Student Answer:\n{student_ans}\n"
            qa_text += "---\n"

        grading_prompt = SIMULATOR_GRADING_PROMPT.format(
            course_code=test.course_code,
            course_title=test.course_title,
            topic=test.topic,
            questions_and_answers=qa_text,
        )

        try:
            grade_response = simulator_client.models.generate_content(
                model="gemini-3.7-flash",
                contents=grading_prompt,
                config=types.GenerateContentConfig(max_output_tokens=2000),
            )
            clean = grade_response.text.replace("```json", "").replace("```", "").strip()
            start = clean.find("[")
            end = clean.rfind("]")
            if start != -1 and end != -1:
                clean = clean[start:end + 1]
            feedback_list = json.loads(clean)
        except Exception as e:
            messages.error(request, f"Grading failed: {str(e)}")
            return redirect("simulator_test", test_id=test.id)

        total_marks = sum(q.get("marks", 10) for q in test.questions)
        earned_marks = sum(f.get("score", 0) for f in feedback_list)

    # Calculate percentage and grade
    percentage = round((earned_marks / total_marks) * 100, 1) if total_marks > 0 else 0
    grade, xp = _get_xp_and_grade(percentage)

    # Get overall feedback from AI
    student_name = profile.user.first_name or profile.user.username
    try:
        overall_response = simulator_client.models.generate_content(
            model="gemini-3.7-flash",
            contents=SIMULATOR_OVERALL_FEEDBACK_PROMPT.format(
                student_name=student_name,
                course_code=test.course_code,
                course_title=test.course_title,
                topic=test.topic,
                percentage=percentage,
                grade=grade,
            ),
            config=types.GenerateContentConfig(max_output_tokens=300),
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