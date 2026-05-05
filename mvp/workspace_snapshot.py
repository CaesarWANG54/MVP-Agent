from __future__ import annotations

from pathlib import Path

from .utils import run_subprocess_capture, safe_snippet


SKIP_NAMES = {
    ".git",
    ".idea",
    ".vscode",
    "__pycache__",
    "node_modules",
    ".venv",
    "venv",
    "dist",
    "build",
}


def collect_workspace_snapshot(
    workspace_root: Path,
    *,
    max_tree_entries: int = 180,
    max_depth: int = 3,
    max_status_lines: int = 40,
) -> dict[str, str]:
    root = workspace_root.resolve()
    return {
        "tree": build_file_tree_text(root, max_entries=max_tree_entries, max_depth=max_depth),
        "diff": build_git_diff_text(root, max_status_lines=max_status_lines),
    }


def build_file_tree_text(workspace_root: Path, *, max_entries: int = 180, max_depth: int = 3) -> str:
    lines = [f"{workspace_root.name}/"]
    count = 0
    truncated = False

    def walk(directory: Path, prefix: str, depth: int) -> None:
        nonlocal count, truncated
        if depth > max_depth or truncated:
            return
        try:
            children = sorted(
                (
                    child
                    for child in directory.iterdir()
                    if child.name not in SKIP_NAMES and not child.name.startswith(".")
                ),
                key=lambda item: (not item.is_dir(), item.name.lower()),
            )
        except OSError:
            return

        for index, child in enumerate(children):
            if count >= max_entries:
                truncated = True
                return
            connector = "└─" if index == len(children) - 1 else "├─"
            suffix = "/" if child.is_dir() else ""
            lines.append(f"{prefix}{connector} {child.name}{suffix}")
            count += 1
            if child.is_dir():
                extension = "   " if index == len(children) - 1 else "│  "
                walk(child, prefix + extension, depth + 1)

    walk(workspace_root, "", 1)
    if truncated:
        lines.append("… 已截断。")
    return "\n".join(lines)


def build_git_diff_text(workspace_root: Path, *, max_status_lines: int = 40) -> str:
    if not _is_git_repo(workspace_root):
        return "当前工作区不是 Git 仓库，无法显示 diff。"

    branch = _git_text(workspace_root, ["git", "branch", "--show-current"], timeout=10) or "(detached)"
    status = _git_text(workspace_root, ["git", "status", "--short"], timeout=12)
    staged = _git_text(workspace_root, ["git", "diff", "--cached", "--stat", "--compact-summary", "--no-ext-diff"], timeout=15)
    unstaged = _git_text(workspace_root, ["git", "diff", "--stat", "--compact-summary", "--no-ext-diff"], timeout=15)

    lines = [f"分支：{branch}"]
    if not status:
        lines.extend(["", "工作区干净，没有未提交改动。"])
        return "\n".join(lines)

    status_lines = status.splitlines()
    lines.extend(["", "状态", *status_lines[:max_status_lines]])
    if len(status_lines) > max_status_lines:
        lines.append(f"… 还有 {len(status_lines) - max_status_lines} 项改动。")

    lines.extend(["", "未暂存 Diff"])
    lines.extend(_section_lines(unstaged, empty_label="没有未暂存改动。"))
    lines.extend(["", "已暂存 Diff"])
    lines.extend(_section_lines(staged, empty_label="没有已暂存改动。"))
    return "\n".join(lines)


def _section_lines(raw_text: str, *, empty_label: str) -> list[str]:
    text = raw_text.strip()
    if not text:
        return [empty_label]
    return [safe_snippet(line, 120) for line in text.splitlines()]


def _is_git_repo(workspace_root: Path) -> bool:
    try:
        result = run_subprocess_capture(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=workspace_root,
            timeout=10,
        )
    except FileNotFoundError:
        return False
    return result.returncode == 0


def _git_text(workspace_root: Path, command: list[str], *, timeout: int) -> str:
    try:
        result = run_subprocess_capture(command, cwd=workspace_root, timeout=timeout)
    except FileNotFoundError:
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()
