from django.db import models
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.conf import settings       

LEVEL_CHOICES = [("100", "100 Level"), ("200", "200 Level"), ("300", "300 Level"), ("400", "400 Level"), ("500", "500 Level")]
SEMESTER_CHOICES = [("1", "First Semester"), ("2", "Second Semester")]
SCHOOL_CHOICES = [("unilag", "University of Lagos")]
DEPARTMENT_CHOICES = [("petroleum", "Petroleum & Gas Engineering")]
class CourseDefinition(models.Model):
    """Admin-defined course catalogue — compulsory or elective"""
    course_code = models.CharField(max_length=10, unique=True)
    course_title = models.CharField(max_length=100)
    level = models.CharField(max_length=3, choices=LEVEL_CHOICES)
    semester = models.CharField(max_length=1, choices=SEMESTER_CHOICES)
    school = models.CharField(max_length=50, choices=SCHOOL_CHOICES, default="unilag")
    department = models.CharField(max_length=50, choices=DEPARTMENT_CHOICES, default="petroleum")
    units = models.IntegerField(default=3)
    is_elective = models.BooleanField(default=False)

    class Meta:
        ordering = ["level", "semester", "is_elective", "course_code"]

    def __str__(self):
        tag = "Elective" if self.is_elective else "Compulsory"
        return f"{self.course_code} — {self.course_title} ({tag})"

class PreGeneratedLesson(models.Model):
    SOURCE_CHOICES = [("slides", "Lecturer slides"), ("outline", "Course outline only")]

    course = models.ForeignKey(CourseDefinition, on_delete=models.CASCADE, related_name="lessons")
    week_number = models.IntegerField()
    topic_title = models.CharField(max_length=200)
    content_chunk = models.TextField()
    is_published = models.BooleanField(default=False)
    generated_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    is_truncated = models.BooleanField(default=False)
    continuation_attempts = models.PositiveSmallIntegerField(default=0)
    source_type = models.CharField(max_length=10, choices=SOURCE_CHOICES, default="slides")
    review_note = models.TextField(blank=True)
    quiz = models.JSONField(default=dict, blank=True)
    quiz_source_hash = models.CharField(max_length=64, blank=True)

    class Meta:
        ordering = ["week_number", "topic_title"]
        unique_together = ["course", "week_number", "topic_title"]

    def __str__(self):
        return f"{self.course.course_code} — Week {self.week_number} — {self.topic_title}"


class StudentProfile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="profile")
    matric_number = models.CharField(max_length=20, blank=True)
    school = models.CharField(max_length=50, choices=SCHOOL_CHOICES, default="unilag")
    department = models.CharField(max_length=50, choices=DEPARTMENT_CHOICES, default="petroleum")
    level = models.CharField(max_length=3, choices=LEVEL_CHOICES)
    semester = models.CharField(max_length=1, choices=SEMESTER_CHOICES)
    is_staff_member = models.BooleanField(default=False)
    elective_courses = models.ManyToManyField(
        'CourseDefinition',
        blank=True,
        related_name="enrolled_students",
        limit_choices_to={"is_elective": True}
    )
    xp = models.IntegerField(default=0)
    streak = models.IntegerField(default=0)
    last_session_date = models.DateField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    queue_position = models.IntegerField(default=0)
    queue_date = models.DateField(null=True, blank=True)

    def __str__(self):
        return f"{self.user.username} — {self.level}L"

class TimetableEntry(models.Model):
    DAYS = [("Mon", "Monday"), ("Tue", "Tuesday"), ("Wed", "Wednesday"), ("Thu", "Thursday"), ("Fri", "Friday")]

    student = models.ForeignKey(StudentProfile, on_delete=models.CASCADE, related_name="timetable")
    course_code = models.CharField(max_length=10)
    course_title = models.CharField(max_length=100)
    day = models.CharField(max_length=3, choices=DAYS)
    time = models.CharField(max_length=10, default="09:00")
    week_number = models.IntegerField(default=1)
    total_weeks = models.IntegerField(default=10)
    is_completed = models.BooleanField(default=False)
    is_missed = models.BooleanField(default=False)
    rescheduled_to = models.DateField(null=True, blank=True)
    # Weekdays this course is studied (Mon=0 … Sun=6).
    # None = not generated yet, [] = student deliberately left it unscheduled.
    study_days = models.JSONField(null=True, blank=True, default=None)
    test_due_date = models.DateField(null=True, blank=True)

    class Meta:
        ordering = ["day", "time"]

    def __str__(self):
        return f"{self.student.user.username} — {self.course_code} ({self.day})"


