"""Service regression tests."""

import atexit
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory

TEST_SETTINGS = TemporaryDirectory()
atexit.register(TEST_SETTINGS.cleanup)
config = Path(TEST_SETTINGS.name) / "config.local.json"
config.write_text(
    json.dumps(
        {
            "openproject": {
                "origin": "https://openproject.example.com",
                "user_id": 1001,
                "api_key": "fixture-api-key",
            },
            "mattermost": {
                "origin": "https://mattermost.example.com",
                "email": "operator@example.com",
            },
        }
    )
)
config.chmod(0o600)
os.environ["CHAT_AGENT_GATEWAY_CONFIG"] = str(config)
