"""The live price loop: fetch (fetcher) -> normalise (extractor) -> cache.

`find_symbol_record` sits here rather than in either sibling because both
read the same TSETMC payload shape, and rather than in `marketdata` because
it is a pure function over a payload -- importing it from a warehouse module
dragged `marketdata.ingest` and the whole fetcher stack into the request-time
price path for the sake of one dictionary lookup.
"""


def find_symbol_record(records, symbol, aliases=()):
    """Return an exact l18/l30 match; never guess with substring matching."""
    if not isinstance(records, list):
        return None
    names = {
        str(value).strip().casefold()
        for value in (symbol, *aliases)
        if str(value).strip()
    }
    for record in records:
        if not isinstance(record, dict):
            continue
        if names & {
            str(record.get("l18", "")).strip().casefold(),
            str(record.get("l30", "")).strip().casefold(),
        }:
            return record
    return None
