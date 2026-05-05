from __future__ import annotations

from pathlib import Path

from .utils import run_subprocess_capture, safe_snippet


def build_code_stream_chunks(
    workspace_root: Path,
    *,
    target_files: list[str],
    changes_made: list[str],
    summary: str,
    deliverable: str,
    max_chunks: int = 4,
) -> list[str]:
    files = _candidate_files(target_files, changes_made)
    chunks: list[str] = []

    headline_parts = []
    if summary.strip():
        headline_parts.append(f"摘要：{summary.strip()}")
    if deliverable.strip() and deliverable.strip() != summary.strip():
        headline_parts.append(f"交付：{safe_snippet(deliverable.strip(), 360)}")
    if files:
        headline_parts.append("作用域：" + ", ".join(files[:6]))
    if headline_parts:
        chunks.append("\n".join(headline_parts))

    diff_preview = build_targeted_diff_preview(workspace_root, files)
    if diff_preview:
        chunks.append("代码差异预览\n" + diff_preview)
    else:
        file_previews = build_file_previews(workspace_root, files, max_files=2)
        chunks.extend(file_previews)

    return chunks[:max_chunks]


def build_targeted_diff_preview(
    workspace_root: Path,
    files: list[str],
    *,
    max_lines: int = 140,
) -> str:
    if not files or not (workspace_root / ".git").exists():
        return ""
    command = ["git", "diff", "--no-ext-diff", "--unified=0", "--", *files[:8]]
    try:
        completed = run_subprocess_capture(command, cwd=workspace_root, timeout=18)
    except Exception:
        return ""
    if completed.returncode != 0:
        return ""
    text = completed.stdout.strip()
    if not text:
        return ""
    lines = text.splitlines()[:max_lines]
    if len(text.splitlines()) > max_lines:
        lines.append("... diff truncated ...")
    return "\n".join(lines)


def build_file_previews(
    workspace_root: Path,
    files: list[str],
    *,
    max_files: int = 2,
    max_lines: int = 48,
) -> list[str]:
    previews: list[str] = []
    for rel_path in files[:max_files]:
        full_path = workspace_root / rel_path
        if not full_path.is_file():
            continue
        try:
            text = full_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        snippet_lines = text.splitlines()[:max_lines]
        if len(text.splitlines()) > max_lines:
            snippet_lines.append("... file truncated ...")
        language = _language_hint(rel_path)
        previews.append(
            "\n".join(
                [
                    f"文件预览：{rel_path}",
                    f"```{language}",
                    "\n".join(snippet_lines).rstrip(),
                    "```",
                ]
            ).strip()
        )
    return previews


def _candidate_files(target_files: list[str], changes_made: list[str]) -> list[str]:
    candidates: list[str] = []
    for raw in [*target_files, *changes_made]:
        value = str(raw or "").strip().replace("\\", "/")
        if not value:
            continue
        token = value.split(":", 1)[0].strip()
        if "/" not in token and "." not in Path(token).name:
            continue
        if token not in candidates:
            candidates.append(token.lstrip("./"))
    return candidates


def _language_hint(path: str) -> str:
    suffix = Path(path).suffix.lower()
    mapping = {
        ".py": "python",
        ".ts": "ts",
        ".tsx": "tsx",
        ".js": "js",
        ".jsx": "jsx",
        ".json": "json",
        ".md": "md",
        ".toml": "toml",
        ".yaml": "yaml",
        ".yml": "yaml",
        ".java": "java",
        ".go": "go",
        ".rs": "rust",
        ".kt": "kotlin",
        ".cs": "csharp",
    }
    return mapping.get(suffix, "")
