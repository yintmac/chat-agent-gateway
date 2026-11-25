"""Paths shared by the service, its repository router and launchd."""

from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
STATE_DIR = Path.home() / "Library/Application Support/Chat Agent Gateway"
