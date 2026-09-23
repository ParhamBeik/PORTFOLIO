"""One controlled provider request from the VPS; run via stdin, never print the key.

Example: docker exec -i portfolio-saas-backend-1 python - Tsetmc/Index.php type=1
with this file redirected to standard input. Observe panel counters separately.
"""
import json
import os
import sys

import requests

path = sys.argv[1]
parameters = dict(argument.split("=", 1) for argument in sys.argv[2:])
allowed_paths = {
    "Tsetmc/Index.php", "Tsetmc/Option.php", "Tsetmc/Symbol.php",
    "Tsetmc/AllSymbols.php", "Tsetmc/History.php", "Tsetmc/Candlestick.php",
    "Tsetmc/Transaction.php", "Tsetmc/Shareholder.php",
    "Codal/Announcement.php", "Market/Gold_Currency.php",
    "Market/Gold_Currency_Pro.php", "Market/Cryptocurrency.php",
    "Market/Commodity.php",
}
if path not in allowed_paths:
    raise SystemExit("Refusing unknown provider path")
key = os.environ.get("TSETMC_API_KEY")
if not key:
    raise SystemExit("Provider key unavailable in container")
try:
    response = requests.get(
        f"https://Api.BrsApi.ir/{path}",
        params={**parameters, "key": key},
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/json, text/plain, */*",
        },
        timeout=20,
    )
except requests.RequestException:
    raise SystemExit("Provider request failed; key and URL suppressed") from None
print(json.dumps({"status": response.status_code, "bytes": len(response.content)}))
