from django import forms
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.models import User
from .models import StudentProfile, LEVEL_CHOICES, SEMESTER_CHOICES
 
 
class SignupForm(UserCreationForm):
    first_name = forms.CharField(max_length=50, required=True, widget=forms.TextInput(attrs={"placeholder": "e.g. Emeka"}))
    last_name = forms.CharField(max_length=50, required=True, widget=forms.TextInput(attrs={"placeholder": "e.g. Okafor"}))
    email = forms.EmailField(required=True, widget=forms.EmailInput(attrs={"placeholder": "your@email.com"}))
 
    class Meta:
        model = User
        fields = ["first_name", "last_name", "username", "email", "password1", "password2"]
 
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["username"].widget.attrs["placeholder"] = "Choose a username"
        self.fields["password1"].widget.attrs["placeholder"] = "Create a password"
        self.fields["password2"].widget.attrs["placeholder"] = "Repeat your password"
        for field in self.fields.values():
            field.widget.attrs["class"] = "form-input"
 
 
class OnboardingForm(forms.ModelForm):
    class Meta:
        model = StudentProfile
        fields = ["level", "semester"]
        widgets = {
            "level": forms.Select(attrs={"class": "form-input"}),
            "semester": forms.Select(attrs={"class": "form-input"}),
        }
 