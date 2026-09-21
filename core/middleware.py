class NoCacheForAuthenticatedPagesMiddleware:
    """Browser back/forward serves a cached copy of a previous page by
    default. After an action changes visible state — approving a
    question, finishing a lecture chunk, ending a battle match —
    pressing back can show a stale page whose controls no longer match
    reality. This forces every authenticated page to be re-fetched from
    the server on back/forward instead of served from cache."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if request.user.is_authenticated and not request.path.startswith(("/static/", "/media/")):
            response["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            response["Pragma"] = "no-cache"
        return response