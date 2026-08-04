"""Dashboard export layer.

The daily pipeline calls this module after it builds a snapshot. The `.xlsx`
export (`export_daily_xlsx`) is the current dashboard, regenerated from history
so charts stay stable day to day.
"""

import os
import datetime

from utils import log_step

# --- Output paths and sheet names -------------------------------------------
DEFAULT_XLSX_TEMPLATE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "template",
    "Portfolio_Tracker_v2.xlsx",
)
DEFAULT_EXPORT_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "exports"
)


def export_daily_xlsx(
    snapshot,
    template_path=DEFAULT_XLSX_TEMPLATE,
    output_dir=DEFAULT_EXPORT_DIR,
    history_path=None,
):
    """Save the current v2 `.xlsx` dashboard for today's snapshot.

    The v2 workbook is regenerated from history_snapshots.jsonl so History_Data
    stays normalized to one daily-average row per date. If the template path
    does not exist, a fresh template is created before today's export is saved.
    """
    os.makedirs(output_dir, exist_ok=True)

    today_str = datetime.date.today().strftime("%Y-%m-%d")
    output_path = os.path.join(output_dir, f"Portfolio_{today_str}.xlsx")

    try:
        from build_template import build_workbook

        if template_path and not os.path.exists(template_path):
            build_workbook(template_path, history_path=history_path, snapshot=snapshot)
            log_step(f"V2 .xlsx template created: {template_path}", "success")

        path = build_workbook(
            output_path,
            history_path=history_path,
            snapshot=snapshot,
        )
        log_step(f"V2 .xlsx dashboard saved for today: {path}", "success")
        return path
    except Exception as exc:
        log_step(f"Could not save V2 .xlsx dashboard: {exc}", "error")
        return None