class Session(models.Model):
    student = models.ForeignKey(StudentProfile, on_delete=models.CASCADE, related_name="sessions")
    course_code = models.CharField(max_length=10)
    course_title = models.CharField(max_length=100)
    week_number = models.IntegerField(default=1)
    topics = models.JSONField(default=list)
    current_topic_index = models.IntegerField(default=0)
    is_complete = models.BooleanField(default=False)
    xp_earned = models.IntegerField(default=0)
    started_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-started_at"]

    def __str__(self):
        return f"{self.student.user.username} — {self.course_code} Week {self.week_number}"


class TopicSession(models.Model):
    session = models.ForeignKey(Session, on_delete=models.CASCADE, related_name="topic_sessions")
    topic_name = models.CharField(max_length=200)
    topic_index = models.IntegerField(default=0)
    intro_content = models.TextField(blank=True)
    lecture_content = models.TextField(blank=True)
    chunks = models.JSONField(default=list)           
    current_chunk_index = models.IntegerField(default=0)
    quiz_question = models.TextField(blank=True)
    quiz_options = models.JSONField(default=list)
    correct_answer_index = models.IntegerField(default=0)
    quiz_explanation = models.TextField(blank=True)
    student_answer_index = models.IntegerField(null=True, blank=True)
    passed_quiz = models.BooleanField(default=False)
    xp_earned = models.IntegerField(default=0)
    is_complete = models.BooleanField(default=False)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["topic_index"]

    def __str__(self):
        return f"{self.session} — {self.topic_name}"
    
class ChatMessage(models.Model):
    ROLE_CHOICES = [
        ("user", "User"),
        ("ai", "AI"),
    ]

    topic_session = models.ForeignKey(
        TopicSession, on_delete=models.CASCADE, related_name="chatmessage_set"
    )
    role = models.CharField(max_length=10, choices=ROLE_CHOICES)
    content = models.TextField()
    image_url = models.URLField(blank=True, null=True)
    is_pregenerated = models.BooleanField(default=False)   # ← new
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return f"{self.role} — {self.content[:40]}"


class SlideDocument(models.Model):
    course_code = models.CharField(max_length=10)
    course_title = models.CharField(max_length=100)
    level = models.CharField(max_length=3, choices=LEVEL_CHOICES)
    # File field is now optional because the document acts as a container for all uploaded slides
    file = models.FileField(upload_to="slides/", max_length=500, blank=True, null=True) 
    extracted_text = models.TextField(blank=True)
    extracted_topics = models.JSONField(default=list)
    topics_incomplete = models.BooleanField(default=False)
    uploaded_at = models.DateTimeField(auto_now_add=True)
    parsed = models.BooleanField(default=False)
    # New tracking field to count how many slide files have been appended
    append_count = models.IntegerField(default=1)

    class Meta:
        ordering = ["course_code"]
        unique_together = ["course_code", "level"]

    def __str__(self):
        return f"{self.course_code} — Slide Document (Appended {self.append_count} times)"

