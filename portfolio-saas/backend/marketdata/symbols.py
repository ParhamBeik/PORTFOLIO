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
