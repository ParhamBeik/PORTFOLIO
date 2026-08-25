"""Which holdings the user has switched off, in one place.

A hidden holding is still owned and still listed -- it is simply excluded from
every figure the app computes. That exclusion has to be identical in the live
valuation, the historical net-worth line, the performance metrics and the risk
breakdown, so all of them resolve the set through here rather than each writing
their own `filter(is_hidden=True)`.

Two shapes, because callers work in two currencies of identity: valuation and
returns key everything by `Asset.key`, while the ORM-level filters need ids.
"""
from ..models import Holding


def hidden_asset_ids(accounts) -> set[int]:
    """Asset ids switched off in any of `accounts`."""
    if not accounts:
        return set()
    return set(
        Holding.objects.filter(account__in=accounts, is_hidden=True).values_list(
            "asset_id", flat=True
        )
    )


def hidden_keys(user, account=None) -> frozenset[str]:
    """Asset keys switched off, for one portfolio or across all of the user's.

    Hiding is per holding, so the same asset can be counted in one portfolio and
    ignored in another. In the aggregate view (`account=None`) a key is hidden
    only where the holding that carries it is hidden -- callers that aggregate
    across accounts must therefore scope per account, not with this union.
    """
    qs = Holding.objects.filter(account__user=user, is_hidden=True)
    if account is not None:
        qs = qs.filter(account=account)
    return frozenset(qs.values_list("asset__key", flat=True))
