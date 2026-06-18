from django.urls import path
from core import views

urlpatterns = [
    path("", views.login_view, name="login"),
    path("signup/", views.signup_view, name="signup"),
    path("logout/", views.logout_view, name="logout"),
    path("onboarding/", views.onboarding_view, name="onboarding"),
    path("dashboard/", views.dashboard_view, name="dashboard"),
    path("session/<str:course_code>/", views.session_view, name="session"),
    path("quiz/<int:topic_session_id>/", views.quiz_view, name="quiz"),
    path("answer/", views.answer_view, name="answer"),
    path("next-topic/", views.next_topic_view, name="next_topic"),
    path("leaderboard/", views.leaderboard_view, name="leaderboard"),
    path("timetable/", views.timetable_view, name="timetable"),
    path("reschedule/<int:entry_id>/", views.reschedule_session, name="reschedule"),
    path("profile/edit/", views.profile_edit_view, name="profile_edit"),
<<<<<<< HEAD
=======
    path("review/<int:topic_session_id>/", views.review_view, name="review"),
    path("history/", views.history_view, name="history"),
    path("session/<str:course_code>/restart/<int:week_number>/", views.restart_session_view, name="restart_session"),
>>>>>>> 63092e89ef9db556e340fe3f2c2ee7e587f6078f
]