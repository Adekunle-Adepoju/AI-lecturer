from django import forms
from .models import SlideDocument, CourseOutline, PastQuestion, CourseDefinition, LEVEL_CHOICES, SEMESTER_CHOICES, SCHOOL_CHOICES, DEPARTMENT_CHOICES


class SlideUploadForm(forms.ModelForm):
    class Meta:
        model = SlideDocument
        fields = ["course_code", "course_title", "level", "file"]
        labels = {
            "course_code": "Course Code",
            "course_title": "Course Title",
            "level": "Level",
            "file": "Slide File (PDF, DOCX or PPTX)",
        }

    def validate_unique(self):
        """
        Override this to bypass the unique_together check.
        Our custom view logic handles appending to existing records instead.
        """
        pass


class CourseOutlineUploadForm(forms.ModelForm):
    class Meta:
        model = CourseOutline
        fields = ["course_code", "course_title", "level", "file"]
        labels = {
            "course_code": "Course Code",
            "course_title": "Course Title",
            "level": "Level",
            "file": "Outline File (PDF or DOCX)",
        }


class PastQuestionUploadForm(forms.ModelForm):
    class Meta:
        model = PastQuestion
        fields = ["course_code", "course_title", "level", "file"]
        labels = {
            "course_code": "Course Code",
            "course_title": "Course Title",
            "level": "Level",
            "file": "Past Question File (PDF or DOCX)",
        }


class CourseDefinitionForm(forms.ModelForm):
    class Meta:
        model = CourseDefinition
        fields = ["course_code", "course_title", "level", "semester", "school", "department", "units", "is_elective"]
        labels = {
            "is_elective": "This is an elective course",
        }