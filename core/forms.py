from django import forms
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.models import User
from .models import (
    StudentProfile, CourseDefinition,
    LEVEL_CHOICES, SEMESTER_CHOICES, SCHOOL_CHOICES, DEPARTMENT_CHOICES
)


class SignupForm(UserCreationForm):
    first_name = forms.CharField(max_length=50, required=True, label="First Name")
    last_name = forms.CharField(max_length=50, required=True, label="Last Name")
    matric_number = forms.CharField(max_length=20, required=True, label="Matric Number")
    school = forms.ChoiceField(choices=SCHOOL_CHOICES, label="School")
    department = forms.ChoiceField(choices=DEPARTMENT_CHOICES, label="Department")
    level = forms.ChoiceField(choices=LEVEL_CHOICES, label="Level")
    semester = forms.ChoiceField(choices=SEMESTER_CHOICES, label="Semester")

    class Meta:
        model = User
        fields = [
            "first_name", "last_name", "username", "matric_number",
            "school", "department", "level", "semester",
            "password1", "password2"
        ]


class ElectiveSelectionForm(forms.Form):
    """Shown after signup and on profile edit — pick electives"""
    electives = forms.ModelMultipleChoiceField(
        queryset=CourseDefinition.objects.none(),
        widget=forms.CheckboxSelectMultiple,
        required=False,
        label="Select your elective courses"
    )

    def __init__(self, *args, level=None, semester=None, school=None, department=None, **kwargs):
        super().__init__(*args, **kwargs)
        if level and semester:
            self.fields["electives"].queryset = CourseDefinition.objects.filter(
                level=level,
                semester=semester,
                is_elective=True,
                school=school or "unilag",
                department=department or "petroleum",
            )


class OnboardingForm(forms.ModelForm):
    class Meta:
        model = StudentProfile
        fields = ["level", "semester"]


class ProfileEditForm(forms.ModelForm):
    first_name = forms.CharField(max_length=50, required=True, label="First Name")
    last_name = forms.CharField(max_length=50, required=True, label="Last Name")

    class Meta:
        model = StudentProfile
        fields = ["matric_number", "school", "department", "level", "semester"]

    def __init__(self, *args, **kwargs):
        self.user = kwargs.pop("user", None)
        super().__init__(*args, **kwargs)
        if self.user:
            self.fields["first_name"].initial = self.user.first_name
            self.fields["last_name"].initial = self.user.last_name

    def save(self, commit=True):
        profile = super().save(commit=False)
        if self.user:
            self.user.first_name = self.cleaned_data["first_name"]
            self.user.last_name = self.cleaned_data["last_name"]
            self.user.save()
        if commit:
            profile.save()
            from core.views import _generate_timetable
            _generate_timetable(profile)
        return profile