from django.urls import path
from rest_framework_simplejwt.views import TokenRefreshView

from . import views

urlpatterns = [
    path("auth/login/", views.LoginView.as_view()),
    path("auth/refresh/", TokenRefreshView.as_view()),
    path("auth/logout/", views.LogoutView.as_view()),
    path("auth/device-token/", views.DeviceTokenView.as_view()),
    path("me/", views.MeView.as_view()),
    path("me/prefs/", views.MePrefsView.as_view()),
    path("health/", views.HealthView.as_view()),
    path("today/", views.TodayView.as_view()),
    path("busy/", views.BusyView.as_view()),
    # MD personal tasks (MD only)
    path("private-tasks/", views.PrivateTaskListView.as_view()),
    path("private-tasks/<uuid:task_id>/", views.PrivateTaskDetailView.as_view()),
    path("private-tasks/<uuid:task_id>/complete/", views.PrivateTaskCompleteView.as_view()),
    # Shared MD tasks (MD direct, PA proposes)
    path("shared-tasks/<uuid:task_id>/", views.SharedTaskDetailView.as_view()),
    path("shared-tasks/<uuid:task_id>/complete/", views.SharedTaskCompleteView.as_view()),
    # MD -> PA tasks (inside JARVIS)
    path("pa-tasks/", views.PaTaskListView.as_view()),
    path("pa-tasks/<uuid:task_id>/status/", views.PaTaskStatusView.as_view()),
    # Staff + delegation to TaskPanel
    path("staff/", views.StaffListView.as_view()),
    path("staff/sync/", views.StaffSyncView.as_view()),
    path("delegations/", views.DelegationListView.as_view()),
    path("delegations/team/", views.TeamDelegationView.as_view()),
    path("delegations/refresh/", views.DelegationRefreshView.as_view()),
    path("delegations/<uuid:task_id>/check/", views.DelegationCheckView.as_view()),
    # PA -> MD requests
    path("requests/", views.RequestListView.as_view()),
    path("requests/<uuid:request_id>/accept/", views.RequestAcceptView.as_view()),
    path("requests/<uuid:request_id>/decline/", views.RequestDeclineView.as_view()),
    path("requests/<uuid:request_id>/withdraw/", views.RequestWithdrawView.as_view()),
    # Assistant
    path("assistant/interpret/", views.InterpretView.as_view()),
    path("assistant/ask/", views.AskView.as_view()),
    path("assistant/turn/", views.ConversationTurnView.as_view()),
    path("assistant/briefing/", views.BriefingView.as_view()),
    path("assistant/usage/", views.AiUsageView.as_view()),
    # Things the user asked JARVIS to remember (owner-only)
    path("memories/", views.MemoryListView.as_view()),
    path("memories/<uuid:memory_id>/", views.MemoryDetailView.as_view()),
    # Reminders + alerts
    path("reminders/", views.ReminderListView.as_view()),
    path("reminders/ack/", views.ReminderAckView.as_view()),
    path("alerts/", views.AlertListView.as_view()),
    path("alerts/<uuid:alert_id>/ack/", views.AlertAckView.as_view()),
]
