from django.urls import path

from .views import ClientPerfIngestView, PerfReportView

urlpatterns = [
    path("client/", ClientPerfIngestView.as_view(), name="perf-client"),
    path("report/", PerfReportView.as_view(), name="perf-report"),
]
