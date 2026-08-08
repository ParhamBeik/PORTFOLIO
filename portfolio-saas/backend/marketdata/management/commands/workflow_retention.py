from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from marketdata.models import SystemLogEvent, WorkflowRun


class Command(BaseCommand):
    help = "Report retention candidates; delete WorkflowRun rows only with --apply. Legacy logs are never deleted here."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **options):
        cutoff = timezone.now() - timedelta(days=settings.WORKFLOW_RETENTION_DAYS)
        workflow = WorkflowRun.objects.filter(created_at__lt=cutoff)
        legacy = SystemLogEvent.objects.filter(timestamp__lt=cutoff)
        self.stdout.write(
            f"workflow_candidates={workflow.count()} "
            f"legacy_log_candidates={legacy.count()} "
            f"legacy_log_total={SystemLogEvent.objects.count()} "
            f"cutoff={cutoff.isoformat()}"
        )
        if options["apply"]:
            deleted, _ = workflow.delete()
            self.stdout.write(f"workflow_deleted={deleted}; legacy logs preserved")
