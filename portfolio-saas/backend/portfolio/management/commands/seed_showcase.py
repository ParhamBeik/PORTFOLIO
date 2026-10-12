"""Build the showcase account: a Tehran family's last 18 months, on real prices.

    python manage.py seed_showcase                 # first time: asks for a password
    python manage.py seed_showcase --reset         # wipe the demo's data and rebuild it
    python manage.py seed_showcase --dry-run       # do everything, print the report, keep nothing

Every write goes through the path a person's would: trades through
`create_ledger_entry` with no price given, so each one is booked at the
warehouse's real close for that day; the apartment, the mortgage, the watchlist
and the research question through the app's own API views. Nothing here invents
a price. A trade whose date has no price is moved back a few days, then skipped
and reported -- never booked at a made-up number.

The password is set only when the user is created, at a hidden prompt; `--reset`
keeps it. The research run is real (it calls the configured provider once and is
billed like any other); it is skipped when one already exists, so a rebuild never
pays twice.
"""
import datetime as dt
import getpass
from decimal import ROUND_DOWN, Decimal
from zoneinfo import ZoneInfo

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.urls import resolve
from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate

from accounts.models import User
from marketdata.currency import holding_value_to_toman
from portfolio.models import Account, Asset, Holding, LedgerEntry, Snapshot, WatchlistItem
from portfolio.services.catalog import ensure_asset
from portfolio.services.ledger import (
    LedgerError,
    PriceResolutionError,
    create_ledger_entry,
    record_house_mark,
    resolve_historical_price,
)

TEHRAN = ZoneInfo("Asia/Tehran")
DEFAULT_EMAIL = "demo@parhambm.ir"
MONTHS = 18
# Sat-Wed: a date the TSE could have traded on.
TRADING_WEEKDAYS = {5, 6, 0, 1, 2}

# (month offset, account, kind, asset ref, toman amount or quantity)
# Asset refs: ("tse", symbol) | ("brs", symbol) | ("key", seeded asset key).
# BUY/SELL amounts are a Toman budget turned into whole units at that day's close;
# SELL with a float < 1 sells that share of the position.
STORY = [
    (0, "family", "opening_cash", None, 4_000_000_000),
    (0, "family", "buy", ("tse", "فولاد"), 450_000_000),
    (0, "family", "buy", ("tse", "فملی"), 400_000_000),
    (0, "family", "buy", ("tse", "وبملت"), 300_000_000),
    (0, "family", "buy", ("tse", "وغدیر"), 300_000_000),
    (0, "family", "buy", ("key", "emami_coin"), 600_000_000),
    (0, "savings", "opening_cash", None, 1_800_000_000),
    (0, "savings", "buy", ("key", "emami_coin"), 700_000_000),
    (0, "savings", "buy", ("key", "half_coin"), 300_000_000),
    (0, "savings", "buy", ("key", "gold_18k_gram"), 250_000_000),
    (0, "savings", "buy", ("key", "usd_cash"), 400_000_000),
    (1, "family", "buy", ("tse", "خساپا"), 200_000_000),  # halted all of Farvardin 1404
    (3, "family", "deposit", None, 500_000_000),
    (3, "family", "buy", ("tse", "شستا"), 250_000_000),
    (3, "family", "buy", ("tse", "کگل"), 200_000_000),
    (6, "family", "dividend", ("tse", "فولاد"), 38_000_000),
    (6, "family", "sell", ("tse", "خساپا"), 0.5),
    (6, "family", "buy", ("tse", "اخابر"), 200_000_000),
    (8, "savings", "buy", ("key", "usd_cash"), 200_000_000),
    (9, "family", "buy", ("brs", "BTC"), 150_000_000),
    (9, "family", "buy", ("key", "quarter_coin"), 180_000_000),
    (12, "family", "deposit", None, 300_000_000),
    (12, "family", "buy", ("tse", "فارس"), 250_000_000),
    (12, "family", "sell", ("tse", "وبملت"), 0.3),
    (14, "savings", "sell", ("key", "emami_coin"), 0.25),
    (15, "family", "dividend", ("tse", "فملی"), 42_000_000),
    (15, "family", "fee", None, 2_500_000),
    (15, "family", "withdrawal", None, 150_000_000),
    (17, "family", "buy", ("key", "usdt_irt"), 60_000_000),
]
# Apartment: (month offset, price per m² in millions of Toman). 92 m² in Tehran.
APARTMENT = {"name": "Apartment, Tehran", "area_sqm": 92, "marks": [(0, 95), (6, 108), (12, 124), (17, 138)]}
MORTGAGE = {"label": "Apartment mortgage", "lender": "Bank Maskan", "principal": 2_400_000_000,
            "rate": 23, "term": 120}
WATCHLIST = ["خودرو", "شپنا", "شبندر"]
RESEARCH = {"symbol": "فولاد", "question": "How have monthly sales changed over the last year?"}


