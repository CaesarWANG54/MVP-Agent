from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from .utils import run_subprocess_capture


TOKEN_RE = re.compile(r"[A-Za-z0-9_]+")
PATH_LIKE_RE = re.compile(r"[\w./\\-]+\.[A-Za-z0-9]+")
EXCLUDED_PREFIXES = (
    ".git/",
    "mvp_runs/",
    "__pycache__/",
    ".venv/",
    "venv/",
    "env/",
    "node_modules/",
)
EXCLUDED_SEGMENTS = (
    "/__pycache__/",
    ".pyc",
)


@dataclass(slots=True)
class RepoFile:
    path: str
    basename: str
    parent: str
    suffix: str
    parts: tuple[str, ...]
    tokens: tuple[str, ...]
    category: str


class RepositoryIndex:
    def __init__(self, workspace_root: Path, max_files: int = 8000) -> None:
        self.workspace_root = workspace_root
        self.max_files = max_files
        self._entries: list[RepoFile] | None = None
        self._by_lower_path: dict[str, RepoFile] = {}
        self._by_basename: dict[str, list[RepoFile]] = {}

    def infer_files(self, text: str, existing_paths: list[str] | None = None, limit: int = 6) -> list[str]:
        entries = self.entries()
        if not entries:
            raw_existing = [item.strip().replace("\\", "/") for item in (existing_paths or []) if item and item.strip()]
            return raw_existing[:limit]

        query = text.strip()
        query_lower = query.lower()
        query_tokens = set(self._tokenize(query))
        normalized_existing = self.normalize_paths(existing_paths or [])
        explicit_matches = self.normalize_paths(PATH_LIKE_RE.findall(query))

        scored: dict[str, float] = {}
        ordered_explicit = self._merge_unique(normalized_existing, explicit_matches)
        for rank, path in enumerate(ordered_explicit):
            scored[path] = max(scored.get(path, 0.0), 120.0 - rank)

        for entry in entries:
            score = scored.get(entry.path, 0.0)
            overlap = query_tokens & set(entry.tokens)
            if overlap:
                score += len(overlap) * 4.0

            basename_tokens = set(self._tokenize(entry.basename))
            basename_overlap = query_tokens & basename_tokens
            if basename_overlap:
                score += len(basename_overlap) * 6.0

            if entry.basename.lower() in query_lower:
                score += 25.0
            if entry.path.lower() in query_lower:
                score += 40.0

            if any(part in query_tokens for part in entry.parts[:2]):
                score += 3.0

            if entry.category == "testing" and self._contains_any(query_lower, ("test", "qa", "regression", "unit", "integration")):
                score += 8.0
            if entry.category == "documentation" and self._contains_any(query_lower, ("doc", "readme", "guide", "document")):
                score += 8.0
            if entry.category == "config" and self._contains_any(query_lower, ("config", "setting", "json", "yaml", "toml")):
                score += 6.0
            if entry.category == "code" and self._contains_any(query_lower, ("code", "module", "function", "class", "refactor", "bug", "feature", "router", "planner", "orchestrator")):
                score += 2.0

            if score > 0.0:
                scored[entry.path] = score

        ranked = sorted(
            scored.items(),
            key=lambda item: (
                -item[1],
                len(item[0].split("/")),
                len(item[0]),
                item[0],
            ),
        )
        return [path for path, _ in ranked[:limit]]

    def normalize_paths(self, paths: list[str]) -> list[str]:
        normalized: list[str] = []
        for raw in paths:
            matched = self.match_path(raw)
            if matched and matched.path not in normalized:
                normalized.append(matched.path)
        return normalized

    def cluster_files(self, paths: list[str], max_groups: int = 3) -> list[list[str]]:
        normalized = self.normalize_paths(paths)
        if not normalized:
            return []
        if len(normalized) == 1:
            return [normalized]

        top_levels = {path.split("/", 1)[0] for path in normalized}
        groups: dict[str, list[str]] = {}
        for path in normalized:
            key = self._group_key(path, top_levels)
            groups.setdefault(key, []).append(path)

        # Merge groups whose files likely import each other
        merged_groups = self._merge_cross_import_groups(
            list(groups.values()))
        ordered_groups = sorted(
            merged_groups,
            key=lambda items: (self._path_order(normalized, items[0]), len(items)),
        )
        if len(ordered_groups) <= max_groups:
            return ordered_groups

        kept = ordered_groups[: max_groups - 1]
        merged_tail: list[str] = []
        for group in ordered_groups[max_groups - 1 :]:
            merged_tail.extend(group)
        kept.append(merged_tail)
        return kept

    def _merge_cross_import_groups(
            self, groups: list[list[str]]) -> list[list[str]]:
        """Merge groups whose files likely import each other based on shared prefixes."""
        if len(groups) <= 1:
            return groups
        merged: list[list[str]] = []
        used: set[int] = set()
        for i, group in enumerate(groups):
            if i in used:
                continue
            current = list(group)
            used.add(i)
            for j, other in enumerate(groups):
                if j in used:
                    continue
                if self._groups_likely_related(current, other):
                    current.extend(other)
                    used.add(j)
            merged.append(current)
        return merged

    @staticmethod
    def _groups_likely_related(a: list[str], b: list[str]) -> bool:
        """Check if two file groups likely cross-import based on shared module prefixes."""
        a_prefixes = {"/".join(p.split("/")[:-1]) for p in a if "/" in p}
        b_prefixes = {"/".join(p.split("/")[:-1]) for p in b if "/" in p}
        # Same parent directory
        if a_prefixes & b_prefixes:
            return True
        # Check if any file basename appears across groups (potential import target)
        a_names = {p.split("/")[-1].rsplit(".", 1)[0] for p in a}
        b_names = {p.split("/")[-1].rsplit(".", 1)[0] for p in b}
        if a_names & b_names:
            return True
        # Check if package init files relate the groups
        a_dirs = {p.rsplit("/", 1)[0] for p in a}
        for bp in b:
            bp_dir = bp.rsplit("/", 1)[0]
            if bp_dir in a_dirs:
                return True
        return False

    def describe_scope(self, paths: list[str]) -> str:
        normalized = self.normalize_paths(paths)
        if not normalized:
            return "general scope"
        if len(normalized) == 1:
            return normalized[0]

        top_levels = []
        for path in normalized:
            top = path.split("/", 1)[0]
            if top not in top_levels:
                top_levels.append(top)
        if len(top_levels) == 1:
            return f"{top_levels[0]}/..."
        if len(top_levels) == 2:
            return f"{top_levels[0]} + {top_levels[1]}"
        return ", ".join(top_levels[:2]) + " + more"

    def entries(self) -> list[RepoFile]:
        if self._entries is None:
            self._entries = self._build_entries()
        return self._entries

    def match_path(self, value: str) -> RepoFile | None:
        candidate = value.strip().replace("\\", "/").lstrip("./")
        if not candidate:
            return None

        lower = candidate.lower()
        direct = self._by_lower_path.get(lower)
        if direct is not None:
            return direct

        basename = Path(candidate).name.lower()
        basename_matches = self._by_basename.get(basename, [])
        if len(basename_matches) == 1:
            return basename_matches[0]

        suffix_matches = [entry for entry in self.entries() if entry.path.lower().endswith(lower)]
        if len(suffix_matches) == 1:
            return suffix_matches[0]

        candidate_tokens = set(self._tokenize(candidate))
        best_entry: RepoFile | None = None
        best_score = 0.0
        for entry in self.entries():
            score = 0.0
            if entry.basename.lower() == basename:
                score += 30.0
            overlap = candidate_tokens & set(entry.tokens)
            if overlap:
                score += len(overlap) * 4.0
            if lower in entry.path.lower():
                score += 8.0
            if score > best_score:
                best_entry = entry
                best_score = score
        return best_entry if best_score >= 8.0 else None

    def _build_entries(self) -> list[RepoFile]:
        raw_paths = self._discover_paths()
        entries: list[RepoFile] = []
        self._by_lower_path.clear()
        self._by_basename.clear()
        for raw_path in raw_paths[: self.max_files]:
            normalized = raw_path.replace("\\", "/").strip().lstrip("./")
            if (
                not normalized
                or any(normalized.startswith(prefix) for prefix in EXCLUDED_PREFIXES)
                or any(segment in f"/{normalized}" for segment in EXCLUDED_SEGMENTS)
            ):
                continue
            path_obj = Path(normalized)
            parts = tuple(part.lower() for part in path_obj.parts if part)
            entry = RepoFile(
                path=normalized,
                basename=path_obj.name,
                parent=path_obj.parent.as_posix() if path_obj.parent.as_posix() != "." else "",
                suffix=path_obj.suffix.lower(),
                parts=parts,
                tokens=tuple(self._tokenize(normalized)),
                category=self._categorize(path_obj),
            )
            entries.append(entry)
            self._by_lower_path[normalized.lower()] = entry
            self._by_basename.setdefault(entry.basename.lower(), []).append(entry)
        return entries

    def _discover_paths(self) -> list[str]:
        rg_bin = shutil.which("rg")
        if rg_bin:
            try:
                completed = run_subprocess_capture([rg_bin, "--files"], cwd=self.workspace_root, timeout=12)
                if completed.returncode == 0:
                    return [line.strip() for line in completed.stdout.splitlines() if line.strip()]
            except Exception:
                pass

        paths: list[str] = []
        for path in self.workspace_root.rglob("*"):
            if path.is_file():
                try:
                    relative = path.relative_to(self.workspace_root).as_posix()
                except ValueError:
                    continue
                paths.append(relative)
        return paths

    def _categorize(self, path: Path) -> str:
        normalized = path.as_posix().lower()
        suffix = path.suffix.lower()
        if normalized.startswith(("tests/", "test/")) or path.name.lower().startswith("test_") or normalized.endswith("_test.py"):
            return "testing"
        if normalized.startswith(("docs/",)) or suffix in {".md", ".rst", ".txt"}:
            return "documentation"
        if suffix in {".json", ".toml", ".yaml", ".yml", ".ini"}:
            return "config"
        if suffix in {".png", ".jpg", ".jpeg", ".gif", ".ico", ".svg"}:
            return "asset"
        if suffix in {".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs", ".java", ".kt", ".cs"}:
            return "code"
        return "other"

    def _group_key(self, path: str, top_levels: set[str]) -> str:
        parts = path.split("/")
        if len(top_levels) > 1:
            return parts[0]
        if len(parts) >= 2:
            if parts[0] == "mvp" and len(parts) >= 3 and parts[1] == "workers":
                return "mvp/workers"
            return "/".join(parts[:2])
        return parts[0]

    def _path_order(self, ordered_paths: list[str], path: str) -> int:
        try:
            return ordered_paths.index(path)
        except ValueError:
            return len(ordered_paths)

    def _tokenize(self, value: str) -> list[str]:
        return [token.lower() for token in TOKEN_RE.findall(value)]

    def _contains_any(self, value: str, needles: tuple[str, ...]) -> bool:
        return any(needle in value for needle in needles)

    def _merge_unique(self, left: list[str], right: list[str]) -> list[str]:
        merged = list(left)
        for item in right:
            if item not in merged:
                merged.append(item)
        return merged
