"""Enhanced code intelligence — AST-aware repository analysis.

Extracts symbols, builds dependency graphs, and provides
semantic file search. Falls back to regex for non-Python files.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class SymbolInfo:
    name: str
    kind: str                    # "function", "class", "method", "import", "variable"
    line: int
    parent: str = ""             # enclosing class name for methods
    signature: str = ""          # simplified signature string
    docstring: str = ""


@dataclass(slots=True)
class FileIntel:
    path: str
    symbols: list[SymbolInfo] = field(default_factory=list)
    imports: list[str] = field(default_factory=list)
    exports: list[str] = field(default_factory=list)
    category: str = "unknown"
    summary: str = ""


@dataclass(slots=True)
class DepEdge:
    source: str
    target: str
    kind: str = "import"         # "import", "relative_import", "reference"


# ---------------------------------------------------------------------------
# Regex patterns for non-Python files
# ---------------------------------------------------------------------------

JS_FUNCTION_RE = re.compile(
    r"""(?:export\s+)?(?:async\s+)?function\s+(\w+)\s*\(([^)]*)\)""",
    re.IGNORECASE,
)
JS_ARROW_RE = re.compile(
    r"""(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?\(([^)]*)\)""",
    re.IGNORECASE,
)
JS_CLASS_RE = re.compile(
    r"""(?:export\s+)?class\s+(\w+)""", re.IGNORECASE
)
JS_METHOD_RE = re.compile(
    r"""^\s*(?:async\s+)?(\w+)\s*\(([^)]*)\)\s*\{""", re.MULTILINE
)
JS_IMPORT_RE = re.compile(
    r"""import\s+.*?\s+from\s+['"]([^'"]+)['"]""", re.IGNORECASE
)
TS_DECORATOR_RE = re.compile(r"@\w+")

GO_FUNC_RE = re.compile(r"""func\s+(?:\([^)]*\)\s+)?(\w+)\s*\(([^)]*)\)""")
GO_METHOD_RE = re.compile(r"""func\s+\((\w+)\s+\*?(\w+)\)\s+(\w+)\s*\(([^)]*)\)""")
JAVA_CLASS_RE = re.compile(
    r"""(?:public\s+)?(?:abstract\s+)?(?:final\s+)?class\s+(\w+)""", re.IGNORECASE
)
JAVA_METHOD_RE = re.compile(
    r"""(?:public|private|protected)\s+(?:static\s+)?(?:final\s+)?(?:\w+(?:<[^>]+>)?)\s+(\w+)\s*\(([^)]*)\)""",
    re.IGNORECASE,
)
KT_FUN_RE = re.compile(r"""fun\s+(\w+)\s*\(([^)]*)\)""")
RS_FN_RE = re.compile(r"""fn\s+(\w+)\s*\(([^)]*)\)""")


# Mapping from file extension to parser dispatch
SUFFIX_PARSERS: dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".go": "go",
    ".java": "java",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".rs": "rust",
    ".cs": "csharp",
}


# ---------------------------------------------------------------------------
# CodeIntel
# ---------------------------------------------------------------------------

class CodeIntel:
    """Extracts symbols and builds dependency graphs from source files.

    Uses Python's ``ast`` module for .py files and regex-based
    fallback parsers for JavaScript, TypeScript, Go, Java, Kotlin,
    Rust, and C#.
    """

    def __init__(self, workspace_root: Path) -> None:
        self.workspace_root = workspace_root
        self._file_cache: dict[str, FileIntel | None] = {}

    # -- public API ------------------------------------------------------------

    def analyze_file(self, file_path: str) -> FileIntel | None:
        """Extract symbols and dependencies from a single file."""
        if file_path in self._file_cache:
            return self._file_cache[file_path]

        full_path = self.workspace_root / file_path
        if not full_path.is_file():
            self._file_cache[file_path] = None
            return None

        text = self._read_file(full_path)
        if text is None:
            self._file_cache[file_path] = None
            return None

        suffix = full_path.suffix.lower()
        parser_kind = SUFFIX_PARSERS.get(suffix, "")
        result = self._parse(text, parser_kind, file_path)
        self._file_cache[file_path] = result
        return result

    def analyze_many(self, file_paths: list[str]) -> dict[str, FileIntel]:
        """Batch-analyze multiple files."""
        result: dict[str, FileIntel] = {}
        for fp in file_paths:
            intel = self.analyze_file(fp)
            if intel is not None:
                result[fp] = intel
        return result

    def find_defining_file(self, symbol_name: str, candidate_paths: list[str]) -> str | None:
        """Find which file defines a symbol by name."""
        for fp in candidate_paths:
            intel = self.analyze_file(fp)
            if intel is None:
                continue
            for sym in intel.symbols:
                if sym.name == symbol_name:
                    return fp
                if sym.kind == "export" and symbol_name in sym.signature:
                    return fp
        return None

    def build_dep_graph(self, file_paths: list[str]) -> list[DepEdge]:
        """Build a dependency graph for a set of files."""
        edges: list[DepEdge] = []
        known_files: set[str] = set()
        # Index exports
        exports_map: dict[str, list[str]] = {}  # symbol -> [file_paths]
        for fp in file_paths:
            known_files.add(fp)
            intel = self.analyze_file(fp)
            if intel is None:
                continue
            for sym in intel.symbols:
                if sym.kind in {"function", "class", "export"}:
                    exports_map.setdefault(sym.name, []).append(fp)

        for fp in file_paths:
            intel = self.analyze_file(fp)
            if intel is None:
                continue
            for imp in intel.imports:
                # Resolve import to actual file
                resolved = self._resolve_import(imp, fp, known_files)
                if resolved:
                    edges.append(DepEdge(source=fp, target=resolved, kind="import"))
        return edges

    def summarize_file(self, file_path: str) -> str:
        """Produce a one-line summary of a file."""
        intel = self.analyze_file(file_path)
        if intel is None:
            return f"File {file_path} not found or unreadable."
        kinds: dict[str, int] = {}
        for sym in intel.symbols:
            kinds[sym.kind] = kinds.get(sym.kind, 0) + 1
        parts: list[str] = []
        if "class" in kinds:
            parts.append(f"{kinds['class']} classes")
        if "function" in kinds:
            parts.append(f"{kinds['function']} functions")
        if "method" in kinds:
            parts.append(f"{kinds['method']} methods")
        if intel.imports:
            parts.append(f"{len(intel.imports)} imports")
        summary = ", ".join(parts) if parts else "empty file"
        return f"{file_path}: {summary} [{intel.category}]"

    # -- internal --------------------------------------------------------------

    def _read_file(self, path: Path) -> str | None:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None
        if not text.strip():
            return None
        return text

    def _parse(self, text: str, parser_kind: str, file_path: str) -> FileIntel:
        if parser_kind == "python":
            return self._parse_python(text, file_path)
        if parser_kind in {"typescript", "javascript"}:
            return self._parse_js_ts(text, file_path, parser_kind)
        if parser_kind == "go":
            return self._parse_go(text, file_path)
        if parser_kind == "java":
            return self._parse_java(text, file_path)
        if parser_kind == "kotlin":
            return self._parse_kotlin(text, file_path)
        if parser_kind == "rust":
            return self._parse_rust(text, file_path)
        return self._parse_generic(text, file_path)

    # -- Python AST parser -----------------------------------------------------

    def _parse_python(self, text: str, file_path: str) -> FileIntel:
        symbols: list[SymbolInfo] = []
        imports: list[str] = []
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return FileIntel(path=file_path, category="code")

        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                parent = ""
                # Walk up to find parent class
                for ancestor in ast.walk(tree):
                    if isinstance(ancestor, ast.ClassDef) and _node_contains(ancestor, node):
                        parent = ancestor.name
                        break
                kind = "method" if parent else "function"
                sig = _py_signature(node)
                symbols.append(SymbolInfo(
                    name=node.name, kind=kind, line=node.lineno,
                    parent=parent, signature=sig,
                    docstring=ast.get_docstring(node) or "",
                ))
            elif isinstance(node, ast.ClassDef):
                symbols.append(SymbolInfo(
                    name=node.name, kind="class", line=node.lineno,
                    signature=_py_class_sig(node),
                    docstring=ast.get_docstring(node) or "",
                ))
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    imports.append(alias.name)
                    symbols.append(SymbolInfo(
                        name=alias.asname or alias.name, kind="import",
                        line=node.lineno, signature=alias.name,
                    ))
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    imports.append(node.module)
                for alias in node.names:
                    full = f"{node.module or ''}.{alias.name}" if node.module else alias.name
                    symbols.append(SymbolInfo(
                        name=alias.asname or alias.name, kind="import",
                        line=node.lineno, signature=full,
                    ))

        category = "code"
        if file_path.startswith("tests/") or file_path.endswith("_test.py"):
            category = "testing"
        elif file_path.endswith(".pyi"):
            category = "code"

        return FileIntel(
            path=file_path, symbols=symbols, imports=imports,
            exports=[s.name for s in symbols if s.kind in {"function", "class"}],
            category=category,
            summary=self._build_summary(symbols, imports),
        )

    # -- JS/TS parser ----------------------------------------------------------

    def _parse_js_ts(self, text: str, file_path: str, lang: str) -> FileIntel:
        symbols: list[SymbolInfo] = []
        imports: list[str] = []

        # Remove comments (simple approach)
        cleaned = re.sub(r"//[^\n]*", "", text)
        cleaned = re.sub(r"/\*[\s\S]*?\*/", "", cleaned)

        for m in JS_CLASS_RE.finditer(cleaned):
            symbols.append(SymbolInfo(name=m.group(1), kind="class", line=0))
        for m in JS_FUNCTION_RE.finditer(cleaned):
            symbols.append(SymbolInfo(
                name=m.group(1), kind="function", line=0,
                signature=f"({m.group(2).strip()})",
            ))
        for m in JS_ARROW_RE.finditer(cleaned):
            symbols.append(SymbolInfo(
                name=m.group(1), kind="function", line=0,
                signature=f"({m.group(2).strip()})",
            ))
        for m in JS_IMPORT_RE.finditer(cleaned):
            imports.append(m.group(1))

        category = "code"
        if "test" in file_path.lower() or file_path.startswith("__tests__"):
            category = "testing"

        return FileIntel(
            path=file_path, symbols=symbols, imports=imports,
            category=category,
            summary=self._build_summary(symbols, imports),
        )

    # -- Go parser -------------------------------------------------------------

    def _parse_go(self, text: str, file_path: str) -> FileIntel:
        symbols: list[SymbolInfo] = []
        imports: list[str] = []
        cleaned = re.sub(r"//[^\n]*", "", text)
        cleaned = re.sub(r"/\*[\s\S]*?\*/", "", cleaned)

        for m in GO_FUNC_RE.finditer(cleaned):
            symbols.append(SymbolInfo(
                name=m.group(1), kind="function", line=0,
                signature=f"({m.group(2).strip()})",
            ))
        for m in GO_METHOD_RE.finditer(cleaned):
            symbols.append(SymbolInfo(
                name=m.group(3), kind="method", line=0, parent=m.group(2),
                signature=f"({m.group(4).strip()})",
            ))
        import_block = re.search(r'import\s*(?:\((.*?)\)|"([^"]+)")', cleaned, re.DOTALL)
        if import_block:
            if import_block.group(1):
                imports.extend(re.findall(r'"([^"]+)"', import_block.group(1)))
            elif import_block.group(2):
                imports.append(import_block.group(2))
        return FileIntel(
            path=file_path, symbols=symbols, imports=imports, category="code",
            summary=self._build_summary(symbols, imports),
        )

    # -- Java parser -----------------------------------------------------------

    def _parse_java(self, text: str, file_path: str) -> FileIntel:
        symbols: list[SymbolInfo] = []
        imports: list[str] = re.findall(r"import\s+([\w.]+)", text)
        cleaned = re.sub(r"//[^\n]*", "", text)
        cleaned = re.sub(r"/\*[\s\S]*?\*/", "", cleaned)

        for m in JAVA_CLASS_RE.finditer(cleaned):
            symbols.append(SymbolInfo(name=m.group(1), kind="class", line=0))
        for m in JAVA_METHOD_RE.finditer(cleaned):
            symbols.append(SymbolInfo(
                name=m.group(1), kind="method", line=0,
                signature=f"({m.group(2).strip()})",
            ))
        return FileIntel(
            path=file_path, symbols=symbols, imports=imports, category="code",
            summary=self._build_summary(symbols, imports),
        )

    # -- Kotlin parser ---------------------------------------------------------

    def _parse_kotlin(self, text: str, file_path: str) -> FileIntel:
        symbols: list[SymbolInfo] = []
        imports: list[str] = []
        cleaned = re.sub(r"//[^\n]*", "", text)
        cleaned = re.sub(r"/\*[\s\S]*?\*/", "", cleaned)

        for m in JAVA_CLASS_RE.finditer(cleaned):
            symbols.append(SymbolInfo(name=m.group(1), kind="class", line=0))
        for m in KT_FUN_RE.finditer(cleaned):
            symbols.append(SymbolInfo(
                name=m.group(1), kind="function", line=0,
                signature=f"({m.group(2).strip()})",
            ))
        return FileIntel(
            path=file_path, symbols=symbols, imports=imports, category="code",
            summary=self._build_summary(symbols, imports),
        )

    # -- Rust parser -----------------------------------------------------------

    def _parse_rust(self, text: str, file_path: str) -> FileIntel:
        symbols: list[SymbolInfo] = []
        imports: list[str] = []
        cleaned = re.sub(r"//[^\n]*", "", text)
        cleaned = re.sub(r"/\*[\s\S]*?\*/", "", cleaned)

        for m in RS_FN_RE.finditer(cleaned):
            symbols.append(SymbolInfo(
                name=m.group(1), kind="function", line=0,
                signature=f"({m.group(2).strip()})",
            ))
        struct_matches = re.finditer(r"struct\s+(\w+)", cleaned)
        for m in struct_matches:
            symbols.append(SymbolInfo(name=m.group(1), kind="class", line=0))
        use_matches = re.finditer(r"use\s+([\w:]+)", cleaned)
        for m in use_matches:
            imports.append(m.group(1))
        return FileIntel(
            path=file_path, symbols=symbols, imports=imports, category="code",
            summary=self._build_summary(symbols, imports),
        )

    # -- Generic fallback ------------------------------------------------------

    def _parse_generic(self, text: str, file_path: str) -> FileIntel:
        return FileIntel(path=file_path, category="unknown")

    # -- helpers ---------------------------------------------------------------

    def _build_summary(self, symbols: list[SymbolInfo], imports: list[str]) -> str:
        parts: list[str] = []
        kinds: dict[str, int] = {}
        for s in symbols:
            kinds[s.kind] = kinds.get(s.kind, 0) + 1
        for k, v in sorted(kinds.items()):
            parts.append(f"{v} {k}{'s' if v > 1 else ''}")
        if imports:
            parts.append(f"{len(imports)} imports")
        return ", ".join(parts) if parts else "empty"

    def _resolve_import(self, imp: str, source_file: str, known_files: set[str]) -> str | None:
        """Resolve an import string to a known file path."""
        # Direct match
        if imp in known_files:
            return imp
        # With .py extension
        py_version = imp.replace(".", "/") + ".py"
        if py_version in known_files:
            return py_version
        # Partial match
        imp_parts = imp.split(".")
        for kf in known_files:
            kf_normalized = kf.replace("/", ".").replace(".py", "").replace(".ts", "").replace(".js", "")
            if kf_normalized.endswith(imp) or imp.endswith(kf_normalized):
                return kf
            if imp_parts[-1] in kf:
                return kf
        return None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _node_contains(parent: ast.AST, child: ast.AST) -> bool:
    """Check if parent node contains child node by line range."""
    return (
        hasattr(parent, "lineno") and hasattr(child, "lineno")
        and hasattr(parent, "end_lineno") and parent.end_lineno is not None
        and parent.lineno <= child.lineno <= parent.end_lineno
    )


def _py_signature(node: ast.FunctionDef) -> str:
    """Build a simplified signature string for a Python function."""
    args: list[str] = []
    for arg in node.args.args:
        annotation = ""
        if arg.annotation:
            annotation = f": {ast.unparse(arg.annotation)}" if hasattr(ast, "unparse") else ""
        args.append(f"{arg.arg}{annotation}")
    if node.args.vararg:
        args.append(f"*{node.args.vararg.arg}")
    if node.args.kwarg:
        args.append(f"**{node.args.kwarg.arg}")
    return f"({', '.join(args)})"


def _py_class_sig(node: ast.ClassDef) -> str:
    """Build a signature for a class showing bases."""
    bases = [ast.unparse(b) for b in node.bases] if hasattr(ast, "unparse") else []
    return f"({', '.join(bases)})" if bases else ""
