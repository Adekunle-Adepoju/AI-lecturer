# focus_sprint/context_processors.py
def active_focus_sprint(request):
    if request.user.is_authenticated and hasattr(request.user, "profile"):
        from focus_sprint.models import FocusSprint
        return {"active_focus_sprint": FocusSprint.objects.filter(
            student=request.user.profile, status="active").first()}
    return {}