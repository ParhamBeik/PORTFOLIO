"""Read-only warehouse audit: unit coherence, date conformance, ledger drift.

Strictly read-only -- there is no --apply. It emits a CSV manifest plus its
SHA-256 so a later repair step can be locked to exactly the rows examined here,
the same two-phase idiom `backfill_validation` uses.

Why SQL rather than the existing per-series helpers: `nightly_series_validation`
already walks one symbol at a time with `detect_factor_ratio_actions` /
`screen_series`. This command answers the *cross-cutting* questions those cannot
-- "do many unrelated symbols step by the same factor on the same day?" -- which
is precisely what separates a unit error from a capital increase.

Verdicts:
  unit_error         both adj and unadj move together, or >=3 symbols share a
                     date and factor -- a real corporate action does neither
  corporate_action   unadj steps while adj stays flat: legitimate, leave alone
  suspect            a jump that fits neither pattern; needs provider re-fetch
"""
import csv
import hashlib
import re
from collections import Counter

from django.core.management.base import BaseCommand
from django.db import connection

# A capital increase moves the unadjusted series only; the adjusted series is
# back-corrected and stays flat. Both moving means the underlying number changed.
ADJ_FLAT_CEILING = 1.5
# Unit errors are powers of ten. Bracket loosely: the same-day move rides along.
UNIT_BAND = (9.0, 11.0)
# One symbol can legitimately do a 10x capital increase. Three on one day at the
# same factor is a feed defect.
COINCIDENCE_MIN_SYMBOLS = 3
# A mis-scaled ingest is transient -- days, not years. A longer excursion is a
# real regime (cascading capital increases), so it is left alone.
MAX_EXCURSION_ROWS = 60
# Back-adjustment drives decades-old closes toward zero (کاما's adjusted close is
# 0.8 against a raw 7303) and onto a coarse grid, where 0.8 -> 8.0 is a genuine
# provider value, not a mis-scale. A provider re-fetch confirmed 142 of 142 such
# کاما rows as correct -- every one a false positive at the old 1.0 floor. Below
# this the "exactly 10x" test carries no information, so the row is not judged.
MIN_COMPARABLE_PRICE = 100.0
# Centred window for the local median a row is judged against. It must stay
# wide enough that a mis-scaled RUN cannot outvote the healthy rows around it:
# ثجوان was wrong for 10 consecutive sessions, which at window=21 made the bad
# level the median and inverted the verdict, flagging the one correct row. At 41
# a run has to exceed 20 sessions before it can do that.
LOCAL_WINDOW = 41
# A mis-scaled row is its true value over exactly ten. Real market moves of
# "roughly ten times" are 9.7x or 10.4x and must not be touched, so the ratio has
# to land within 1% of the power of ten.
EXACTNESS_TOLERANCE = 0.01
JALALI_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _rows(sql, params=None):
    with connection.cursor() as cur:
        cur.execute(sql, params or [])
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