class SlideExtractionChunk(models.Model):
    """One macro-chunk of a slide deck's extracted text, tracked individually
    so topic-extraction retries only reprocess chunks that failed or are
    still pending — instead of restarting the whole document from scratch
    and wasting API quota re-extracting chunks that already succeeded."""
    STATUS_CHOICES = [
        ("PENDING", "Pending"),
        ("COMPLETED", "Completed"),
        ("FAILED", "Failed"),
    ]

    slide = models.ForeignKey(SlideDocument, on_delete=models.CASCADE, related_name="extraction_chunks")
    chunk_index = models.IntegerField()
    chunk_text = models.TextField(blank=True)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default="PENDING")
    extracted_topics = models.JSONField(default=list)
    error_message = models.TextField(blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["chunk_index"]
        unique_together = ["slide", "chunk_index"]

    def __str__(self):
        return f"{self.slide.course_code} — Extraction Chunk {self.chunk_index} ({self.status})"

class SlideCleanupChunk(models.Model):
    """One macro-chunk of a slide deck's raw extracted text, tracked individually
    through the AI cleanup pass so a rate-limit failure or crash partway through
    a large deck doesn't lose already-cleaned chunks — re-running cleanup only
    reprocesses chunks that are still PENDING or FAILED."""
    STATUS_CHOICES = [
        ("PENDING", "Pending"),
        ("COMPLETED", "Completed"),
        ("FAILED", "Failed"),
    ]

    slide = models.ForeignKey(SlideDocument, on_delete=models.CASCADE, related_name="cleanup_chunks")
    chunk_index = models.IntegerField()
    chunk_text = models.TextField(blank=True)
    cleaned_text = models.TextField(blank=True)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default="PENDING")
    error_message = models.TextField(blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["chunk_index"]
        unique_together = ["slide", "chunk_index"]

    def __str__(self):
        return f"{self.slide.course_code} — Cleanup Chunk {self.chunk_index} ({self.status})"

class SlideChunk(models.Model):
    """One week's worth of slide content, split out from the full transcript
    so lecture generation only receives what's relevant to that week."""
    slide = models.ForeignKey(SlideDocument, on_delete=models.CASCADE, related_name="chunks")
    week_number = models.IntegerField()
    chunk_text = models.TextField(blank=True)

    class Meta:
        ordering = ["week_number"]
        unique_together = ["slide", "week_number"]

    def __str__(self):
        return f"{self.slide.course_code} — Week {self.week_number} chunk"


class CourseOutline(models.Model):
    course_code = models.CharField(max_length=10)
    course_title = models.CharField(max_length=100)
    level = models.CharField(max_length=3, choices=LEVEL_CHOICES)
    file = models.FileField(upload_to="outlines/")
    extracted_text = models.TextField(blank=True)
    topics_json = models.JSONField(default=dict)
    uploaded_at = models.DateTimeField(auto_now_add=True)
    parsed = models.BooleanField(default=False)

    class Meta:
        unique_together = ["course_code", "level"]

    def __str__(self):
        return f"{self.course_code} — Course Outline"


class PastQuestion(models.Model):
    course_code = models.CharField(max_length=10)
    course_title = models.CharField(max_length=100)
    level = models.CharField(max_length=3, choices=LEVEL_CHOICES)
    file = models.FileField(upload_to="past_questions/")
    extracted_text = models.TextField(blank=True)
    parsed_questions = models.JSONField(default=list)
    uploaded_at = models.DateTimeField(auto_now_add=True)
    parsed = models.BooleanField(default=False)

    class Meta:
        ordering = ["course_code", "-uploaded_at"]

    def __str__(self):
        return f"{self.course_code} — Past Questions ({self.uploaded_at.strftime('%Y')})"


class Test(models.Model):
    STATUS_CHOICES = [
        ("pending", "Not Started"),
        ("in_progress", "In Progress"),
        ("complete", "Complete"),
    ]

    student = models.ForeignKey(StudentProfile, on_delete=models.CASCADE, related_name="tests")
    course_code = models.CharField(max_length=10)
    course_title = models.CharField(max_length=100)
    week_triggered = models.IntegerField(default=6)
    questions = models.JSONField(default=list)
    answers = models.JSONField(default=list)
    score = models.IntegerField(default=0)
    xp_earned = models.IntegerField(default=0)
    status = models.CharField(max_length=15, choices=STATUS_CHOICES, default="pending")
    started_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-started_at"]

    def __str__(self):
        return f"{self.student.user.username} — {self.course_code} Test (Week {self.week_triggered})"


class Exam(models.Model):
    STATUS_CHOICES = [
        ("pending", "Not Started"),
        ("in_progress", "In Progress"),
        ("complete", "Complete"),
    ]

    student = models.ForeignKey(StudentProfile, on_delete=models.CASCADE, related_name="exams")
    course_code = models.CharField(max_length=10)
    course_title = models.CharField(max_length=100)
    week_triggered = models.IntegerField(default=12)
    questions = models.JSONField(default=list)
    answers = models.JSONField(default=list)
    score = models.IntegerField(default=0)
    xp_earned = models.IntegerField(default=0)
    status = models.CharField(max_length=15, choices=STATUS_CHOICES, default="pending")
    started_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-started_at"]

    def __str__(self):
        return f"{self.student.user.username} — {self.course_code} Exam (Week {self.week_triggered})"

class SimulatorTest(models.Model):
    """Student-initiated or auto-triggered test from the simulator"""
    MODE_CHOICES = [
        ("auto", "Automatic Test Week"),
        ("voluntary", "Voluntary Practice Test"),
    ]
    FORMAT_CHOICES = [
        ("mcq", "Multiple Choice"),
        ("theory", "Theory / Calculation"),
        ("mixed", "Mixed"),
    ]
    STATUS_CHOICES = [
        ("in_progress", "In Progress"),
        ("complete", "Complete"),
    ]

    student = models.ForeignKey(StudentProfile, on_delete=models.CASCADE, related_name="simulator_tests")
    course_code = models.CharField(max_length=10)
    course_title = models.CharField(max_length=100)
    topic = models.CharField(max_length=200, blank=True)
    mode = models.CharField(max_length=10, choices=MODE_CHOICES, default="voluntary")
    question_format = models.CharField(max_length=10, choices=FORMAT_CHOICES, default="theory")
    week_number = models.IntegerField(default=1)

    # Questions stored as JSON list
    # For MCQ: [{question, options, correct_index, explanation}]
    # For theory/calc: [{question, marks, model_answer}]
    questions = models.JSONField(default=list)

    # Student's answers — list matching questions order
    # For MCQ: list of chosen indices
    # For theory: list of typed strings
    answers = models.JSONField(default=list)

    # Grading
    ai_feedback = models.JSONField(default=list)   # per-question feedback from AI
    overall_feedback = models.TextField(blank=True)
    percentage_score = models.FloatField(default=0)
    xp_earned = models.IntegerField(default=0)
    grade = models.CharField(max_length=2, blank=True)

    status = models.CharField(max_length=15, choices=STATUS_CHOICES, default="in_progress")
    started_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-started_at"]

    def __str__(self):
        return f"{self.student.user.username} — {self.course_code} {self.mode} test"


class Challenge(models.Model):
    STATUS_CHOICES = [
        ("pending", "Pending Acceptance"),
        ("active", "In Progress"),
        ("complete", "Complete"),
        ("declined", "Declined"),
        ("expired", "Expired"),
    ]

    # Changed on_delete to CASCADE to maintain relational database integrity if an active profile is dropped
    challenger = models.ForeignKey(StudentProfile, on_delete=models.CASCADE, related_name="challenges_sent")
    opponent = models.ForeignKey(StudentProfile, on_delete=models.CASCADE, related_name="challenges_received")
    course_code = models.CharField(max_length=10)
    course_title = models.CharField(max_length=100)
    questions = models.JSONField(default=list)
    challenger_answers = models.JSONField(default=list)
    opponent_answers = models.JSONField(default=list)
    challenger_score = models.IntegerField(default=0)
    opponent_score = models.IntegerField(default=0)
    winner = models.ForeignKey(
        StudentProfile, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="challenges_won"
    )
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default="pending")
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.challenger.user.username} vs {self.opponent.user.username} — {self.course_code}"

