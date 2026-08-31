"""Prove which market-data origins this host can actually reach.

The migration off BrsApi is gated on one fact per origin -- can we connect --
and that fact is a property of the *host*, not of the code. It differs between
a laptop, CI, and the Frankfurt VPS, and for TSETMC it is the entire reason the
stock lane is still on a paid reseller.

So this command answers it by measurement, on the machine in question:

    python manage.py check_egress                  # every origin
    python manage.py check_egress --verify-tsetmc  # probe TSETMC's endpoints
    python manage.py check_egress --compare        # do the sources agree?

`--verify-tsetmc` is the gate on `TSETMC_DIRECT_ENABLED`. `sources/tsetmc_direct.py`
was written without ever seeing a live response, because the origin is
unreachable from where it was written; this command is what turns those
documented shapes from hypotheses into verified fact. Do not flip the setting
until it passes.

`--compare` is the ongoing safety net rather than a one-off: it cross-checks
Nobitex against Wallex coin by coin and reports anything outside the tolerance.
A 10x disagreement means a unit regression, and a single frozen coin means a
dead feed -- the two failure modes that produce a plausible wrong number rather
than an obvious error.
"""
import time

from django.conf import settings
from django.core.management.base import BaseCommand

from marketdata.sources import SOURCES, nobitex, tgju, tsetmc_direct, wallex
from marketdata.sources.http import SourceError

OK = "  ok  "
FAIL = " FAIL "
SKIP = " skip "


