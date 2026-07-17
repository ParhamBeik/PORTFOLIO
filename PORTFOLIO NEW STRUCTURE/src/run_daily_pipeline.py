"""Readable entrypoint for the daily portfolio update.

This file is only a safe alias. The real pipeline stays in `main.py` so old
commands and imports keep working.
"""

from main import run_pipeline


if __name__ == "__main__":
    run_pipeline()