class Command(BaseCommand):
    help = "Read-only audit of warehouse unit coherence, date formats and ledger drift."

    def add_arguments(self, parser):
        parser.add_argument("--manifest-path", default="warehouse_audit.csv")
        parser.add_argument(
            "--check", action="append",
            help="Run only named checks (repeatable): units, crosstable, gold, "
                 "candletable, dates, ledger, census",
        )

    def handle(self, *args, **opts):
        only = set(opts["check"] or [])
        findings = []
        for name, fn in (
            ("units", self.check_unit_steps),
            ("crosstable", self.check_cross_table),
            ("gold", self.check_gold_units),
            ("candletable", self.check_candle_table_purity),
            ("dates", self.check_date_conformance),
            ("ledger", self.check_ledger_drift),
            ("census", self.census),
        ):
            if only and name not in only:
                continue
            self.stdout.write(f"[{name}] ...")
            found = fn()
            findings.extend(found)
            self.stdout.write(f"[{name}] {len(found)} finding(s)")

        path = opts["manifest_path"]
        # `corrected` is what a repair would write. Emitting it here, rather than
        # letting a repair step re-derive it, is what makes the manifest hash a
        # real lock: the reviewed number is the applied number.
        cols = ["check", "table", "symbol", "date", "value", "corrected",
                "verdict", "evidence"]
        with open(path, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=cols)
            w.writeheader()
            for f in findings:
                w.writerow({c: f.get(c, "") for c in cols})
        with open(path, "rb") as fh:
            digest = hashlib.sha256(fh.read()).hexdigest()

        self.stdout.write("")
        self.stdout.write("=== verdict summary ===")
        for verdict, n in Counter(f["verdict"] for f in findings).most_common():
            self.stdout.write(f"  {verdict:22} {n}")
        self.stdout.write("")
        self.stdout.write(f"manifest : {path}")
        self.stdout.write(f"sha256   : {digest}")

    # ---- checks -----------------------------------------------------------

    def check_unit_steps(self):
        """Rows whose adj/unadj factor is 10x off that symbol's own baseline.

        The provider publishes both a raw and a back-adjusted close, so
        `factor = adj / unadj` is the symbol's own control. Between corporate
        actions the factor is constant, whatever its value; a capital increase
        moves it permanently for the remaining history. A row written in the
        wrong unit moves it by exactly a power of ten for that row alone.

        Comparing each row's factor against the symbol's MEDIAN factor is what
        makes this robust: it needs no threshold on the price move itself, so a
        genuine 10x capital increase (which shifts a contiguous run, dragging
        the median with it) is not mistaken for a one-row unit slip.
        """
        rows = _rows(
            """
            SELECT u.symbol, left(u.date_time,10) AS d,
                   u.close_price::float AS unadj, a.close_price::float AS adj,
                   (a.close_price/u.close_price)::float AS factor
            FROM marketdata_marketcandle u
            JOIN marketdata_marketcandle a
              ON a.symbol=u.symbol AND a.date_time=u.date_time
             AND a.timeframe='1d_adj' AND a.close_price>0
            WHERE u.timeframe='1d_unadj' AND u.close_price>0
            ORDER BY u.symbol, u.date_time
            """
        )
        by_symbol = {}
        for r in rows:
            by_symbol.setdefault(r["symbol"], []).append(r)
        out = []
        for symbol, series in by_symbol.items():
            out.extend(self._classify_spikes(symbol, series))

        # CORROBORATION GATE. A single symbol deviating on its own is not enough
        # to rewrite a price: a provider re-fetch of کاما returned 153 such rows
        # and confirmed the STORED value every single time -- 153 false
        # positives, zero real defects. Its 20 years of compounding capital
        # increases make adj/unadj unstable, so the local median is a poor
        # baseline and "exactly 10x" fires on legitimate values.
        #
        # A genuine mis-scale is a feed event: it hits many unrelated symbols on
        # one date (23, 27 and 87 symbols on 1405-05-10/11/12). So only a shared
        # date is repairable. A lone deviation is still reported, as `suspect`,
        # with no `corrected` value -- visible, but never rewritten on a hunch.
        per_date = Counter(f["date"] for f in out if f["verdict"] == "unit_error")
        for f in out:
            if f["verdict"] != "unit_error":
                continue
            peers = per_date[f["date"]]
            if peers >= COINCIDENCE_MIN_SYMBOLS:
                f["evidence"] += f"; {peers} symbols that day (feed-wide event)"
            else:
                f["verdict"] = "suspect"
                f["corrected"] = ""
                f["evidence"] += (
                    f"; only {peers} symbol(s) that day -- uncorroborated, "
                    "needs a provider re-fetch before any repair"
                )
        return out

    @staticmethod
    def _classify_spikes(symbol, series):
        """Flag rows whose adj/unadj factor is EXACTLY a power of ten off local.

        Three ideas carry this, and all three are needed:

        * The factor, not the price. `adj/unadj` on the SAME date is immune to
          market movement -- it is constant between corporate actions whatever
          its value. Comparing prices instead re-introduces drift, which is what
          made اخابر's real 68.5-vs-685 slip read as 9.64x and escape.
        * Local median (centred window) as the baseline. It tracks a genuine
          rebase within a few rows, so a capital increase is absorbed rather than
          flagged, and a short mis-scaled run cannot outvote its neighbours.
        * Exactness. A mis-scaled row is its true value over exactly ten
          (1903 -> 190.3). A real move of "roughly ten times" lands on 9.7x and
          is left alone. This is what stopped کاما's ordinary volatility from
          producing ~140 false rows a year.

        Which column is at fault is then decided by asking which one moved
        against its own local median -- that is what makes the finding repairable.
        """
        import statistics

        out = []
        n = len(series)
        factors = [r["factor"] for r in series]
        half = LOCAL_WINDOW // 2
        for i in range(n):
            lo, hi = max(0, i - half), min(n, i + half + 1)
            neighbours = factors[lo:i] + factors[i + 1:hi]
            if len(neighbours) < 4:
                continue
            med = statistics.median(neighbours)
            if med <= 0 or factors[i] <= 0:
                continue
            rel = factors[i] / med
            for target in (10.0, 0.1):
                if abs(rel / target - 1.0) > EXACTNESS_TOLERANCE:
                    continue
                # factor = adj/unadj. factor 10x high means adj is 10x high OR
                # unadj is 10x low; break the tie on each column's own local
                # median, which market drift cannot fake at this exactness.
                for col in ("adj", "unadj"):
                    vals = [r[col] for r in series]
                    cn = vals[lo:i] + vals[i + 1:hi]
                    cmed = statistics.median(cn)
                    if cmed <= 0 or vals[i] <= 0:
                        continue
                    crel = vals[i] / cmed
                    if not (0.05 <= crel <= 0.2 or 5.0 <= crel <= 20.0):
                        continue
                    # Deep back-adjusted history sits on a grid where a 10x step
                    # between neighbouring legitimate values is ordinary. Judge
                    # only where the prices are large enough for the ratio to
                    # mean something.
                    if max(vals[i], cmed) < MIN_COMPARABLE_PRICE:
                        continue
                    corrected = vals[i] * 10 if crel < 1 else vals[i] / 10
                    label = "10x LOW" if crel < 1 else "10x HIGH"
                    out.append({
                        "check": "units",
                        "table": f"marketdata_marketcandle[1d_{'adj' if col == 'adj' else 'unadj'}]",
                        "symbol": symbol, "date": series[i]["d"],
                        "value": vals[i], "corrected": f"{corrected:g}",
                        "verdict": "unit_error",
                        "evidence": (
                            f"factor {factors[i]:g} vs local median {med:g} "
                            f"(exactly {target:g}x); {col}={vals[i]:g} vs its own "
                            f"local median {cmed:g} => {col} is {label}; "
                            f"corrected value would be {corrected:g}"
                        ),
                    })
                    break
                break
        # Permanent rebases: reported so the discrimination is auditable, never
        # repaired. A capital increase steps the factor once and it stays.
        for i in range(1, n):
            if factors[i - 1] <= 0:
                continue
            step = factors[i] / factors[i - 1]
            if not (step >= UNIT_BAND[0] or step <= 1.0 / UNIT_BAND[0]):
                continue
            fwd = factors[i + 1:i + 1 + LOCAL_WINDOW]
            if fwd and abs(statistics.median(fwd) / factors[i] - 1.0) < 0.1:
                out.append({
                    "check": "units", "table": "marketdata_marketcandle[1d_unadj]",
                    "symbol": symbol, "date": series[i]["d"],
                    "value": series[i]["unadj"], "verdict": "corporate_action",
                    "evidence": (f"factor {factors[i-1]:g} -> {factors[i]:g} and "
                                 f"stays there (permanent rebase) -- legitimate"),
                })
        return out

    def check_cross_table(self):
        """MarketCandle and DailyStockHistory must agree for the same symbol+day.

        The adjusted candle is pulled in as an arbiter. A `corrected` value is
        only proposed when the OTHER two sources agree against the unadjusted
        candle -- two votes to one. Where they disagree three ways the row is
        reported but left unrepairable, because there is nothing to prefer.
        """
        rows = _rows(
            """
            SELECT c.symbol, left(c.date_time,10) AS d,
                   c.close_price::float AS candle, h.pl::float AS hist,
                   a.close_price::float AS adj,
                   (c.close_price/NULLIF(h.pl,0))::float AS ratio
            FROM marketdata_marketcandle c
            JOIN marketdata_dailystockhistory h
              ON h.symbol=c.symbol AND h.date=left(c.date_time,10)
            LEFT JOIN marketdata_marketcandle a
              ON a.symbol=c.symbol AND a.date_time=c.date_time
             AND a.timeframe='1d_adj' AND a.close_price>0
            WHERE c.timeframe='1d_unadj' AND c.close_price>0 AND h.pl>0
              AND (c.close_price/h.pl > 1.01 OR c.close_price/h.pl < 0.99)
            ORDER BY abs(ln(c.close_price/h.pl)) DESC
            LIMIT 500
            """
        )
        out = []
        for r in rows:
            ratio = r["ratio"] or 0
            is_unit = 9 <= ratio <= 11 or 0.09 <= ratio <= 0.11
            corrected, table = "", "marketcandle_vs_dailystockhistory"
            adj = r["adj"]
            # Arbiter: does the adjusted candle side with DailyStockHistory?
            if is_unit and adj and abs(adj / r["hist"] - 1.0) <= 0.01:
                corrected = f"{r['candle'] * 10:g}" if ratio < 1 else f"{r['candle'] / 10:g}"
                table = "marketdata_marketcandle[1d_unadj]"
            out.append({
                "check": "crosstable", "table": table,
                "symbol": r["symbol"], "date": r["d"], "value": r["candle"],
                "corrected": corrected,
                "verdict": "unit_error" if is_unit else "suspect",
                "evidence": (
                    f"unadj_candle={r['candle']:g} history_pl={r['hist']:g} "
                    f"ratio={ratio:.3f}"
                    + (f"; adjusted={adj:g} agrees with history -> candle is the "
                       f"odd one out" if corrected else
                       f"; adjusted={adj:g} does not arbitrate, left unrepaired"
                       if adj else "; no adjusted row to arbitrate")
                ),
            })
        return out

    def check_gold_units(self):
        """Declared unit label vs actual magnitude, per gold/FX symbol."""
        rows = _rows(
            """
            SELECT symbol, unit, count(*) n,
                   min(close_price)::float mn, max(close_price)::float mx,
                   (percentile_cont(0.5) WITHIN GROUP (ORDER BY close_price))::float med
            FROM marketdata_goldcurrencyhistory
            WHERE close_price>0 GROUP BY symbol, unit ORDER BY symbol
            """
        )
        out = []
        by_symbol = Counter(r["symbol"] for r in rows)
        for r in rows:
            problems = []
            if not r["unit"]:
                problems.append("blank unit label -- scale unverifiable")
            if by_symbol[r["symbol"]] > 1:
                problems.append(f"symbol carries {by_symbol[r['symbol']]} different unit labels")
            # A band spanning >100x on a currency is contamination, not drift.
            if r["med"] and r["mx"] / max(r["med"], 1e-9) > 100:
                problems.append(f"max {r['mx']:.0f} is {r['mx']/r['med']:.0f}x the median {r['med']:.0f}")
            for p in problems:
                out.append({
                    "check": "gold", "table": "marketdata_goldcurrencyhistory",
                    "symbol": r["symbol"], "date": "", "value": r["med"],
                    "verdict": "unit_error" if "blank" in p else "suspect",
                    "evidence": f"unit={r['unit']!r} n={r['n']} min={r['mn']:.2f} max={r['mx']:.2f}; {p}",
                })
        return out

    def check_candle_table_purity(self):
        """MarketCandle is Rial TSE data; a BRS symbol in it is a Toman row."""
        rows = _rows(
            """
            SELECT c.symbol, c.timeframe, c.date_time AS d, c.close_price
            FROM marketdata_marketcandle c
            WHERE c.symbol IN (
              SELECT brs_symbol FROM portfolio_asset WHERE brs_symbol <> '')
            ORDER BY c.symbol, c.date_time
            """
        )
        return [{
            "check": "candletable", "table": "marketdata_marketcandle",
            "symbol": r["symbol"], "date": r["d"], "value": r["close_price"],
            "verdict": "unit_error",
            "evidence": f"BRS (Toman) symbol in the Rial candle table, timeframe={r['timeframe']}",
        } for r in rows]

    def check_date_conformance(self):
        """Jalali shape, plausible year, and date_time suffix consistency."""
        out = []
        for table, col in (
            ("marketdata_marketcandle", "date_time"),
            ("marketdata_dailystockhistory", "date"),
            ("marketdata_goldcurrencyhistory", "date"),
        ):
            for r in _rows(
                f"""
                SELECT left({col},10) AS d, count(*) n FROM {table}
                WHERE left({col},10) !~ '^[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}$'
                   OR substring({col} from 1 for 4)::int NOT BETWEEN 1300 AND 1500
                GROUP BY 1 ORDER BY n DESC LIMIT 50
                """
            ):
                out.append({
                    "check": "dates", "table": table, "symbol": "", "date": r["d"],
                    "value": r["n"], "verdict": "bad_date",
                    "evidence": f"{r['n']} rows with non-Jalali or out-of-range {col}",
                })
        # Mixed "YYYY-MM-DD" vs "YYYY-MM-DD 00:00:00" in the same table breaks
        # string-compared as_of bounds.
        shapes = _rows(
            """
            SELECT length(date_time) len, count(*) n
            FROM marketdata_marketcandle GROUP BY 1 ORDER BY n DESC
            """
        )
        if len(shapes) > 1:
            out.append({
                "check": "dates", "table": "marketdata_marketcandle", "symbol": "",
                "date": "", "value": len(shapes), "verdict": "suspect",
                "evidence": "mixed date_time widths: "
                            + ", ".join(f"len={s['len']}:{s['n']}" for s in shapes),
            })
        return out

    def check_ledger_drift(self):
        """Stored projections vs a ledger replay, per account.

        Delegates to `portfolio.services.ledger.projection_drift` -- the same
        replay `reconcile_ledger` uses -- rather than re-deriving the rules for
        reversals, real-estate baselines and cash flows in SQL.
        """
        from portfolio.models import Account
        from portfolio.services.ledger import projection_drift

        out = []
        for account in Account.objects.select_related("user"):
            for d in projection_drift(account):
                out.append({
                    "check": "ledger", "table": "portfolio_" + d["kind"],
                    "symbol": d.get("asset", ""), "date": "", "value": d["stored"],
                    "verdict": "ledger_drift",
                    "evidence": f"account={account.name} stored={d['stored']} ledger={d['ledger']}",
                })
        # Liability is Decimal(20,4) while sibling Toman columns are (24,4).
        for r in _rows(
            "SELECT id, amount_tomans::float amt FROM portfolio_liability "
            "WHERE amount_tomans > 1e15"
        ):
            out.append({
                "check": "ledger", "table": "portfolio_liability", "symbol": "",
                "date": "", "value": r["amt"], "verdict": "suspect",
                "evidence": "approaching Decimal(20,4) ceiling; siblings are (24,4)",
            })
        return out

    def census(self):
        """Cheap per-table shape census so no table goes unexamined."""
        out = []
        for table, price_col in (
            ("marketdata_stocktransactiontick", "price"),
            ("marketdata_reallegalhistory", None),
            ("marketdata_codalannouncement", None),
            ("marketdata_shareholderrecord", None),
            ("marketdata_cryptohistory", "close_price_usd"),
            ("marketdata_commodityhistory", "close_price"),
            ("marketdata_marketindexdata", None),
            ("marketdata_etfnavhistory", None),
            ("marketdata_optioncontracthistory", None),
            ("marketdata_stocksymbolmetadata", None),
        ):
            n = _rows(f"SELECT count(*) c FROM {table}")[0]["c"]
            evidence = f"rows={n}"
            verdict = "census"
            if price_col and n:
                s = _rows(
                    f"SELECT count(*) FILTER (WHERE {price_col} IS NULL) nulls, "
                    f"count(*) FILTER (WHERE {price_col} <= 0) nonpos, "
                    f"min({price_col})::float mn, max({price_col})::float mx "
                    f"FROM {table}"
                )[0]
                evidence += (f" nulls={s['nulls']} nonpositive={s['nonpos']} "
                             f"min={s['mn']} max={s['mx']}")
                if s["nonpos"] or s["nulls"]:
                    verdict = "suspect"
            out.append({
                "check": "census", "table": table, "symbol": "", "date": "",
                "value": n, "verdict": verdict, "evidence": evidence,
            })
        return out