class SlideTopicChunk(models.Model):
    """One topic's slice of a week's slide content — split out from the
    week-level SlideChunk so lecture generation for a single topic never
    receives the other 1-2 topics scheduled for the same week."""
    slide = models.ForeignKey(SlideDocument, on_delete=models.CASCADE, related_name="topic_chunks")
    week_number = models.IntegerField()
    topic_name = models.CharField(max_length=200)
    chunk_text = models.TextField(blank=True)
    is_empty = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["week_number", "topic_name"]
        unique_together = ["slide", "week_number", "topic_name"]

    def __str__(self):
        return f"{self.slide.course_code} — Week {self.week_number} — {self.topic_name} (topic chunk)"

class SlideTopicSplitChunk(models.Model):
    """One macro-chunk of a slide deck's text, tracked individually through
    the topic-split pass so a quota exhaustion or crash partway through a
    large deck doesn't lose already-split chunks — re-running only
    reprocesses chunks still PENDING or FAILED."""
    STATUS_CHOICES = [
        ("PENDING", "Pending"),
        ("COMPLETED", "Completed"),
        ("FAILED", "Failed"),
    ]

    slide = models.ForeignKey(SlideDocument, on_delete=models.CASCADE, related_name="topic_split_chunks")
    chunk_index = models.IntegerField()
    chunk_text = models.TextField(blank=True)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default="PENDING")
    split_result = models.JSONField(default=dict)  # {"Topic A": "...", "Topic B": "..."}
    error_message = models.TextField(blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["chunk_index"]
        unique_together = ["slide", "chunk_index"]

    def __str__(self):
        return f"{self.slide.course_code} — Topic Split Chunk {self.chunk_index} ({self.status})"

class SimulatorQuestion(models.Model):
    """One pre-generated, verified test question. Tests are assembled by
    copying rows from here into SimulatorTest.questions, so editing or
    retiring a question never changes a test someone is already taking."""

    TYPE_CHOICES = [
        ("mcq", "Multiple choice"),
        ("theory", "Theory"),
        ("calculation", "Calculation"),
    ]
    STATUS_CHOICES = [
        ("pending_review", "Pending review"),
        ("approved", "Approved"),
        ("retired", "Retired"),
    ]
    SOURCE_CHOICES = [
        ("lecture", "Published lecture"),
        ("slide", "Slide topic chunk"),
    ]

    course_code = models.CharField(max_length=10, db_index=True)
    level = models.CharField(max_length=3, choices=LEVEL_CHOICES)
    topic_name = models.CharField(max_length=200)

    question_type = models.CharField(max_length=12, choices=TYPE_CHOICES)
    cognitive_level = models.CharField(max_length=12, blank=True)  # recall / application / analysis
    marks = models.PositiveSmallIntegerField(default=2)

    # mcq:  {question, options[4], correct_index, explanation}
    # theory/calculation: {question, model_answer, marking_scheme:[{point, marks}]}
    content = models.JSONField(default=dict)

    source_type = models.CharField(max_length=10, choices=SOURCE_CHOICES)
    source_hash = models.CharField(max_length=64)  # sha256 of the source text used

    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default="pending_review")
    verified = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["course_code", "level", "topic_name", "status"]),
        ]
        ordering = ["course_code", "topic_name", "question_type", "id"]

    def __str__(self):
        return f"{self.course_code} — {self.topic_name} [{self.question_type}] ({self.status})"


class SimulatorBankTopic(models.Model):
    """Generation state for one topic of one course. Makes bank generation
    resumable and idempotent, and is what the setup page reads to label a
    topic 'coming soon' (NO_SOURCE) or ready."""

    STATUS_CHOICES = [
        ("PENDING", "Pending"),
        ("COMPLETED", "Completed"),
        ("FAILED", "Failed"),
        ("NO_SOURCE", "No lecture or slide content yet"),
    ]

    course_code = models.CharField(max_length=10)
    level = models.CharField(max_length=3, choices=LEVEL_CHOICES)
    topic_name = models.CharField(max_length=200)

    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default="PENDING")
    source_type = models.CharField(max_length=10, blank=True)
    source_hash = models.CharField(max_length=64, blank=True)
    mcq_saved = models.PositiveSmallIntegerField(default=0)
    long_saved = models.PositiveSmallIntegerField(default=0)
    error_message = models.TextField(blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ["course_code", "level", "topic_name"]
        ordering = ["course_code", "topic_name"]

    def __str__(self):
        return f"{self.course_code} — {self.topic_name} ({self.status})"