"""One-time correct load of the real Father/Mother portfolios.

Reads an explicit --data-dir containing current_state.json and
history_snapshots.jsonl. Does not create or assume repo-side drop folders.

    manage.py import_real_portfolios --data-dir /path/to/data
"""
import json
from datetime import datetime, timezone as dt_timezone
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from accounts.models import User
from portfolio.models import Account, Asset, Holding, Price, Snapshot, Transaction

SAMPLE_EMAIL = "admin@portfolio.local"
SAMPLE_PASSWORD = "admin12345"
REAL_SOURCE = "REAL"

# current_state.json stores the house under "house_price_per_sqm_million" but it
# is valued via the "house_asset" catalog entry, whose Holding.quantity IS the
# price-per-sqm-million the valuation formula consumes.
HOUSE_STATE_KEY = "house_price_per_sqm_million"
HOUSE_ASSET_KEY = "house_asset"


class Command(BaseCommand):
    help = "Load the real Father/Mother portfolios from the tracker data files."

    def add_arguments(self, parser):
        parser.add_argument(
            "--data-dir",
            required=True,
            help="Existing directory with current_state.json and history_snapshots.jsonl.",
        )

    def handle(self, *args, **options):
        if not settings.DEBUG:
            self.stdout.write("Skipping real-portfolio import (DEBUG=False).")
            return

        data_dir = Path(options["data_dir"])
        if not data_dir.is_dir():
            raise CommandError(f"--data-dir does not exist: {data_dir}")
        state_path = data_dir / "current_state.json"
        history_path = data_dir / "history_snapshots.jsonl"
        if not state_path.exists() or not history_path.exists():
            raise CommandError(
                f"Need current_state.json and history_snapshots.jsonl under {data_dir}"
            )

        state = json.loads(state_path.read_text())
        history = self._latest_per_day(history_path)
        assets = {a.key: a for a in Asset.objects.filter(is_active=True)}

        with transaction.atomic():
            user = self._get_or_create_user()
            if user.accounts.exists():
                self.stdout.write("Real portfolios already loaded; skipping.")
                return
            self._reset_user_data(user)
            accounts = self._load_holdings(user, state, assets)
            self._load_history(user, accounts, history, assets)

        self.stdout.write(self.style.SUCCESS(
            f"Real portfolios loaded -> {SAMPLE_EMAIL} "
            f"({len(accounts)} accounts, {len(history)} days of history)."
        ))

    # --- steps -------------------------------------------------------------

    def _get_or_create_user(self) -> User:
        from django.utils import timezone as tz
        user, created = User.objects.get_or_create(
            email=SAMPLE_EMAIL,
            defaults={"first_name": "Admin", "last_name": "User",
                      "is_staff": True,
                      "is_superuser": True,
                            "is_active": True},
        )
        if created:
            user.set_password(SAMPLE_PASSWORD)
            user.save()
        return user

    def _reset_user_data(self, user: User) -> None:
        """Clear derived state so a re-run corrects rather than duplicates."""
        Transaction.objects.filter(account__user=user).delete()
        Holding.objects.filter(account__user=user).delete()
        Snapshot.objects.filter(user=user).delete()
        # Real prices are global but sourced here; only clear our own rows.
        Price.objects.filter(source=REAL_SOURCE).delete()

    def _load_holdings(self, user, state, assets) -> dict:
        """Create one Account per portfolio with its holdings.

        Returns {portfolio_name: Account}.
        """
        from django.utils import timezone
        from portfolio.services.ledger import create_ledger_entry
        from portfolio.models import LedgerEntry

        accounts = {}
        for name, holdings in state.items():
            account, _ = Account.objects.get_or_create(user=user, name=name)
            accounts[name] = account
            opened_at = timezone.now()
            for raw_key, qty in holdings.items():
                key = HOUSE_ASSET_KEY if raw_key == HOUSE_STATE_KEY else raw_key
                asset = assets.get(key)
                if asset is None:
                    continue
                dec_qty = Decimal(str(qty))
                if dec_qty <= 0:
                    continue
                create_ledger_entry(
                    account=account,
                    kind=LedgerEntry.Kind.OPENING_POSITION,
                    asset=asset,
                    quantity=dec_qty,
                    occurred_at=opened_at,
                    source="system",
                    note="Imported opening position",
                )
        return accounts

    def _load_history(self, user, accounts, history, assets) -> None:
        """Backdated Price + per-account + user-level Snapshot rows per day."""
        price_pairs = []  # (pk, ts)
        snap_pairs = []   # (pk, ts)
        for _day, entry in sorted(history.items()):
            ts = entry["ts"]
            snap = entry["snapshot"]

            # Source snapshot JSON is Toman-denominated (`total_values_tomans`),
            # matching Price's storage unit -- no conversion needed.
            rows = [
                Price(
                    asset=assets[key],
                    price=Decimal(str(val)),
                    source=REAL_SOURCE,
                    price_unit=Price.Unit.IRT,
                    price_unit_verified=True,
                )
                for key, val in snap.get("prices", {}).items()
                if key in assets
            ]
            created = Price.objects.bulk_create(rows)
            price_pairs += [(p.pk, ts) for p in created]

            totals = snap.get("total_values_tomans", {})
            user_total = Decimal("0")
            for name, account in accounts.items():
                acct_total = Decimal(str(totals.get(name, 0)))
                user_total += acct_total
                row = Snapshot.objects.create(
                    user=user, account=account, total_value_tomans=acct_total)
                snap_pairs.append((row.pk, ts))
            row = Snapshot.objects.create(
                user=user, account=None, total_value_tomans=user_total)
            snap_pairs.append((row.pk, ts))

        # auto_now_add stamped "now"; rewrite to the real dates.
        for pk, ts in price_pairs:
            Price.objects.filter(pk=pk).update(fetched_at=ts)
        for pk, ts in snap_pairs:
            Snapshot.objects.filter(pk=pk).update(timestamp=ts)

    # --- helpers -----------------------------------------------------------

    def _latest_per_day(self, history_path: Path) -> dict:
        """Collapse the jsonl to the latest snapshot per calendar day.

        Some days have several intraday snapshots; the last one of the day is the
        most current, keeping the chart at the source's true daily resolution.
        """
        by_day = {}
        with history_path.open() as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                ts = self._parse_ts(rec["timestamp"])
                day = ts.date().isoformat()
                prev = by_day.get(day)
                if prev is None or ts > prev["ts"]:
                    by_day[day] = {"ts": ts, "snapshot": rec["snapshot"]}
        return by_day

    @staticmethod
    def _parse_ts(raw: str) -> datetime:
        """Parse the tracker's naive ISO timestamp as UTC (matches USE_TZ)."""
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=dt_timezone.utc)
        return dt
