"""Print the latency rollup: `manage.py perf_report --days 7 --source api`.

`--since/--until` (ISO datetimes) bound the window, so the same command run
over the week before and the week after a deploy gives a before/after table.
`--flush` first copies the live Redis totals so the current hour is included.
"""
import json
from datetime import datetime, timedelta, timezone as dt_timezone

from django.core.management.base import BaseCommand
from django.utils.dateparse import parse_datetime

from perf import report


def _fmt(value):
    return "-" if value is None else str(value)


class Command(BaseCommand):
    help = "Summarise request/page latency by route, market state, day type and hour."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=float, default=7)
        parser.add_argument("--since")
        parser.add_argument("--until")
        parser.add_argument("--source", choices=["api", "client_api", "client_page", "client_boot"])
        parser.add_argument("--limit", type=int, default=25)
        parser.add_argument("--flush", action="store_true")
        parser.add_argument("--json", action="store_true")

    def handle(self, *args, **opts):
        if opts["flush"]:
            report.flush()
        def aware(raw):
            value = parse_datetime(raw) if raw else None
            if value is not None and value.tzinfo is None:
                value = value.replace(tzinfo=dt_timezone.utc)
            return value
        since = aware(opts["since"]) or datetime.now(dt_timezone.utc) - timedelta(days=opts["days"])
        data = report.summarize(since=since, until=aware(opts["until"]),
                                source=opts["source"], limit=opts["limit"])
        if opts["json"]:
            self.stdout.write(json.dumps(data, indent=2, ensure_ascii=False))
            return

        o = data["overall"]
        self.stdout.write(f"window {data['since']} .. {data['until']}  source={data['source']}")
        self.stdout.write(f"overall: n={o['count']} avg={o['avg_ms']}ms p50<={_fmt(o['p50_ms'])} "
                          f"p95<={_fmt(o['p95_ms'])} max={o['max_ms']} err={o['error_rate']}")
        for title, key in (("market state", "by_market_state"), ("day type", "by_day_type"),
                           ("tehran hour", "by_tehran_hour")):
            self.stdout.write(f"\nby {title}:")
            for name, row in data[key].items():
                self.stdout.write(f"  {name:>15}  n={row['count']:<7} avg={row['avg_ms']:<8} "
                                  f"p95<={_fmt(row['p95_ms']):<6} db_q={row['avg_db_queries']:<6} "
                                  f"inflight={row['avg_inflight']}")
        self.stdout.write("\nroutes (by total time):")
        header = f"  {'source':<11} {'method':<6} {'n':>7} {'avg':>8} {'p95<=':>6} {'max':>8} {'dbq':>6} {'db_ms':>7} {'err':>6}  route"
        self.stdout.write(header)
        for r in data["routes"]:
            self.stdout.write(
                f"  {r['source']:<11} {r['method']:<6} {r['count']:>7} {r['avg_ms']:>8} "
                f"{_fmt(r['p95_ms']):>6} {r['max_ms']:>8} {r['avg_db_queries']:>6} "
                f"{r['avg_db_ms']:>7} {r['error_rate']:>6}  {r['route']}"
            )