class Command(BaseCommand):
    help = "Create or rebuild the showcase account with 18 months of real-priced history."

    def add_arguments(self, parser):
        parser.add_argument("--email", default=DEFAULT_EMAIL)
        parser.add_argument("--reset", action="store_true", help="Wipe this user's portfolio data and rebuild it.")
        parser.add_argument("--dry-run", action="store_true", help="Build everything, report, then roll back.")
        parser.add_argument("--skip-research", action="store_true", help="Do not call the AI provider.")

    def handle(self, *args, email, reset, dry_run, skip_research, **options):
        self.notes = []
        user = User.objects.filter(email=User.objects.normalize_email(email)).first()
        if user is not None and not reset:
            raise CommandError(f"{email} already exists. Rebuild it with --reset.")
        password = None
        if user is None and not dry_run:
            password = self._ask_password()

        with transaction.atomic():
            if user is None:
                user = User.objects.create_user(email, password, first_name="Demo", last_name="Family")
                if password is None:
                    user.set_unusable_password()
                    user.save(update_fields=["password"])
            else:
                self._wipe(user)
            start = self._start_day()
            accounts = self._accounts(user, start)
            for month, account_key, kind, ref, amount in STORY:
                self._story_entry(accounts[account_key], month, start, kind, ref, amount)
            house = self._apartment(user, accounts["family"], start)
            if house is not None:
                self._mortgage(user, accounts["family"], house, start)
            for symbol in WATCHLIST:
                self._call(user, "post", "/api/watchlist/", {"symbol": symbol}, label=f"watch {symbol}")
            snapshots = self._snapshots(user, list(accounts.values()), start)
            if not (dry_run or skip_research):
                self._research(user)
            self._report(user, accounts, snapshots)
            if dry_run:
                transaction.set_rollback(True)
                self.stdout.write(self.style.WARNING("Dry run: everything above was rolled back."))

    # -- setup -------------------------------------------------------------

    def _ask_password(self):
        password = getpass.getpass("Password for the demo account: ")
        if len(password) < 10:
            raise CommandError("Use at least 10 characters.")
        if getpass.getpass("Again: ") != password:
            raise CommandError("The two entries differ.")
        return password

    def _wipe(self, user):
        """Everything the demo owns except the login itself and past research runs."""
        Snapshot.objects.filter(user=user).delete()
        user.accounts.all().delete()  # cascades holdings, ledger, liabilities
        Asset.objects.filter(owner=user).delete()  # the minted apartment
        WatchlistItem.objects.filter(user=user).delete()

    def _start_day(self):
        today = timezone.now().astimezone(TEHRAN).date()
        return self._trading_day(today - dt.timedelta(days=MONTHS * 30 + 10))

    @staticmethod
    def _trading_day(day):
        while day.weekday() not in TRADING_WEEKDAYS:
            day -= dt.timedelta(days=1)
        return day

    def _at(self, start, month, back=0):
        day = self._trading_day(start + dt.timedelta(days=month * 30) - dt.timedelta(days=back))
        today = timezone.now().astimezone(TEHRAN).date()
        day = min(day, self._trading_day(today - dt.timedelta(days=1)))
        return dt.datetime.combine(day, dt.time(11, 0), tzinfo=TEHRAN)

    def _accounts(self, user, start):
        accounts = {
            "family": Account.objects.create(user=user, name="Family"),
            "savings": Account.objects.create(user=user, name="Savings"),
        }
        # Snapshots are rebuilt only from an account's creation day onward.
        began = dt.datetime.combine(start, dt.time(9, 0), tzinfo=TEHRAN)
        Account.objects.filter(pk__in=[a.pk for a in accounts.values()]).update(created_at=began)
        return {key: Account.objects.get(pk=a.pk) for key, a in accounts.items()}

    # -- the story ---------------------------------------------------------

    def _asset(self, ref):
        source, symbol = ref
        try:
            if source == "key":
                return Asset.objects.get(key=symbol, is_active=True)
            return ensure_asset(source="tsetmc" if source == "tse" else "brs", symbol=symbol)
        except Exception as exc:  # not in this catalog: the story goes on without it
            self.notes.append(f"skipped {symbol}: {exc}")
            return None

    def _story_entry(self, account, month, start, kind, ref, amount):
        asset = self._asset(ref) if ref else None
        if ref and asset is None:
            return
        name = ref[1] if ref else kind
        # Back a week first (the closest real session before the story's date),
        # then forward: a stock halted on that day still enters on its next print.
        for back in [*range(0, 8), *range(-1, -26, -1)]:
            when = self._at(start, month, back)
            try:
                with transaction.atomic():  # a refused attempt must not poison the rest
                    self._book(account, asset, when, kind, amount, name)
                return
            except (LedgerError, PriceResolutionError) as exc:
                last = exc
        self.notes.append(f"skipped {kind} {name} (month {month}): {last}")

    def _book(self, account, asset, when, kind, amount, name):
        if kind in ("buy", "sell"):
            quantity = self._quantity(account, asset, when, kind, amount)
            if not quantity:
                raise LedgerError("nothing to trade")
            create_ledger_entry(account=account, kind=kind, asset=asset, quantity=quantity,
                                occurred_at=when, note="Showcase")
        else:
            create_ledger_entry(account=account, kind=kind, asset=asset, amount_tomans=amount,
                                occurred_at=when, note="Showcase")

    def _quantity(self, account, asset, when, kind, amount):
        if kind == "sell":
            holding = Holding.objects.filter(account=account, asset=asset).first()
            held = Decimal(holding.quantity) if holding else Decimal(0)
            step = Decimal("0.0001") if asset.asset_class == Asset.AssetClass.CRYPTO else Decimal("1")
            return (held * Decimal(str(amount))).quantize(step, rounding=ROUND_DOWN)
        price = resolve_historical_price(asset, when)  # raises -> LedgerError path below
        per_unit = holding_value_to_toman(asset, Decimal(price))
        if not per_unit:
            raise LedgerError(f"no price for {asset.key} on {when.date()}")
        step = Decimal("0.0001") if asset.asset_class == Asset.AssetClass.CRYPTO else Decimal("1")
        return (Decimal(amount) / Decimal(per_unit)).quantize(step, rounding=ROUND_DOWN)

    def _apartment(self, user, account, start):
        first_month, first_price = APARTMENT["marks"][0]
        response = self._call(user, "post", f"/api/accounts/{account.id}/holdings/", {
            "new_property_name": APARTMENT["name"],
            "quantity": str(first_price),
            "area_sqm": str(APARTMENT["area_sqm"]),
            "purchase_price_per_sqm_million": str(first_price),
            "occurred_at": self._at(start, first_month).isoformat(),
        }, label="apartment")
        if response is None:
            return None
        asset = Asset.objects.filter(owner=user, is_house=True).first()
        for month, price in APARTMENT["marks"][1:]:
            record_house_mark(user=user, account_id=account.id, asset=asset, quantity=Decimal(price),
                              area_sqm=Decimal(APARTMENT["area_sqm"]), occurred_at=self._at(start, month))
        return asset

    def _mortgage(self, user, account, house, start):
        m = MORTGAGE
        monthly_rate = Decimal(m["rate"]) / 1200
        factor = (1 + monthly_rate) ** m["term"]
        installment = (Decimal(m["principal"]) * monthly_rate * factor / (factor - 1)).quantize(Decimal("1"))
        self._call(user, "post", f"/api/accounts/{account.id}/liabilities/", {
            "label": m["label"], "kind": "secured_debt", "lender": m["lender"], "asset_key": house.key,
            "principal_tomans": m["principal"], "annual_rate_pct": m["rate"], "term_months": m["term"],
            "monthly_installment_tomans": str(installment), "started_on": start.isoformat(),
        }, label="mortgage")

    def _research(self, user):
        from research.models import ResearchRun

        if ResearchRun.objects.filter(user=user).exists():
            self.notes.append("research: kept the existing run (not billed again)")
            return
        ceiling = min(Decimal("0.05"), Decimal(str(settings.RESEARCH_MAX_RUN_USD)))
        self._call(user, "post", "/api/research/runs/", {**RESEARCH, "max_cost_usd": str(ceiling)}, label="research")

    def _snapshots(self, user, accounts, start):
        """The net-worth chart: one estimated row per day, from the ledger and that day's prices."""
        from portfolio.services.valuation import value_as_of
        from portfolio.tasks import _rebuild_snapshot_day

        written, day = 0, start
        yesterday = timezone.now().astimezone(TEHRAN).date() - dt.timedelta(days=1)
        accounts = [Account.objects.get(pk=a.pk) for a in accounts]
        while day <= yesterday:
            written += _rebuild_snapshot_day(user, accounts, day, value_as_of)
            day += dt.timedelta(days=1)
        return written

    # -- plumbing ----------------------------------------------------------

    def _call(self, user, method, path, data, *, label):
        """Run one of the app's own API views as this user: same validation a person gets."""
        request = getattr(APIRequestFactory(), method)(path, data, format="json")
        force_authenticate(request, user=user)
        match = resolve(path)
        with transaction.atomic():
            response = match.func(request, *match.args, **match.kwargs)
            if response.status_code >= 400:
                transaction.set_rollback(True)
        if response.status_code >= 400:
            self.notes.append(f"{label}: HTTP {response.status_code} {getattr(response, 'data', '')}")
            return None
        return response

    def _report(self, user, accounts, snapshots):
        from portfolio.services.valuation import value_as_of

        self.stdout.write(self.style.SUCCESS(f"Showcase ready -> {user.email}"))
        for key, account in accounts.items():
            value = value_as_of(user, account=account, as_of=timezone.now(), include_hidden=True)
            entries = LedgerEntry.objects.filter(account=account).count()
            holdings = Holding.objects.filter(account=account).count()
            self.stdout.write(f"  {account.name:8} {entries:3} entries · {holdings:2} holdings · "
                              f"{Decimal(str(value['total'])):,.0f} Toman"
                              + (f" · not valued: {', '.join(map(str, value['excluded']))}" if value["excluded"] else ""))
        self.stdout.write(f"  {snapshots} daily snapshots · {WatchlistItem.objects.filter(user=user).count()} watched")
        for note in self.notes:
            self.stdout.write(self.style.WARNING(f"  ! {note}"))
