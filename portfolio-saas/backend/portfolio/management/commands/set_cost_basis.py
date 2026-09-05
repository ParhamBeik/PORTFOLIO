"""Declare what a position already on the books actually cost.

An `opening_position` says "I already own this" and, until `cost_basis_tomans`
existed, said nothing about what was paid -- so a portfolio assembled by
declaring existing holdings could only ever show a current value, never a gain.
This backfills that declaration for holdings that predate the field.

It writes one column on one ledger row per holding. It does not create entries,
does not move quantities, and does not touch prices, so the worst case of a
wrong figure is a wrong P&L that the next run corrects. `--dry-run` prints the
same table without saving, and re-running with the same values is a no-op.

Matching, in the order tried:
  --key      an exact catalog key ("gold_18k_gram", "emami_coin", "usd_cash")
  --name     a case-insensitive substring of the property's name or the
             holder's nickname for it. Properties are minted with random keys
             (`re-<uuid>`), so a name is the only handle a human has.

Units follow the ledger's own convention exactly, because this writes the same
column the API does: the asset's quote unit -- Rial for a TSE share, Toman for
everything else -- and MILLIONS of Toman per square meter for a property, the
unit `Holding.quantity` already uses for real estate.
"""
from decimal import Decimal, InvalidOperation

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from portfolio.models import HOUSE_PRICE_SCALE, Holding, LedgerEntry
from portfolio.services.ledger import COST_BASIS_KINDS, HOUSE_MARK_KINDS


def _decimal(raw, label):
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, TypeError):
        raise CommandError(f"{label}: not a number ({raw!r})")
    if value <= 0:
        raise CommandError(f"{label}: must be positive ({raw!r})")
    return value


