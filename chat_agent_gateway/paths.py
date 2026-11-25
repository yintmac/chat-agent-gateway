"""Paths shared by the service, its repository router and launchd."""

import os
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
if sys.platform == "win32":
    STATE_DIR = (
        Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData/Local")
        / "Chat Agent Gateway"
    )
else:
    STATE_DIR = Path.home() / "Library/Application Support/Chat Agent Gateway"
