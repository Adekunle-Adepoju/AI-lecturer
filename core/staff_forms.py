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


class BattleQuestionGenerationForm(forms.Form):
    course_code = forms.ChoiceField(
        label="Course (leave blank for all eligible 300L/400L courses)",
        required=False,
    )
    limit = forms.IntegerField(
        label="Chunks to process this run",
        initial=10, min_value=1, max_value=100,
        help_text="Each chunk generates several Gemini calls — keep this modest per click.",
    )
    retry_failed = forms.BooleanField(
        label="Also retry previously failed chunks",
        required=False,
    )

    def __init__(self, *args, **kwargs):
        course_choices = kwargs.pop("course_choices", [])
        super().__init__(*args, **kwargs)
        self.fields["course_code"].choices = [("", "All eligible courses")] + [(c, c) for c in course_choices]