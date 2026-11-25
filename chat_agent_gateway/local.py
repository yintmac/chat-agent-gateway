"""Platform primitives for local state and single-instance workers."""

import os
import sys
from pathlib import Path

import portalocker


def acquire_lock(file, *, blocking=False):
    flags = portalocker.LockFlags.EXCLUSIVE
    if not blocking:
        flags |= portalocker.LockFlags.NON_BLOCKING
    try:
        portalocker.lock(file, flags)
    except portalocker.exceptions.AlreadyLocked as error:
        raise BlockingIOError("网关锁已被其他进程持有。") from error


def require_private(path: Path, file=None, *, exact_mode=None):
    if sys.platform == "win32":
        require_private_windows(path)
        return
    info = os.fstat(file.fileno()) if file is not None else path.stat()
    if (
        info.st_uid != os.getuid()
        or info.st_mode & 0o077
        or exact_mode is not None
        and info.st_mode & 0o777 != exact_mode
    ):
        raise PermissionError("本机配置须由当前用户持有，且其他用户无权访问。")


def require_private_windows(path: Path):
    import pywintypes
    import win32api
    import win32con
    import win32security

    try:
        token = win32security.OpenProcessToken(
            win32api.GetCurrentProcess(), win32con.TOKEN_QUERY
        )
        try:
            user = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
        finally:
            token.Close()
        user_sid = win32security.ConvertSidToStringSid(user)
        descriptor = win32security.GetNamedSecurityInfo(
            str(path),
            win32security.SE_FILE_OBJECT,
            win32security.OWNER_SECURITY_INFORMATION
            | win32security.DACL_SECURITY_INFORMATION,
        )
        permitted = {
            user_sid,
            "S-1-5-18",  # Local System
            "S-1-5-32-544",  # Administrators
        }
        acl = descriptor.GetSecurityDescriptorDacl()
        owner = descriptor.GetSecurityDescriptorOwner()
        if (
            acl is None
            or owner is None
            or win32security.ConvertSidToStringSid(owner) != user_sid
        ):
            raise PermissionError("网关配置和状态目录须由当前 Windows 用户持有。")
        for index in range(acl.GetAceCount()):
            entry = acl.GetAce(index)
            ace_type, _flags = entry[0]
            sid = win32security.ConvertSidToStringSid(entry[-1])
            if (
                ace_type != win32security.ACCESS_DENIED_ACE_TYPE
                and sid not in permitted
            ):
                raise PermissionError(
                    "网关配置和状态目录须使用用户私有的 Windows ACL。"
                )
    except pywintypes.error as error:
        raise PermissionError("无法核验网关文件的 Windows ACL。") from error
