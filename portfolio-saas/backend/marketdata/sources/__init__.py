"""Direct, unmetered market-data origins -- the migration off BrsApi.

BrsApi resells data the origins publish for free. Its paid TSETMC product is
metered at ~10,000/day; Market CGCC is limited to 1,500/day. The paid ceilings
is what makes the archive queue and its priority arithmetic necessary.

Every origin here was probed from the production VPS on 2026-08-31, and the
results split cleanly along one line -- who is allowed to connect:

| Origin  | Reachable | Replaces                      | Unit  | Depth measured           |
|---------|-----------|-------------------------------|-------|--------------------------|
| tgju    | yes       | `Market/Gold_Currency*.php`   | Rial  | dollar to 2011-11-26     |
| wallex  | yes       | `Market/Cryptocurrency.php`   | Toman | BTC to 2018-11-27, 1 req |
| nobitex | yes       | cross-check only              | Rial  | ~500-candle cap          |
| tsetmc  | **no**    | `Tsetmc/*.php`                | Rial  | unverified (blocked)     |

The first three are live and need nothing but this code. TSETMC drops the SYN
from any non-Iranian address, so it needs `IRAN_EGRESS_PROXY` before it can be
verified at all; see `tsetmc_direct` and `manage.py check_egress`.

Two rules hold across every module here, both learned expensively elsewhere in
this codebase:

**Units are declared, never inferred.** Wallex says `quoteAsset`, Nobitex
encodes it in the pair name, TSETMC quotes Rial by convention. TGJU declares
nothing, so `tgju.SLUG_UNITS` is a hand-verified map -- not a magnitude
heuristic, because Nobitex and Wallex quote the same coin exactly 10x apart and
a heuristic cannot tell that from a price move.

**A dead feed still returns a number.** TGJU's `usdt-irr` slug answers with
273,000 stamped 2020-11-11. Nothing in the payload flags it as retired. Every
live quote is therefore age-checked before it is believed.
"""
from . import http, nobitex, tgju, tsetmc_direct, wallex  # noqa: F401

__all__ = ["http", "tgju", "wallex", "nobitex", "tsetmc_direct", "SOURCES"]

#: Registry consumed by `check_egress` and the Ops console. `requires_egress`
#: is the interesting column: it is the difference between "this works now" and
#: "this works when the network does".
SOURCES = {
    "tgju": {
        "module": tgju,
        "label": "TGJU (gold / FX / commodity)",
        "requires_egress": False,
        "setting": "TGJU_ENABLED",
        "replaces": "BrsApi Market/Gold_Currency.php + Gold_Currency_Pro.php",
    },
    "wallex": {
        "module": wallex,
        "label": "Wallex (crypto, Toman)",
        "requires_egress": False,
        "setting": "WALLEX_ENABLED",
        "replaces": "BrsApi Market/Cryptocurrency.php",
    },
    "nobitex": {
        "module": nobitex,
        "label": "Nobitex (crypto cross-check, Rial)",
        "requires_egress": False,
        "setting": "NOBITEX_ENABLED",
        "replaces": "cross-source validation (no BrsApi equivalent)",
    },
    "tsetmc": {
        "module": tsetmc_direct,
        "label": "TSETMC direct (stocks, order book, ticks)",
        "requires_egress": True,
        "setting": "TSETMC_DIRECT_ENABLED",
        "replaces": "BrsApi Tsetmc/*.php",
    },
}
