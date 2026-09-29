from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import AuthenticationForm

User = get_user_model()


class EmailOrUsernameAuthenticationForm(AuthenticationForm):
    """Login form that accepts either a username or an email address."""
    username = forms.CharField(
        label="Username or email",
        widget=forms.TextInput(attrs={"autofocus": True, "autocomplete": "username"}),
    )

    def clean_username(self):
        value = self.cleaned_data["username"].strip()
        if "@" in value:
            matches = User.objects.filter(email__iexact=value)
            if matches.count() == 1:  # only swap if the email is unambiguous
                return matches.first().get_username()
        return value


class AccountDetailsForm(forms.ModelForm):
    class Meta:
        model = User
        fields = ["username", "first_name", "last_name", "email"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["email"].required = True

    def clean_email(self):
        email = self.cleaned_data["email"].strip()
        if User.objects.filter(email__iexact=email).exclude(pk=self.instance.pk).exists():
            raise forms.ValidationError("That email is already used by another account.")
        return email