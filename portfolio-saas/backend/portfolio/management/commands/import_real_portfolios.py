"""One-time correct load of the real Father/Mother portfolios.

This replaces the fabricated `seed_samples` random-walk data. It reads the
canonical files produced by the tracker engine:

    PORTFOLIO NEW STRUCTURE/data/current_state.json      -> exact holdings
    PORTFOLIO NEW STRUCTURE/data/history_snapshots.jsonl -> real daily history

and loads them under the sample family login (PRO). After this load the app
operates only on data entered through the website; this command just corrects
the seeded starting state.

One-time: once the sample family has accounts, re-running leaves all browser
changes untouched. No Transaction rows are created (the real files record none).

    manage.py import_real_portfolios
    manage.py import_real_portfolios --data-dir /path/to/data
"""
import json
from datetime import datetime, timezone as dt_timezone
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction

from accounts.models import User
from portfolio.models import Account, Asset, Holding, Price, Snapshot, Transaction

SAMPLE_EMAIL = "family@portfolio.local"
SAMPLE_PASSWORD = "family12345"
REAL_SOURCE = "REAL"

# current_state.json stores the house under "house_price_per_sqm_million" but it
# is valued via the "house_asset" catalog entry, whose Holding.quantity IS the
# price-per-sqm-million the valuation formula consumes.
HOUSE_STATE_KEY = "house_price_per_sqm_million"
HOUSE_ASSET_KEY = "house_asset"

def _default_data_dir() -> Path:
    """Best-effort default: the tracker data dir two levels above the backend.

    On the host, BASE_DIR is ``.../portfolio-saas/backend`` so two levels up is
    the repo root that holds ``PORTFOLIO NEW STRUCTURE``. In a container BASE_DIR
    is ``/app`` (no such parent), so fall back to a path under BASE_DIR instead
    of indexing past the filesystem root -- callers there pass ``--data-dir``.
    """
    base = Path(settings.BASE_DIR).resolve()
    root = base.parents[1] if len(base.parents) >= 2 else base
    return root / "PORTFOLIO NEW STRUCTURE" / "data"


class Command(BaseCommand):
    help = "Load the real Father/Mother portfolios from the tracker data files."

    def add_arguments(self, parser):
        parser.add_argument(
            "--data-dir",
            default=str(_default_data_dir()),
            help="Directory holding current_state.json and history_snapshots.jsonl.",
        )

    def handle(self, *args, **options):
        if not settings.DEBUG:
            self.stdout.write("Skipping real-portfolio import (DEBUG=False).")
            return

        data_dir = Path(options["data_dir"])
        state_path = data_dir / "current_state.json"
        history_path = data_dir / "history_snapshots.jsonl"
        if not state_path.exists() or not history_path.exists():
            self.stdout.write(self.style.WARNING(
                f"Data files not found under {data_dir}; skipping."
            ))
            return

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
        user, created = User.objects.get_or_create(
            email=SAMPLE_EMAIL,
            defaults={"first_name": "Sample", "last_name": "Family",
                      "tier": User.Tier.PRO},
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
        from portfolio.services.trades import execute_trade

        accounts = {}
        for name, holdings in state.items():
            account, _ = Account.objects.get_or_create(user=user, name=name)
            accounts[name] = account
            for raw_key, qty in holdings.items():
                key = HOUSE_ASSET_KEY if raw_key == HOUSE_STATE_KEY else raw_key
                asset = assets.get(key)
                if asset is None:
                    continue
                if asset.is_house:
                    Holding.objects.create(
                        account=account, asset=asset, quantity=Decimal(str(qty))
                    )
                else:
                    execute_trade(
                        account=account,
                        asset=asset,
                        side=Transaction.Side.BUY,
                        quantity=Decimal(str(qty)),
                        price_tomans=Decimal("0"),
                        skip_snapshots=True,
                    )
        return accounts

    def _load_history(self, user, accounts, history, assets) -> None:
        """Backdated Price + per-account + user-level Snapshot rows per day."""
        price_pairs = []  # (pk, ts)
        snap_pairs = []   # (pk, ts)
        for _day, entry in sorted(history.items()):
            ts = entry["ts"]
            snap = entry["snapshot"]

            rows = [
                Price(asset=assets[key], price=Decimal(str(val)), source=REAL_SOURCE)
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
