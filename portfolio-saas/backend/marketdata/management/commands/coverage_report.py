"""Per-symbol date-gap report over the market-data warehouse.

Read-only companion to backfill_market_data: it never fetches or writes, it just
tells you *where* the archive is thin so you can target the next backfill. The
warehouse stores Jalali (Solar Hijri) date strings; we convert them to ordinals
to measure spans and count missing calendar days between the first and last row.

    manage.py coverage_report                 # tracked symbols, both warehouses
    manage.py coverage_report --source stock   # only DailyStockHistory
    manage.py coverage_report --symbol کاما     # one symbol (repeatable)
    manage.py coverage_report --worst 10        # 10 least-complete symbols only
    manage.py coverage_report --json            # machine-readable for scripting

"Coverage" = distinct days present / calendar days spanned. It is a density
measure inside the observed window, not absolute completeness — a symbol whose
history simply starts late still reads 100% if every day since is present. The
`span` and `first/last` columns give you the window itself.
"""
import json
from datetime import date

import jdatetime
from django.core.management.base import BaseCommand, CommandError

from marketdata.models import DailyStockHistory, GoldCurrencyHistory
from marketdata.tasks import tracked_brs_symbols, tracked_tse_symbols

SOURCES = ("stock", "gold")


def _to_ordinal(jalali: str):
    """Jalali 'YYYY-MM-DD' -> proleptic Gregorian ordinal, or None if unparseable."""
    try:
        y, m, d = (int(p) for p in jalali.split("-"))
        return jdatetime.date(y, m, d).togregorian().toordinal()
    except (ValueError, TypeError):
        return None


def _coverage(dates):
    """Reduce a list of Jalali date strings to a coverage record.

    Weekends are NOT subtracted: the Tehran bourse trades Sat-Wed, but gold/FX and
    crypto series run daily and holiday calendars differ per source, so a naive
    calendar span keeps the metric source-agnostic and conservative (it can only
    understate completeness, never overstate it).
    """
    ordinals = sorted({o for o in (_to_ordinal(d) for d in dates) if o is not None})
    if not ordinals:
        return None
    span = ordinals[-1] - ordinals[0] + 1
    present = len(ordinals)
    return {
        "present": present,
        "span": span,
        "missing": span - present,
        "coverage": present / span if span else 1.0,
        "first_ord": ordinals[0],
        "last_ord": ordinals[-1],
    }


def _fmt_ord(ordinal):
    """Gregorian ordinal -> Jalali 'YYYY-MM-DD' for display symmetry with storage."""
    return jdatetime.date.fromgregorian(date=date.fromordinal(ordinal)).strftime("%Y-%m-%d")


class Command(BaseCommand):
    help = "Report per-symbol historical-date coverage and gaps (read-only)."

    def add_arguments(self, parser):
        parser.add_argument("--symbol", action="append", default=[],
                            help="Limit to this symbol (repeatable). Default: tracked symbols.")
        parser.add_argument("--source", choices=SOURCES, default=None,
                            help=f"Restrict to one warehouse {SOURCES}. Default: both.")
        parser.add_argument("--worst", type=int, default=None,
                            help="Show only the N least-complete symbols.")
        parser.add_argument("--json", action="store_true",
                            help="Emit JSON instead of a table.")

    def handle(self, *args, **options):
        wanted = set(options["symbol"])
        source = options["source"]
        rows = []

        if source in (None, "stock"):
            rows += self._collect("stock", DailyStockHistory, tracked_tse_symbols(), wanted)
        if source in (None, "gold"):
            rows += self._collect("gold", GoldCurrencyHistory, tracked_brs_symbols(), wanted)

        if wanted:
            missing = wanted - {r["symbol"] for r in rows}
            for sym in sorted(missing):
                raise CommandError(f"Symbol {sym!r} not found in the selected warehouse(s).")

        # Least-complete first so the top of the report is where to backfill next.
        rows.sort(key=lambda r: (r["coverage"], -r["missing"]))
        if options["worst"] is not None:
            rows = rows[: options["worst"]]

        if options["json"]:
            self.stdout.write(json.dumps(rows, ensure_ascii=False, indent=2))
            return
        self._render_table(rows)

    def _collect(self, source, model, tracked, wanted):
        """One coverage record per symbol in `model`, scoped to tracked/wanted."""
        symbols = wanted or set(tracked)
        present_symbols = model.objects.values_list("symbol", flat=True).distinct()
        scope = [s for s in present_symbols if not symbols or s in symbols]

        out = []
        for symbol in scope:
            dates = list(
                model.objects.filter(symbol=symbol).values_list("date", flat=True)
            )
            cov = _coverage(dates)
            if cov is None:
                continue
            out.append({
                "source": source,
                "symbol": symbol,
                "present": cov["present"],
                "span": cov["span"],
                "missing": cov["missing"],
                "coverage": round(cov["coverage"], 4),
                "first": _fmt_ord(cov["first_ord"]),
                "last": _fmt_ord(cov["last_ord"]),
            })
        return out

    def _render_table(self, rows):
        if not rows:
            self.stdout.write("No warehouse rows for the selected symbols.")
            return
        header = f"{'SOURCE':<6} {'SYMBOL':<16} {'DAYS':>6} {'SPAN':>6} {'GAP':>6} {'COV':>6}  {'FIRST':<11} {'LAST':<11}"
        self.stdout.write(header)
        self.stdout.write("-" * len(header))
        for r in rows:
            pct = f"{r['coverage'] * 100:.0f}%"
            line = (f"{r['source']:<6} {r['symbol']:<16} {r['present']:>6} "
                    f"{r['span']:>6} {r['missing']:>6} {pct:>6}  "
                    f"{r['first']:<11} {r['last']:<11}")
            style = self.style.SUCCESS if r["coverage"] >= 0.95 else (
                self.style.WARNING if r["coverage"] >= 0.7 else self.style.ERROR)
            self.stdout.write(style(line))
        gaps = sum(r["missing"] for r in rows)
        self.stdout.write("-" * len(header))
        self.stdout.write(f"{len(rows)} symbols, {gaps} missing calendar days total.")
