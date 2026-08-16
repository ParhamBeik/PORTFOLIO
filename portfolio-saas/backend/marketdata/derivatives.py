"""Small, explicit rules for building a continuous derivatives series."""
from collections import defaultdict


ROLL_DAYS = 5


def select_roll_series(rows, *, roll_days=ROLL_DAYS):
    """Keep the most-liquid eligible contract, rolling before expiry."""
    by_day = defaultdict(list)
    for row in rows:
        if row["days_to_expiry"] > roll_days:
            by_day[row["day"]].append(row)
    result = []
    previous = None
    for day in sorted(by_day):
        chosen = max(by_day[day], key=lambda row: (row.get("volume", 0), row["contract_code"]))
        result.append({**chosen, "rolled": previous is not None and previous != chosen["contract_code"]})
        previous = chosen["contract_code"]
    return result