class Command(BaseCommand):
    help = "Probe every market-data origin's reachability from this host."

    def add_arguments(self, parser):
        parser.add_argument(
            "--verify-tsetmc", action="store_true",
            help="Probe each direct-TSETMC endpoint and print what came back. "
                 "This is the gate on TSETMC_DIRECT_ENABLED.",
        )
        parser.add_argument(
            "--ins-code", default="35425587644337450",
            help="Instrument code for TSETMC probes (default: فولاد).",
        )
        parser.add_argument(
            "--compare", action="store_true",
            help="Cross-check Nobitex against Wallex and report disagreements.",
        )

    # ------------------------------------------------------------------ helpers

    def _probe(self, label, fn):
        """Run one probe, timing it, and never let a failure abort the sweep.

        A reachability report whose first failure stops the run is worth less
        than no report: the whole point is the shape of the pattern across
        origins, which is what identified the TSETMC block as geographic rather
        than as a route problem.
        """
        started = time.monotonic()
        try:
            detail = fn()
        except SourceError as exc:
            elapsed = time.monotonic() - started
            self.stdout.write(f"[{FAIL}] {label:<34} {elapsed:5.2f}s  {exc}")
            return False
        except Exception as exc:  # noqa: BLE001 - a probe must report, not raise
            elapsed = time.monotonic() - started
            self.stdout.write(
                f"[{FAIL}] {label:<34} {elapsed:5.2f}s  "
                f"unexpected {type(exc).__name__}: {exc}"
            )
            return False
        elapsed = time.monotonic() - started
        self.stdout.write(f"[{OK}] {label:<34} {elapsed:5.2f}s  {detail}")
        return True

    # --------------------------------------------------------------- probes

    def _probe_tgju(self):
        current = tgju.fetch_live()
        rows = tgju.live_rows(current)
        mapped = {r["symbol"] for r in rows}
        missing = sorted(set(tgju.BRS_TO_SLUG) - mapped)
        detail = f"{len(current)} instruments, {len(rows)}/{len(tgju.BRS_TO_SLUG)} mapped"
        if missing:
            # Missing here usually means the staleness gate fired, which is a
            # finding, not noise -- it is how a retired slug announces itself.
            detail += f"; refused/absent: {', '.join(missing)}"
        return detail

    def _probe_wallex(self):
        symbols = wallex.fetch_markets()
        rows = wallex.live_rows(symbols)
        tmn = sum(1 for r in rows if r["quote"] == "TMN")
        return f"{len(symbols)} symbols, {len(rows)} priced ({tmn} Toman-quoted)"

    def _probe_wallex_history(self):
        payload = wallex.fetch_ohlc("USDTTMN", resolution="D")
        rows = wallex.candles(payload)
        if not rows:
            return "no candles"
        span = f"{rows[0]['ts']}..{rows[-1]['ts']}"
        return f"{len(rows)} daily candles in one request ({span})"

    def _probe_nobitex(self):
        rows = nobitex.live_rows()
        return f"{len(rows)} live pairs"

    def _probe_brsapi(self):
        """The incumbent, probed the same way so the comparison is like-for-like."""
        import requests
        response = requests.get(
            "https://Api.BrsApi.ir/Market/Gold_Currency.php",
            params={"key": settings.BRS_API_KEY or "probe"},
            timeout=(5, 15),
        )
        return f"HTTP {response.status_code}, {len(response.content)} bytes"

    # ----------------------------------------------------------------- handle

    def handle(self, *args, **options):
        self.stdout.write(self.style.MIGRATE_HEADING(
            "\nMarket-data egress report"
        ))
        proxy = getattr(settings, "IRAN_EGRESS_PROXY", "")
        self.stdout.write(
            f"  IRAN_EGRESS_PROXY: {proxy or '(unset -- Iran-blocked origins will fail)'}"
        )
        self.stdout.write("")

        results = {}
        results["tgju"] = self._probe("tgju live board", self._probe_tgju)
        results["wallex"] = self._probe("wallex markets", self._probe_wallex)
        self._probe("wallex USDTTMN daily history", self._probe_wallex_history)
        results["nobitex"] = self._probe("nobitex live stats", self._probe_nobitex)
        results["brsapi"] = self._probe("brsapi (incumbent)", self._probe_brsapi)

        # TSETMC is probed unconditionally, because "still blocked" is the
        # single most useful line in this report -- it is what tells the
        # operator whether the egress work has landed.
        results["tsetmc"] = self._probe(
            "tsetmc direct",
            lambda: f"market watch returned "
                    f"{len(tsetmc_direct.fetch_endpoint('market_watch') or [])} keys",
        )

        if options["verify_tsetmc"]:
            self._verify_tsetmc(options["ins_code"])
        if options["compare"]:
            self._compare()

        self._summary(results)

    def _verify_tsetmc(self, ins_code):
        self.stdout.write(self.style.MIGRATE_HEADING(
            f"\nDirect-TSETMC endpoint verification (insCode={ins_code})"
        ))
        self.stdout.write(
            "  These shapes were written without ever seeing a live response.\n"
            "  Do not set TSETMC_DIRECT_ENABLED=1 until every line below is ok.\n"
        )
        passed = 0
        for name in tsetmc_direct.ENDPOINTS:
            def probe(n=name):
                payload = tsetmc_direct.fetch_endpoint(n, ins_code)
                if isinstance(payload, dict):
                    return f"keys: {sorted(payload)[:5]}"
                return f"{type(payload).__name__} of {len(payload)}"
            passed += bool(self._probe(f"  {name}", probe))
        total = len(tsetmc_direct.ENDPOINTS)
        style = self.style.SUCCESS if passed == total else self.style.WARNING
        self.stdout.write(style(f"\n  {passed}/{total} TSETMC endpoints verified."))

    def _compare(self):
        self.stdout.write(self.style.MIGRATE_HEADING(
            "\nCross-source agreement (Nobitex vs Wallex, normalised to Toman)"
        ))
        try:
            agreements, disagreements = nobitex.cross_check(
                nobitex.live_rows(), wallex.live_rows()
            )
        except SourceError as exc:
            self.stdout.write(self.style.ERROR(f"  unavailable: {exc}"))
            return
        for row in agreements:
            self.stdout.write(
                f"  [{OK}] {row['coin']:<6} nobitex={row['nobitex_toman']:>18,.0f} "
                f"wallex={row['wallex_toman']:>18,.0f}  {row['spread'] * 100:5.2f}%"
            )
        for row in disagreements:
            self.stdout.write(self.style.ERROR(
                f"  [{FAIL}] {row['coin']:<6} nobitex={row['nobitex_toman']:>18,.0f} "
                f"wallex={row['wallex_toman']:>18,.0f}  {row['spread'] * 100:5.2f}%"
            ))
        if disagreements:
            self.stdout.write(self.style.WARNING(
                f"\n  {len(disagreements)} coin(s) outside tolerance. A ~900% spread "
                "is a Rial/Toman unit regression, not a market move."
            ))
        else:
            self.stdout.write(self.style.SUCCESS(
                f"\n  All {len(agreements)} shared coins agree within tolerance."
            ))

    def _summary(self, results):
        self.stdout.write(self.style.MIGRATE_HEADING("\nMigration readiness"))
        for key, meta in SOURCES.items():
            reachable = results.get(key)
            enabled = getattr(settings, meta["setting"], False)
            if reachable is None:
                state = "not probed"
            elif reachable and enabled:
                state = "ACTIVE"
            elif reachable:
                state = f"reachable, disabled ({meta['setting']}=0)"
            elif meta["requires_egress"]:
                state = "BLOCKED - needs IRAN_EGRESS_PROXY"
            else:
                state = "unreachable"
            self.stdout.write(f"  {meta['label']:<44} {state}")
            self.stdout.write(f"  {'':44} replaces: {meta['replaces']}")
        self.stdout.write("")