class Command(BaseCommand):
    help = "Set the declared purchase price on holdings recorded as openings."

    def add_arguments(self, parser):
        parser.add_argument("--email", help="Owner of the portfolios to touch.")
        parser.add_argument(
            "--account-id",
            type=int,
            help="Restrict to one portfolio. Omit to apply across the user's portfolios.",
        )
        parser.add_argument(
            "--list",
            action="store_true",
            help="Print every holding and whether it already declares a basis.",
        )
        parser.add_argument(
            "--key",
            action="append",
            default=[],
            metavar="ASSET_KEY=PRICE",
            help="Set the basis for a catalog asset, e.g. gold_18k_gram=10000000.",
        )
        parser.add_argument(
            "--name",
            action="append",
            default=[],
            metavar="SUBSTRING=PRICE",
            help="Set the basis for a property matched by name, e.g. Tehran=24.",
        )
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        holdings = Holding.objects.select_related(
            "account", "asset", "account__user"
        ).order_by("account_id", "asset__name")
        if options["email"]:
            holdings = holdings.filter(account__user__email=options["email"])
        if options["account_id"]:
            holdings = holdings.filter(account_id=options["account_id"])
        holdings = list(holdings)
        if not holdings:
            raise CommandError("No holdings matched. Check --email / --account-id.")

        if options["list"]:
            return self._list(holdings)

        targets = self._parse_targets(options)
        if not targets:
            raise CommandError("Nothing to do. Pass --key / --name, or --list.")

        planned, unmatched = self._plan(holdings, targets)
        for line in unmatched:
            self.stderr.write(self.style.WARNING(line))
        if not planned:
            self.stdout.write("Nothing to change.")
            return

        for row in planned:
            self.stdout.write(row["summary"])
        if options["dry_run"]:
            self.stdout.write(self.style.WARNING("--dry-run: nothing saved."))
            return

        with transaction.atomic():
            for row in planned:
                entry = row["entry"]
                entry.cost_basis_tomans = row["value"]
                # `updated_at` is `auto_now`, and it is what the position-metrics
                # cache keys on -- an in-place edit moves no id, so without the
                # save touching it the P&L table answers from cache for an hour.
                entry.save(update_fields=["cost_basis_tomans", "updated_at"])
        self.stdout.write(self.style.SUCCESS(f"Updated {len(planned)} holding(s)."))

    # ------------------------------------------------------------------

    def _parse_targets(self, options):
        """[(spec, price)], where spec is ("key"|"name", value)."""
        targets = []
        for raw in options["key"]:
            key, _, price = raw.partition("=")
            if not key or not price:
                raise CommandError(f"--key expects KEY=PRICE, got {raw!r}")
            targets.append((("key", key.strip()), _decimal(price, raw)))
        for raw in options["name"]:
            name, _, price = raw.partition("=")
            if not name or not price:
                raise CommandError(f"--name expects SUBSTRING=PRICE, got {raw!r}")
            targets.append((("name", name.strip()), _decimal(price, raw)))
        return targets

    def _matches(self, holding, spec):
        kind, needle = spec
        if kind == "key":
            return holding.asset.key == needle
        text = f"{holding.asset.name} {holding.display_name}".casefold()
        return needle.casefold() in text

    def _plan(self, holdings, targets):
        """What each target would do, and why any of them would do nothing."""
        planned, unmatched = [], []
        for spec, value in targets:
            hits = [h for h in holdings if self._matches(h, spec)]
            if not hits:
                unmatched.append(f"no holding matched {spec[0]}={spec[1]!r}")
                continue
            if len(hits) > 1 and spec[0] == "name":
                # Refused rather than guessed. Two properties can legitimately
                # share a name, and writing a purchase price onto the wrong one
                # is silent -- both rows still look plausible afterwards.
                names = ", ".join(f"holding={h.id} {h.label!r}" for h in hits)
                unmatched.append(
                    f"{spec[1]!r} matched {len(hits)} holdings ({names}); "
                    "narrow the substring or use --key"
                )
                continue
            for holding in hits:
                entry = self._entry_for(holding)
                if entry is None:
                    unmatched.append(
                        f"holding={holding.id} {holding.label!r}: no opening or "
                        "valuation mark to carry a purchase price. A holding "
                        "built from buys already has a cost basis from them."
                    )
                    continue
                if entry.cost_basis_tomans == value:
                    self.stdout.write(
                        f"holding={holding.id} {holding.label!r}: already {value}"
                    )
                    continue
                planned.append({
                    "entry": entry,
                    "value": value,
                    "summary": (
                        f"holding={holding.id} account={holding.account_id} "
                        f"{holding.label!r} entry={entry.id} ({entry.kind}): "
                        f"{entry.cost_basis_tomans} -> {value} "
                        f"{self._unit(holding)}"
                    ),
                })
        return planned, unmatched

    def _entry_for(self, holding):
        """The row that should carry the declaration.

        For a property, the LATEST live mark: marks replace, and
        `performance._house_position` reads the most recent one that declared a
        basis, so writing to the newest makes a correction win over anything
        older. For everything else, the opening -- there is only ever one that
        matters, and a buy states its own price.
        """
        kinds = HOUSE_MARK_KINDS if holding.asset.is_house else {
            LedgerEntry.Kind.OPENING_POSITION
        }
        return (
            LedgerEntry.objects.filter(
                account=holding.account,
                asset=holding.asset,
                kind__in=kinds & COST_BASIS_KINDS,
                reversal_of__isnull=True,
                reversed_by__isnull=True,
            )
            .order_by("-timestamp", "-pk")
            .first()
        )

    def _unit(self, holding):
        if holding.asset.is_house:
            return "million Toman / m²"
        return "Rial per unit" if holding.asset.tse_symbol else "Toman per unit"

    def _list(self, holdings):
        for holding in holdings:
            entry = self._entry_for(holding)
            if entry is None:
                state = "no opening (basis comes from its buys)"
            elif entry.cost_basis_tomans is None:
                state = "NO BASIS DECLARED"
            else:
                declared = Decimal(entry.cost_basis_tomans)
                total = (
                    declared * HOUSE_PRICE_SCALE * Decimal(holding.area_sqm)
                    if holding.asset.is_house
                    else declared * Decimal(holding.quantity)
                )
                state = f"basis {declared} {self._unit(holding)} (total ~{total:,.0f})"
            self.stdout.write(
                f"holding={holding.id} account={holding.account_id} "
                f"user={holding.account.user.email} key={holding.asset.key} "
                f"name={holding.label!r} qty={holding.quantity} -> {state}"
            )
