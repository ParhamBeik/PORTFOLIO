"""Time every endpoint the UI actually loads, against whatever data is really there.

The test suite cannot answer "is the Optimal page usable?". It runs against a
handful of fixture rows, so a view that takes 15 s over 40 M production rows and
one that takes 3 ms over 10 fixture rows are indistinguishable to it -- which is
exactly how a 15-second Ops page and a 1.7 MB payload both stayed green.

This closes that gap from the other side: run it on the box that has the data.

    python manage.py benchmark_pages                 # every page
    python manage.py benchmark_pages --page ops      # one page
    python manage.py benchmark_pages --fail-over 5   # non-zero exit if too slow

Budgets below are what a person will sit through, not what the code currently
does; a red line is a bug report, not a target to relax.
"""
import json
import time

from django.core.management.base import BaseCommand, CommandError
from django.test import Client

# page -> [(label, url, seconds_budget, bytes_budget)]
# One entry per request the page fires on load, so the "page total" row is what
# the user actually waits for.
PAGES = {
    "dashboard": [
        ("valuation (nominal)", "/api/valuation/", 1.0, 250_000),
        ("valuation (real)", "/api/valuation/?basis=real_toman", 1.0, 250_000),
        ("valuation (usd)", "/api/valuation/?basis=usd_denominated", 1.0, 250_000),
        ("valuation (usdt)", "/api/valuation/?basis=usdt_denominated", 1.0, 250_000),
        ("snapshots 90d", "/api/snapshots/?days=90", 1.5, 500_000),
        ("snapshots 90d (real)", "/api/snapshots/?days=90&basis=real_toman", 1.5, 500_000),
        ("insights", "/api/insights/", 1.0, 100_000),
        ("latest prices", "/api/prices/latest/", 0.5, 250_000),
    ],
    "optimal": [
        ("my-optimal", "/api/optimization/my-optimal/", 3.0, 300_000),
        ("frontier", "/api/optimization/frontier/", 3.0, 300_000),
        ("robustness", "/api/optimization/robustness/", 3.0, 300_000),
    ],
    "universe": [
        ("best-overall", "/api/optimization/best-overall/", 3.0, 300_000),
        ("diversifiers", "/api/analytics/diversifiers/", 5.0, 300_000),
        ("benchmarks", "/api/analytics/benchmarks/", 3.0, 300_000),
    ],
    "analytics": [
        ("analytics", "/api/analytics/", 2.0, 300_000),
        ("performance", "/api/performance/", 2.0, 300_000),
    ],
    "ops": [
        ("overview", "/api/admin/overview/", 2.0, 400_000),
        ("workflows", "/api/admin/workflows/", 1.0, 200_000),
        ("logs", "/api/admin/logs/", 1.0, 200_000),
        ("archive states", "/api/admin/archive-states/", 1.0, 200_000),
        ("assets", "/api/admin/assets/", 1.5, 200_000),
    ],
}


class Command(BaseCommand):
    help = "Time the endpoints behind each UI page against real data."

    def add_arguments(self, parser):
        parser.add_argument("--email", help="User to authenticate as (default: first staff user).")
        parser.add_argument("--page", action="append", choices=sorted(PAGES),
                            help="Limit to one page; repeatable.")
        parser.add_argument("--host", default=None,
                            help="Host header; must be in ALLOWED_HOSTS.")
        parser.add_argument("--repeat", type=int, default=1,
                            help="Requests per endpoint; the best time is reported, "
                                 "so a cold cache does not masquerade as a slow view.")
        parser.add_argument("--fail-over", type=float, default=None,
                            help="Exit non-zero if any endpoint exceeds this many seconds.")
        parser.add_argument("--json", action="store_true", help="Emit JSON instead of a table.")

    def handle(self, *args, **opts):
        client = self._client(opts)
        pages = opts["page"] or sorted(PAGES)
        rows, worst = [], 0.0

        for page in pages:
            page_total = 0.0
            for label, url, budget, size_budget in PAGES[page]:
                best, status, size, err = self._time(client, url, opts["repeat"])
                page_total += best
                worst = max(worst, best)
                rows.append({
                    "page": page, "endpoint": label, "url": url,
                    "seconds": round(best, 3), "budget_seconds": budget,
                    "bytes": size, "budget_bytes": size_budget,
                    "status": status, "error": err,
                    "over_time": best > budget,
                    "over_size": size > size_budget,
                })
            rows.append({"page": page, "endpoint": "— page total —", "url": "",
                         "seconds": round(page_total, 3), "budget_seconds": None,
                         "bytes": None, "budget_bytes": None, "status": "",
                         "error": None, "over_time": False, "over_size": False})

        if opts["json"]:
            self.stdout.write(json.dumps(rows, indent=2))
        else:
            self._table(rows)

        limit = opts["fail_over"]
        if limit is not None and worst > limit:
            raise CommandError(f"slowest endpoint {worst:.2f}s exceeds --fail-over {limit}s")

    def _client(self, opts):
        from django.conf import settings
        from accounts.models import User
        from rest_framework_simplejwt.tokens import RefreshToken

        if opts["email"]:
            user = User.objects.filter(email=opts["email"]).first()
            if user is None:
                raise CommandError(f"no user with email {opts['email']}")
        else:
            # Ops endpoints are staff-only, so default to a staff user or half
            # the run reports 403 and looks misleadingly fast.
            user = User.objects.filter(role="admin").order_by("id").first()
            if user is None:
                raise CommandError("no staff user to authenticate as; pass --email")
        host = opts["host"] or (settings.ALLOWED_HOSTS[0] if settings.ALLOWED_HOSTS else "testserver")
        self.stdout.write(f"Authenticated as {user.email} (staff={user.is_staff}) via {host}\n")
        return Client(
            HTTP_AUTHORIZATION=f"Bearer {RefreshToken.for_user(user).access_token}",
            HTTP_HOST=host,
            HTTP_X_FORWARDED_PROTO="https",
        )

    def _time(self, client, url, repeat):
        best, status, size, err = None, None, 0, None
        for _ in range(max(1, repeat)):
            start = time.perf_counter()
            try:
                response = client.get(url)
                elapsed = time.perf_counter() - start
                status, size = response.status_code, len(response.content)
            except Exception as exc:  # a 500 that escapes the handler is a result too
                elapsed = time.perf_counter() - start
                status, err = "EXC", f"{type(exc).__name__}: {exc}"[:120]
            best = elapsed if best is None else min(best, elapsed)
        return best, status, size, err

    def _table(self, rows):
        self.stdout.write(f"\n{'page':10} {'endpoint':22} {'status':>6} {'secs':>8} {'KB':>8}  flags")
        self.stdout.write("-" * 74)
        for row in rows:
            if row["endpoint"].startswith("—"):
                self.stdout.write(
                    f"{'':10} {row['endpoint']:22} {'':>6} {row['seconds']:>8.2f} {'':>8}"
                )
                continue
            flags = []
            if row["over_time"]:
                flags.append(f"SLOW>{row['budget_seconds']}s")
            if row["over_size"]:
                flags.append(f"BIG>{row['budget_bytes'] // 1000}KB")
            if row["status"] not in (200, 201):
                flags.append(f"HTTP {row['status']}")
            if row["error"]:
                flags.append(row["error"])
            line = (f"{row['page']:10} {row['endpoint']:22} {str(row['status']):>6} "
                    f"{row['seconds']:>8.2f} {row['bytes'] / 1000:>8.1f}  {' '.join(flags)}")
            self.stdout.write(self.style.ERROR(line) if flags else line)
