from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase

from core import views
from core.departments import DEPARTMENT_CHOICES, OPEN_DEPARTMENT_CHOICES, siwes_applies
from core.models import CourseDefinition, PreGeneratedLesson, Session, StudentProfile, TimetableEntry, TopicSession
from core.simulator_bank import resolve_topic_source


class Registry(SimpleTestCase):
    def test_cbg_exists_but_is_closed(self):
        self.assertIn("cbg", dict(DEPARTMENT_CHOICES))
        self.assertNotIn("cbg", dict(OPEN_DEPARTMENT_CHOICES))

    def test_siwes(self):
        self.assertTrue(siwes_applies("petroleum", "400", "2"))
        self.assertFalse(siwes_applies("petroleum", "300", "2"))
        self.assertFalse(siwes_applies("cbg", "400", "2"))
        self.assertFalse(siwes_applies("nope", "400", "2"))


def _student(name, department):
    user = User.objects.create_user(name, password="x")
    return StudentProfile.objects.create(user=user, department=department, level="100", semester="1")


class TwoDepartmentsOneCode(TestCase):
    def setUp(self):
        kw = dict(course_code="CHM101", course_title="General Chemistry I", level="100", semester="1")
        self.pgg_course = CourseDefinition.objects.create(department="petroleum", **kw)
        self.cbg_course = CourseDefinition.objects.create(department="cbg", **kw)
        mk = lambda c, text: PreGeneratedLesson.objects.create(
            course=c, week_number=1, topic_title="Atoms", content_chunk=text, is_published=True)
        self.pgg_lesson, self.cbg_lesson = mk(self.pgg_course, "PGG text"), mk(self.cbg_course, "CBG text")
        self.pgg, self.cbg = _student("pgg", "petroleum"), _student("cbg", "cbg")

    def test_course_level_uses_own_department(self):
        CourseDefinition.objects.filter(pk=self.cbg_course.pk).update(level="200")
        self.assertEqual(views._course_level("CHM101", self.cbg), "200")
        self.assertEqual(views._course_level("CHM101", self.pgg), "100")

    def test_lesson_pages_use_own_department(self):
        self.assertEqual(views._lesson_pages("CHM101", "Atoms", "petroleum"), ["PGG text"])
        self.assertEqual(views._lesson_pages("CHM101", "Atoms", "cbg"), ["CBG text"])

    def test_refresh_leaves_other_department_alone(self):
        sessions = {}
        for who in (self.pgg, self.cbg):
            s = Session.objects.create(student=who, course_code="CHM101", course_title="x",
                                       week_number=1, topics=["Atoms"])
            sessions[who.pk] = TopicSession.objects.create(
                session=s, topic_name="Atoms", lecture_content="old", is_complete=False)
        views._refresh_lesson_for_students(self.pgg_lesson)
        sessions[self.pgg.pk].refresh_from_db()
        sessions[self.cbg.pk].refresh_from_db()
        self.assertEqual(sessions[self.pgg.pk].lecture_content, "")
        self.assertEqual(sessions[self.cbg.pk].lecture_content, "old")

    def test_bank_source_is_stable_for_shared_course(self):
        self.assertEqual(resolve_topic_source("CHM101", "100", "Atoms"), ("lecture", "PGG text"))
        self.cbg_lesson.save()  # newer edit must not change the choice
        self.assertEqual(resolve_topic_source("CHM101", "100", "Atoms"), ("lecture", "PGG text"))

        def test_read_page_falls_back_to_own_department_lesson(self):
            s = Session.objects.create(student=self.cbg, course_code="CHM101", course_title="x",
                                    week_number=1, topics=["Atoms"])
            TopicSession.objects.create(session=s, topic_name="Atoms", lecture_content="", is_complete=True)
            entry = TimetableEntry.objects.create(student=self.cbg, course_code="CHM101",
                                                course_title="x", day="Mon")
            ctx = views._topic_read_context(self.cbg, "CHM101", "Atoms", entry)
            self.assertEqual(ctx["chat_messages"][0]["content"], "CBG text")