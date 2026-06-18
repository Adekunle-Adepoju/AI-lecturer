import json
import markdown
import datetime
from datetime import date, timedelta
import random

from django.contrib import messages
from django.contrib.auth import login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import AuthenticationForm
from django.conf import settings
from django.shortcuts import render, redirect, get_object_or_404
from django.utils import timezone
from django.views.decorators.http import require_POST
from groq import Groq

from .forms import SignupForm, OnboardingForm, ProfileEditForm
from .models import (
    StudentProfile, TimetableEntry, Session, TopicSession,
    SlideDocument, CourseOutline, COURSES, COURSE_OUTLINES
)
from .prompt import SYSTEM_PROMPT


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
                _generate_timetable(profile)
                login(request, user)
                return redirect("dashboard")
            except Exception as e:
                form.add_error(None, f"Error creating account: {str(e)}")
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


# ─── Onboarding — kept for existing users without profile ──────────────────────

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


# ─── Timetable generator ───────────────────────────────────────────────────────

def _generate_timetable(profile):
    courses = COURSES.get(profile.level, {}).get(profile.semester, [])
    days = ["Mon", "Tue", "Wed", "Thu", "Fri"]

    TimetableEntry.objects.filter(student=profile).delete()

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

    today = datetime.datetime.now().strftime("%a")
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


# ─── Helpers ───────────────────────────────────────────────────────────────────

def _get_topics_for_week(course_code, level, week_number):
    """Get 3 topics for this week — prefer slide-extracted topics, fall back to hardcoded outline"""
    from .models import SlideDocument

    try:
        slide = SlideDocument.objects.get(course_code=course_code, level=level, parsed=True)
        if slide.extracted_topics:
            start = (week_number - 1) * 3
            end = start + 3
            topics = slide.extracted_topics[start:end]
            if topics:
                return topics
            # If we've run past the slide's topic list, repeat the last 3 as review
            if len(slide.extracted_topics) >= 3:
                return slide.extracted_topics[-3:]
    except SlideDocument.DoesNotExist:
        pass

    # Fallback — hardcoded outline
    topics = COURSE_OUTLINES.get(course_code, {}).get(week_number, [])
    if not topics:
        topics = ["Core Concepts", "Key Applications", "Problem Solving"]
    return topics[:3]


def _generate_topic_lecture(course_code, course_title, topic_name, week, level, student_name, topic_index=0, slide_text=""):
    client = get_groq_client()

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

        # Fix common JSON-breaking escape sequences from the model
        clean = clean.replace("\\*", "*").replace("\\%", "%")

        # Try to isolate just the JSON object in case of stray text
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
            import re
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
        return render(request, "core/session.html", {"entry": entry})

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
            existing_topic = existing_session.topic_sessions.filter(
                topic_index=current_index,
                is_complete=False,
            ).first()

            if existing_topic:
                intro_html = markdown.markdown(existing_topic.intro_content, extensions=["extra"])
                return render(request, "core/session.html", {
                    "entry": entry,
                    "session": existing_session,
                    "topic_session": existing_topic,
                    "intro": intro_html,
                    "topic_number": current_index + 1,
                    "total_topics": len(existing_session.topics),
                    "topic_name": existing_topic.topic_name,
                })
            else:
                topic_name = existing_session.topics[current_index]
                return _teach_topic(request, existing_session, entry, profile, topic_name, current_index)

        topics = _get_topics_for_week(course_code, profile.level, entry.week_number)
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
    slide_text = ""
    outline_text = ""

    try:
        slide_doc = SlideDocument.objects.get(
            course_code=session.course_code,
            level=profile.level
        )
        if slide_doc.extracted_text:
            slide_text = f"\n\nLECTURER SLIDES (full course reference — focus only on content relevant to this topic):\n{slide_doc.extracted_text[:6000]}"
    except SlideDocument.DoesNotExist:
        pass

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

    # Final fallback if quiz still empty
    if not parsed["question"]:
        parsed["question"] = f"Which of the following best describes a key concept from the topic '{topic_name}'?"
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

@login_required
def history_view(request):
    if not hasattr(request.user, "profile"):
        return redirect("onboarding")

    profile = request.user.profile

    # Get all completed topic sessions, grouped by course
    topic_sessions = TopicSession.objects.filter(
        session__student=profile,
        is_complete=True
    ).select_related("session").order_by("-session__started_at", "topic_index")

    # Group by course code
    history_by_course = {}
    for ts in topic_sessions:
        code = ts.session.course_code
        title = ts.session.course_title
        if code not in history_by_course:
            history_by_course[code] = {
                "course_title": title,
                "topics": []
            }
        history_by_course[code]["topics"].append(ts)

    return render(request, "core/history.html", {
        "history_by_course": history_by_course,
    })


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

# ─── Restart session ──────────────────────────────────────────────────────────

@login_required
@require_POST
def restart_session_view(request, course_code, week_number):
    if not hasattr(request.user, "profile"):
        return redirect("onboarding")

    profile = request.user.profile
    entry = get_object_or_404(TimetableEntry, student=profile, course_code=course_code)

    # Find the session for that specific week (completed or not)
    session = Session.objects.filter(
        student=profile,
        course_code=course_code,
        week_number=week_number,
    ).first()

    if session:
        # Claw back XP earned in this session so it doesn't double up
        profile.xp = max(0, profile.xp - session.xp_earned)
        profile.save()

        # Deleting the session cascades and deletes its TopicSessions too
        session.delete()

    # Roll the timetable entry back to this week and mark it not completed
    entry.week_number = week_number
    entry.is_completed = False
    entry.save()

    messages.success(request, f"{course_code} Week {week_number} has been restarted from the beginning.")
    return redirect("session", course_code=course_code)