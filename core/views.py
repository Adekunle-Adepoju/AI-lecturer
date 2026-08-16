import os
import json
from urllib import request
import markdown
import random
from datetime import date, timedelta

from django.contrib import messages
from django.contrib.auth import login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import AuthenticationForm
from django.conf import settings
from django.shortcuts import render, redirect, get_object_or_404
from django.utils import timezone
from django.views.decorators.http import require_POST
import anthropic
from django.http import StreamingHttpResponse
from django.http import JsonResponse

from .forms import SignupForm, OnboardingForm, ProfileEditForm, ElectiveSelectionForm
from .models import (
    StudentProfile, TimetableEntry, Session, TopicSession, ChatMessage,
    SlideDocument, CourseOutline, COURSES, COURSE_OUTLINES, CourseDefinition, PastQuestion,
    
)
from .prompt import SYSTEM_PROMPT, CHAT_SYSTEM_PROMPT
from functools import wraps
from .staff_forms import SlideUploadForm, CourseOutlineUploadForm, PastQuestionUploadForm, CourseDefinitionForm



# ─── Gemini client ───────────────────────────────────────────────────────────────

client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from env

def _build_history(topic_session):
    """Convert saved ChatMessages into Claude message format."""
    history = []
    messages = list(topic_session.chatmessage_set.order_by("created_at"))
    for msg in messages[:-1]:
        role = "user" if msg.role == "user" else "assistant"
        history.append({"role": role, "content": msg.content})
    return history

def chat_message_view(request):
    topic_session_id = request.POST.get("topic_session_id")
    user_message = request.POST.get("message")
    topic_session = get_object_or_404(TopicSession, id=topic_session_id)

    is_start_trigger = (user_message == "__START__")

    if not is_start_trigger:
        ChatMessage.objects.create(topic_session=topic_session, role="user", content=user_message)

    slide_context = ""
    try:
        slide_doc = SlideDocument.objects.get(
            course_code=topic_session.session.course_code,
            level=topic_session.session.student.level,
        )
        slide_context = slide_doc.extracted_text[:6000]
    except SlideDocument.DoesNotExist:
        pass

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

    messages = history + [{"role": "user", "content": message_to_send}]

    def event_stream():
        full_reply = ""
        try:
            with client.messages.stream(
                model="claude-sonnet-5",
                max_tokens=2048,
                system=system_instruction,
                messages=messages,
            ) as stream:
                for text in stream.text_stream:
                    full_reply += text
                    yield f"data: {json.dumps({'chunk': text})}\n\n"
        except Exception as e:
            import traceback
            traceback.print_exc()
            yield f"data: {json.dumps({'error': str(e)})}\n\n"
            return

        is_complete = "TOPIC_COMPLETE" in full_reply
        clean_reply = full_reply.replace("TOPIC_COMPLETE", "").strip()
        ChatMessage.objects.create(topic_session=topic_session, role="ai", content=clean_reply)

        yield f"data: {json.dumps({'done': True, 'topic_complete': is_complete})}\n\n"

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
                total_weeks=15,
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
            total_weeks=15,
        ))
    TimetableEntry.objects.bulk_create(entries)


# ─── Dashboard ─────────────────────────────────────────────────────────────────

@login_required
def dashboard_view(request):
    if not hasattr(request.user, "profile"):
        return redirect("onboarding")

    profile = request.user.profile
    courses = COURSES.get(profile.level, {}).get(profile.semester, [])
    timetable = profile.timetable.all()
    recent_sessions = profile.sessions.all()[:5]
    leaderboard = StudentProfile.objects.select_related("user").order_by("-xp")[:10]
    sessions_done = profile.sessions.count()
    
    today = timezone.now().strftime("%a")  # Safe cross-platform timezone management
    todays_courses = profile.timetable.filter(day=today, is_completed=False)
    incomplete_sessions = {
        s.course_code: s
        for s in Session.objects.filter(student=profile, is_complete=False)
    }

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
    })


# ─── Helpers ───────────────────--------------------------------───────────────

def _get_topics_for_week(course_code, level, week_number):
    try:
        slide = SlideDocument.objects.get(course_code=course_code, level=level, parsed=True)
        if slide.extracted_topics:
            start = (week_number - 1) * 3
            end = start + 3
            topics = slide.extracted_topics[start:end]
            if topics:
                return topics
            if len(slide.extracted_topics) >= 3:
                return slide.extracted_topics[-3:]
    except SlideDocument.DoesNotExist:
        pass

    # Fallback — hardcoded outline
    topics = COURSE_OUTLINES.get(course_code, {}).get(week_number, [])
    if not topics:
        topics = ["Core Concepts", "Key Applications", "Problem Solving"]
    return topics[:3]


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
        f"Week: {week} of 15\n"
        f"STRICT INSTRUCTION: Teach ONLY '{topic_name}'. Do not teach any other topic. "
        f"Follow the course outline strictly. This is the exact topic scheduled for this session."
        f"{slide_text}"
        f"{past_q_text}"
    )

    response = client.messages.create(
        model="claude-sonnet-5",
        max_tokens=8000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
    )

    return response.content[0].text


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


# ─── Session ───────────────────────────────────────────────────────────────────

