"""Use operating-system credential storage for Mattermost sessions."""

import subprocess
import sys


def read_secret(service, account):
    if sys.platform == "win32":
        import pywintypes
        import win32cred

        try:
            item = win32cred.CredRead(
                f"{service}:{account}", win32cred.CRED_TYPE_GENERIC
            )
            token = item["CredentialBlob"]
            return token.decode("utf-8") if isinstance(token, bytes) else token
        except (pywintypes.error, UnicodeError) as error:
            raise OSError("无法从 Windows 凭据管理器读取 Mattermost 会话。") from error

    if sys.platform != "darwin":
        raise OSError("当前平台未配置 Mattermost 凭据存储。")
    command = ["/usr/bin/security", "find-generic-password", "-s", service]
    if account:
        command += ["-a", account]
    result = subprocess.run(
        [*command, "-w"], capture_output=True, text=True, timeout=15
    )
    if result.returncode:
        raise OSError("无法从 macOS 钥匙串读取 Mattermost 会话。")
    return result.stdout.strip()


def write_secret(service, account, value):
    if sys.platform == "win32":
        import pywintypes
        import win32cred

        try:
            win32cred.CredWrite(
                {
                    "Type": win32cred.CRED_TYPE_GENERIC,
                    "TargetName": f"{service}:{account}",
                    "UserName": account,
                    "CredentialBlob": value.encode("utf-8"),
                    "Persist": win32cred.CRED_PERSIST_LOCAL_MACHINE,
                },
                0,
            )
        except pywintypes.error as error:
            raise OSError("Mattermost 会话写入 Windows 凭据管理器失败。") from error
        return

    if sys.platform != "darwin":
        raise OSError("当前平台未配置 Mattermost 凭据存储。")
    # The account and token are validated by the Mattermost adapter.
    result = subprocess.run(
        ["/usr/bin/security", "-i"],
        input=f'add-generic-password -U -s {service} -a {account} -w "{value}"\n',
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode:
        raise OSError("Mattermost 会话写入 macOS 钥匙串失败。")
