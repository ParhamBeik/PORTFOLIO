"""Read account counters from the VPS without printing credentials or raw bodies.

Run via `docker exec -i portfolio-saas-backend-1 python -` with this file on
standard input. This is a manual diagnostic, not an application task.
"""
import json
import os

import requests

key = os.environ.get("TSETMC_API_KEY")
if not key:
    raise SystemExit("Provider key unavailable in container")
params = {"Key": key}
phone = os.environ.get("BRSAPI_ACCOUNT_PHONE")
if phone:
    params["Phone"] = phone
try:
    response = requests.get(
        "https://api.brsapi.ir/Panel/z_user_overview.php",
        params=params,
        timeout=15,
    )
    body = response.json()
except (requests.RequestException, ValueError):
    raise SystemExit("Panel read failed; credentials and URL suppressed") from None
metrics = body.get("metrics", []) if isinstance(body, dict) else []
print(json.dumps({
    "status": response.status_code,
    "successful": body.get("successful") if isinstance(body, dict) else None,
    "meters": [
        {"name": row.get("Name_En"), "usage": row.get("Usage_To_Limit")}
        for row in metrics if isinstance(row, dict)
    ],
}))