@login_required
def session_view(request, course_code):
    if not hasattr(request.user, "profile"):
        return redirect("onboarding")

    profile = request.user.profile
    entry = get_object_or_404(TimetableEntry, student=profile, course_code=course_code)

    if request.method == "GET":
        topics = _get_topics_for_week(course_code, request.user.profile.level, entry.week_number)
        return render(request, "core/session.html", {
            "entry": entry,
            "chat_mode": False,
            "upcoming_topics": topics,
        })

    action = request.POST.get("action")

    if action == "start":
        existing_session = Session.objects.filter(
            student=profile,
            course_code=course_code,
            week_number=entry.week_number,
            is_complete=False,
        ).first()

        if existing_session:
            current_index = existing_session.current_topic_index
            topic_session = existing_session.topic_sessions.filter(
                topic_index=current_index, is_complete=False,
            ).first()
            if not topic_session:
                topic_session = TopicSession.objects.create(
                    session=existing_session,
                    topic_name=existing_session.topics[current_index],
                    topic_index=current_index,
                )
        else:
            topics = _get_topics_for_week(course_code, profile.level, entry.week_number)
            existing_session = Session.objects.create(
                student=profile,
                course_code=course_code,
                course_title=entry.course_title,
                week_number=entry.week_number,
                topics=topics,
                current_topic_index=0,
            )
            topic_session = TopicSession.objects.create(
                session=existing_session,
                topic_name=topics[0],
                topic_index=0,
            )

        has_started_chat = topic_session.chatmessage_set.filter(role="ai").exists()
        existing_messages = list(topic_session.chatmessage_set.order_by("created_at").values("role", "content"))

        return render(request, "core/session.html", {
            "entry": entry,
            "session": existing_session,
            "topic_session": topic_session,
            "topic_number": topic_session.topic_index + 1,
            "total_topics": len(existing_session.topics),
            "topic_name": topic_session.topic_name,
            "chat_mode": True,
            "has_started_chat": has_started_chat,
            "existing_messages_json": json.dumps(existing_messages),
        })

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

    try:
        slide_doc = SlideDocument.objects.get(
            course_code=session.course_code,
            level=profile.level,
            week_number=session.week_number,
        )
        if slide_doc.extracted_text:
            slide_text = f"\n\nLECTURER SLIDES (focus only on content relevant to this topic):\n{slide_doc.extracted_text[:6000]}"
    except SlideDocument.DoesNotExist:
        pass

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
    entry = get_object_or_404(TimetableEntry, student=profile, course_code=session.course_code)

    session.current_topic_index = next_index
    session.save()

    topic_name = session.topics[next_index]
    return _teach_topic(request, session, entry, profile, topic_name, next_index)


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

    if request.method == "POST":
        form = ProfileEditForm(request.POST, instance=profile, user=request.user)
        if form.is_valid():
            form.save()
            messages.success(request, "Profile updated successfully.")
            return redirect("dashboard")
    else:
        form = ProfileEditForm(instance=profile, user=request.user)

    return render(request, "core/profile_edit.html", {"form": form})


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
    user = request.user
    
    # 1. PROFILE CHECK & FALLBACK FOR ADMINS
    if hasattr(user, 'studentprofile'):
        profile = user.studentprofile
        current_level = profile.level
        current_semester = profile.semester
    else:
        # Fallback parameters if logged in as an Admin/Superuser without a profile row
        current_level = 300
        current_semester = 1
        profile = None  # Flagged so we know not to write to TimetableEntry later

    # 2. DIFFERENTIATING COMPULSORY VS ELECTIVES
    # We query the CourseOutline table filtering by the user's current track
    available_courses = CourseOutline.objects.filter(
        level=current_level, 
        semester=current_semester
    )
    
    # Django splits them cleanly based on your 'is_compulsory' boolean model column
    compulsory_courses = available_courses.filter(is_compulsory=True)
    elective_courses = available_courses.filter(is_compulsory=False)
    
    # 3. TRACKING CURRENTLY ACTIVE ELECTIVES
    if profile:
        active_elective_codes = TimetableEntry.objects.filter(
            student=profile,
            course_code__in=elective_courses.values_list('course_code', flat=True)
        ).values_list('course_code', flat=True)
    else:
        # For Admin testing, pre-select nothing or everything so the page renders normally
        active_elective_codes = []

    # 4. POST PROCESSING (SAVING SELECTIONS)
    if request.method == "POST":
        if not profile:
            messages.warning(request, "Oga Admin, choices weren't saved because your account doesn't have a Student Profile attached.")
            return redirect('dashboard')
            
        selected_elective_codes = request.POST.getlist('selected_electives')
        
        # Drop elective slots that were unchecked
        TimetableEntry.objects.filter(
            student=profile, 
            course_code__in=elective_courses.values_list('course_code', flat=True)
        ).exclude(course_code__in=selected_elective_codes).delete()
        
        Session.objects.filter(
            student=profile, 
            course_code__in=elective_courses.values_list('course_code', flat=True)
        ).exclude(course_code__in=selected_elective_codes).delete()
        
        # Provision newly checked electives
        for code in selected_elective_codes:
            course = elective_courses.get(course_code=code)
            TimetableEntry.objects.get_or_create(
                student=profile,
                course_code=course.course_code,
                course_title=course.course_title,
                defaults={
                    'day': 'Wednesday', 
                    'time': '12:00 PM',
                    'week_number': 1,
                    'total_weeks': 12
                }
            )
        
        messages.success(request, "Your semester course window has been updated successfully!")
        return redirect('dashboard')

    context = {
        'compulsory_courses': compulsory_courses,
        'elective_courses': elective_courses,
        'active_elective_codes': active_elective_codes,
        'is_admin_testing': (profile is None)
    }
    return render(request, 'core/manage_courses.html', context)

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
        form = SlideUploadForm(request.POST, request.FILES)
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

@require_POST
def chat_next_topic_view(request):
    topic_session_id = request.POST.get("topic_session_id")
    topic_session = get_object_or_404(TopicSession, id=topic_session_id)
    return JsonResponse({"redirect": f"/quiz/{topic_session.id}/"})