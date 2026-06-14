import json
import markdown
from datetime import date, timedelta

from django.contrib import messages
from django.contrib.auth import login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import AuthenticationForm
from django.conf import settings
from django.shortcuts import render, redirect, get_object_or_404
from django.utils import timezone
from django.views.decorators.http import require_POST
from groq import Groq

from .forms import SignupForm, OnboardingForm
from .models import StudentProfile, TimetableEntry, Session, TopicSession, SlideDocument, COURSES
from .prompt import SYSTEM_PROMPT, TOPIC_GENERATOR_PROMPT


# ─── Groq client ───────────────────────────────────────────────────────────────

def get_groq_client():
    return Groq(api_key=settings.GROQ_API_KEY)


# ─── Auth views ────────────────────────────────────────────────────────────────

def signup_view(request):
    if request.user.is_authenticated:
        return redirect("dashboard")
    if request.method == "POST":
        form = SignupForm(request.POST)
        if form.is_valid():
            user = form.save()
            login(request, user)
            return redirect("onboarding")
    else:
        form = SignupForm()
    return render(request, "core/signup.html", {"form": form})


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

def _generate_timetable(profile):
    courses = COURSES.get(profile.level, {}).get(profile.semester, [])
    days = ["Mon", "Tue", "Wed", "Thu", "Fri"]
    times = ["09:00", "11:00"]

    TimetableEntry.objects.filter(student=profile).delete()

    entries = []
    for i, course in enumerate(courses):
        day = days[i % len(days)]
        # First 5 courses get 09:00, next 5 get 11:00
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
    today_entry = profile.timetable.filter(is_completed=False).first()
    sessions_done = profile.sessions.count()

    return render(request, "core/dashboard.html", {
        "profile": profile,
        "courses": courses,
        "timetable": timetable,
        "recent_sessions": recent_sessions,
        "leaderboard": leaderboard,
        "today_entry": today_entry,
        "sessions_done": sessions_done,
    })


# ─── Helpers ───────────────────────────────────────────────────────────────────

def _generate_topics(course_code, course_title, week, level):
    """Ask Groq to generate 3 topics for this course/week"""
    client = get_groq_client()
    response = client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[
            {"role": "system", "content": TOPIC_GENERATOR_PROMPT},
            {"role": "user", "content": f"Course: {course_code} — {course_title}\nLevel: {level}L\nWeek: {week} of 15\nGenerate 3 specific topics to teach this week."},
        ],
    )
    raw = response.choices[0].message.content.strip()
    raw = raw.replace("```json", "").replace("```", "").strip()
    topics = json.loads(raw)
    return topics[:3]


def _generate_topic_lecture(course_code, course_title, topic_name, week, level, student_name, topic_index=0, slide_text=""):
    """Ask Groq to teach one topic in full"""
    client = get_groq_client()
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
)
    response = client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        max_tokens=8000,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ],
    )
    return response.choices[0].message.content


def _parse_lecture(full_text):
    """Split AI response into intro, lecture, quiz parts"""
    if "---LECTURE---" in full_text:
        parts = full_text.split("---LECTURE---")
        intro_raw = parts[0].replace("---INTRO---", "").strip()
        lecture_raw = parts[1].strip()
    else:
        intro_raw = full_text[:500]
        lecture_raw = full_text

    quiz_parts = lecture_raw.split("Quiz time")
    lecture_only = quiz_parts[0].strip()
    quiz_raw = quiz_parts[1].strip() if len(quiz_parts) > 1 else ""

    lines = [l.strip() for l in quiz_raw.split("\n") if l.strip()]
    question_lines = []
    options = []
    for line in lines:
        if len(line) > 1 and line[0] in "ABCD" and line[1] == ".":
            options.append(line)
        elif not options:
            question_lines.append(line)
    question = " ".join(question_lines).strip()

    if not options:
        options = ["Option A", "Option B", "Option C", "Option D"]

    correct_index = 0
    for i, opt in enumerate(options):
        if "✓" in opt or "(correct)" in opt.lower():
            correct_index = i
            options[i] = opt.replace("✓", "").replace("(correct)", "").strip()

    return {
        "intro": intro_raw,
        "lecture": lecture_only,
        "question": question,
        "options": options,
        "correct_index": correct_index,
    }


# ─── Session ───────────────────────────────────────────────────────────────────

@login_required
def session_view(request, course_code):
    if not hasattr(request.user, "profile"):
        return redirect("onboarding")

    profile = request.user.profile
    entry = get_object_or_404(TimetableEntry, student=profile, course_code=course_code)

    if request.method == "GET":
        return render(request, "core/session.html", {"entry": entry})

    action = request.POST.get("action")

    if action == "start":
        try:
            topics = _generate_topics(
                course_code, entry.course_title,
                entry.week_number, profile.level
            )
        except Exception as e:
            return render(request, "core/session.html", {
                "entry": entry,
                "error": f"Could not generate topics: {str(e)}",
            })

        session = Session.objects.create(
            student=profile,
            course_code=course_code,
            course_title=entry.course_title,
            week_number=entry.week_number,
            topics=topics,
            current_topic_index=0,
        )

        return _teach_topic(request, session, entry, profile, topics[0], 0)

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

    return redirect("dashboard")


def _teach_topic(request, session, entry, profile, topic_name, topic_index):
    """Generate and show intro for a topic"""
    from .models import CourseOutline

    slide_text = ""
    outline_text = ""

    # Get slide for this week
    try:
        slide_doc = SlideDocument.objects.get(
            course_code=session.course_code,
            week_number=session.week_number
        )
        if slide_doc.extracted_text:
            slide_text = f"\n\nLECTURER SLIDES FOR THIS WEEK:\n{slide_doc.extracted_text[:4000]}"
    except SlideDocument.DoesNotExist:
        pass

    # Get course outline context
    try:
        outline = CourseOutline.objects.get(
            course_code=session.course_code,
            level=profile.level,
            parsed=True
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

    topic_session = TopicSession.objects.create(
        session=session,
        topic_name=topic_name,
        topic_index=topic_index,
        intro_content=parsed["intro"],
        lecture_content=parsed["lecture"],
        quiz_question=parsed["question"],
        quiz_options=parsed["options"],
        correct_answer_index=parsed["correct_index"],
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

    feedback = (
        "Correct! Well done! 🎉" if correct
        else f"Not quite — the correct answer was option {topic_session.correct_answer_index + 1}. Keep going! 💪"
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