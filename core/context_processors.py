# core/context_processors.py
from focus_sprint.models import FocusSprint


def active_focus_sprint(request):
    if not request.user.is_authenticated:
        return {}
    profile = getattr(request.user, "profile", None)
    if not profile:
        return {}
    sprint = FocusSprint.objects.filter(student=profile, status="active").first()
    return {"active_focus_sprint": sprint}