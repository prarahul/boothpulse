from django.contrib import admin
from django.urls import include, path
from django.views.generic import RedirectView
from rest_framework.routers import DefaultRouter

from accounts.views import LogoutView, MeView, OTPRequestView, OTPVerifyView
from booths.views import (
    BoothReportView, BoothViewSet, DashboardView, ImportRollView, SyncView, VoterViewSet,
    dashboard_page,
)

router = DefaultRouter()
router.register("booths", BoothViewSet, basename="booth")
router.register("voters", VoterViewSet, basename="voter")

admin.site.site_header = "BoothPulse control room"
admin.site.site_title = "BoothPulse"

api = [
    path("auth/otp/request/", OTPRequestView.as_view()),
    path("auth/otp/verify/", OTPVerifyView.as_view()),
    path("auth/logout/", LogoutView.as_view()),
    path("me/", MeView.as_view()),
    path("sync/", SyncView.as_view()),
    path("dashboard/", DashboardView.as_view()),
    path("import/roll/", ImportRollView.as_view()),
    path("reports/booths.csv", BoothReportView.as_view()),
    path("", include(router.urls)),
]

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/", include(api)),
    path("dashboard/", dashboard_page, name="dashboard"),
    path("", RedirectView.as_view(url="/dashboard/", permanent=False)),
]
