"""Find repositories and resolve their configured worktrees."""

import re
import subprocess
from pathlib import Path

from .config import SETTINGS
from .notify import BridgeError

BRANCHES = SETTINGS.workspace_branches


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    ).stdout.strip()


def available_repositories(root=SETTINGS.workspace_root):
    if root is None:
        return []
    return sorted(
        path.name
        for path in root.iterdir()
        if path.is_dir()
        and not path.is_symlink()
        and (path / ".git").exists()
        and re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", path.name)
    )


def mentioned_repository(summary, repositories):
    matches = [
        name
        for name in repositories
        if re.search(
            rf"(?<![\w.-]){re.escape(name)}(?:\.git)?(?![\w.-])", summary, re.IGNORECASE
        )
    ]
    return matches[0] if len(matches) == 1 else None


def workspace_for(name, root=SETTINGS.workspace_root):
    if root is None:
        raise BridgeError("请先在本机配置 workspace_root。")
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", name):
        raise BridgeError("请填写工作区下的仓库目录名。")
    repo = root / name
    if repo.is_symlink() or not (repo / ".git").exists():
        raise BridgeError(f"工作区下找不到仓库 {name}。")
    if Path(git(repo, "rev-parse", "--show-toplevel")).resolve() != repo.resolve():
        raise BridgeError(f"{name} 不是独立仓库。")
    desired_branch = BRANCHES.get(name)
    if desired_branch is None:
        return repo.resolve()
    for entry in git(repo, "worktree", "list", "--porcelain").split("\n\n"):
        fields = dict(line.split(" ", 1) for line in entry.splitlines() if " " in line)
        if fields.get("branch") == f"refs/heads/{desired_branch}":
            path = Path(fields["worktree"]).resolve()
            if path.is_relative_to(root.resolve()) and path.is_dir():
                return path
    raise BridgeError(f"{name} 找不到已签出 {desired_branch} 的工作树。")
