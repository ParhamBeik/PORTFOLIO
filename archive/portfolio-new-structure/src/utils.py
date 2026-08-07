"""Small shared helpers.

These functions keep file handling, logging, and timestamps consistent across
the pipeline without making each module repeat the same code.
"""

import json
import os
import logging
from datetime import datetime

from rich.console import Console
from rich.logging import RichHandler


# --- Terminal output ---------------------------------------------------------
def setup_rich_logging(name: str):
    """Set a nicer logger format so the terminal output is easier to read."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(rich_tracebacks=True, show_time=True, show_level=True)],
    )
    return logging.getLogger(name)


console = Console()


def log_step(message: str, status: str = "info"):
    """Print a single readable status line for every important action."""
    symbols = {
        "info": "ℹ",
        "success": "✅",
        "warning": "⚠️",
        "error": "❌",
        "skip": "⏭",
    }
    prefix = symbols.get(status, "•")
    console.print(f"[bold]{prefix}[/bold] {message}")


# --- JSON file helpers -------------------------------------------------------
def load_json(filepath):
    """Read JSON from a file and return a dictionary."""
    if not os.path.exists(filepath):
        return {}
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read().strip()
            if not content:
                return {}
            return json.loads(content)
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        logging.getLogger(__name__).warning(
            "Could not read JSON from %s (%s). Using empty data.", filepath, exc
        )
        return {}


def save_json(filepath, data):
    """Write data to a JSON file, creating folders when needed."""
    directory = os.path.dirname(filepath)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def append_jsonl(filepath, data):
    """Insert one JSONL record and keep newest timestamps first."""
    directory = os.path.dirname(filepath)
    if directory:
        os.makedirs(directory, exist_ok=True)
    records = []
    if os.path.exists(filepath):
        with open(filepath, "r", encoding="utf-8") as f:
            for line in f:
                text = line.strip()
                if not text:
                    continue
                try:
                    records.append(json.loads(text))
                except json.JSONDecodeError:
                    logging.getLogger(__name__).warning(
                        "Skipping invalid JSONL line while updating %s.", filepath
                    )
    records.append(data)
    records = sort_records_newest_first(records)
    write_jsonl(filepath, records)


def _timestamp_sort_key(record):
    timestamp = record.get("timestamp") if isinstance(record, dict) else None
    if not timestamp:
        return datetime.min
    try:
        return datetime.fromisoformat(str(timestamp))
    except ValueError:
        return datetime.min


def sort_records_newest_first(records):
    """Return JSONL records sorted by timestamp descending."""
    return sorted(records, key=_timestamp_sort_key, reverse=True)


def write_jsonl(filepath, records):
    """Write records to JSONL, one compact JSON object per line."""
    directory = os.path.dirname(filepath)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(filepath, "w", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


# --- Time --------------------------------------------------------------------
def get_current_timestamp():
    """Return the current timestamp in ISO format."""
    return datetime.now().isoformat()
