"""Service regression tests."""

import atexit
import json
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory


def make_private_file(path, mode=0o600):
    if sys.platform != "win32":
        path.chmod(mode)
        return

    import ntsecuritycon
    import win32api
    import win32con
    import win32security

    # An elevated Windows runner can assign Administrators as the default owner.
    token = win32security.OpenProcessToken(
        win32api.GetCurrentProcess(), win32con.TOKEN_QUERY
    )
    try:
        owner = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
    finally:
        token.Close()

    acl = win32security.ACL()
    acl.AddAccessAllowedAce(
        win32security.ACL_REVISION, ntsecuritycon.FILE_ALL_ACCESS, owner
    )
    win32security.SetNamedSecurityInfo(
        str(path),
        win32security.SE_FILE_OBJECT,
        win32security.OWNER_SECURITY_INFORMATION
        | win32security.DACL_SECURITY_INFORMATION
        | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
        owner,
        None,
        acl,
        None,
    )


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
make_private_file(config)
os.environ["CHAT_AGENT_GATEWAY_CONFIG"] = str(config)
