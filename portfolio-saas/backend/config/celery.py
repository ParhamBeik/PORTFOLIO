"""Celery app + static beat schedule.

Real-time push is one Celery beat tick (every 2 min) -> fetch_and_publish, which
fetches global prices, persists them, and broadcasts the price map over Redis
pub/sub to every SSE client. The schedule is static (version-controlled) rather
than DB-backed; a single beat instance is plenty for v1 and the scheduler stays
swappable if that changes.
"""
import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

app = Celery("portfolio")
# Read all CELERY_* settings from Django settings.
app.config_from_object("django.conf:settings", namespace="CELERY")
# Discover tasks.py in each installed app (portfolio.tasks.fetch_and_publish).
app.autodiscover_tasks()

app.conf.beat_schedule = {
    "fetch-prices-every-2-min": {
        "task": "portfolio.tasks.fetch_and_publish",
        "schedule": 120.0,
    },
}


@app.task(bind=True, ignore_result=True)
def debug_task(self):
    print(f"Celery request: {self.request!r}")
