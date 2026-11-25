import json
import tempfile
import sys
import unittest
from pathlib import Path

from chat_agent_gateway.config import Settings, read_settings


class ConfigTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "config.local.json"

    def write(self, value, mode=0o600):
        self.path.write_text(json.dumps(value))
        self.path.chmod(mode)

    def test_missing_config_does_not_select_any_external_service_or_workspace(self):
        self.assertEqual(read_settings(self.path), Settings())

    def test_private_config_reads_settings_and_resolves_relative_workspace(self):
        self.write(
            {
                "openproject": {
                    "origin": "https://openproject.example.com/",
                    "user_id": 1001,
                    "api_key": "fixture-api-key",
                },
                "mattermost": {
                    "origin": "https://chat.example.com",
                    "email": "Operator@Example.com",
                },
                "workspace_root": "projects",
                "workspace_branches": {"sample-app": "review"},
            }
        )
        settings = read_settings(self.path)
        self.assertEqual(settings.openproject_origin, "https://openproject.example.com")
        self.assertEqual(settings.openproject_user_id, 1001)
        self.assertEqual(settings.mattermost_email, "operator@example.com")
        self.assertEqual(
            settings.workspace_root, (self.path.parent / "projects").resolve()
        )
        self.assertEqual(settings.workspace_branches, {"sample-app": "review"})
        self.assertNotIn("fixture-api-key", repr(settings))

    def test_rejects_group_readable_config_and_urls_that_carry_credentials(self):
        if sys.platform != "win32":
            self.write({}, mode=0o640)
            with self.assertRaises(PermissionError):
                read_settings(self.path)
        self.write(
            {"openproject": {"origin": "https://user:example@openproject.example.com"}}
        )
        with self.assertRaisesRegex(ValueError, "HTTPS") as caught:
            read_settings(self.path)
        self.assertNotIn("user:example", str(caught.exception))

    @unittest.skipUnless(sys.platform == "win32", "NTFS ACL test")
    def test_windows_rejects_an_acl_that_allows_everyone_to_read_settings(self):
        import win32con
        import win32security

        self.write({})
        filename = str(self.path)
        object_type = win32security.SE_FILE_OBJECT
        acl_information = win32security.DACL_SECURITY_INFORMATION
        descriptor = win32security.GetNamedSecurityInfo(
            filename, object_type, acl_information
        )
        dacl = descriptor.GetSecurityDescriptorDacl()
        dacl.AddAccessAllowedAce(
            win32security.ACL_REVISION,
            win32con.GENERIC_READ,
            win32security.ConvertStringSidToSid("S-1-1-0"),
        )
        win32security.SetNamedSecurityInfo(
            filename, object_type, acl_information, None, None, dacl, None
        )
        with self.assertRaises(PermissionError):
            read_settings(self.path)
