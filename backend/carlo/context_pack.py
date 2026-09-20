"""Deterministic, bounded repository context for an implementation work package."""

import ast
import re
from pathlib import Path
from typing import Any

from .planning import ImplementationTask


class ContextPackError(ValueError):
    pass


class ContextPackBudgetExceeded(ContextPackError):
    def __init__(self, estimated_tokens: int, max_tokens: int) -> None:
        self.estimated_tokens = estimated_tokens
        self.max_tokens = max_tokens
        super().__init__(
            f"context pack needs about {estimated_tokens} tokens, above the {max_tokens}-token budget; replan into smaller packages"
        )


def _symbol_span(path: Path, source: str, symbol: str) -> tuple[int, int]:
    if path.suffix == ".py":
        try:
            tree = ast.parse(source)
        except SyntaxError as error:
            raise ContextPackError(f"cannot parse {path.name} for symbol {symbol}: {error.msg}") from error
        matches: list[tuple[int, int]] = []

        def visit(body: list[ast.stmt], prefix: str = "") -> None:
            for node in body:
                if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                    name = f"{prefix}{node.name}"
                    if name == symbol or ("." not in symbol and node.name == symbol):
                        start = min([node.lineno, *(item.lineno for item in node.decorator_list)])
                        matches.append((start, node.end_lineno or node.lineno))
                    visit(node.body, f"{name}.")

        visit(tree.body)
    else:
        name = re.escape(symbol.rsplit(".", 1)[-1])
        declaration = re.compile(
            rf"\b(?:class|struct|enum|protocol|extension|func|function|interface|type|const|let|var)\s+{name}\b"
        )
        lines = source.splitlines()
        matches = []
        for index, line in enumerate(lines):
            if line.lstrip().startswith(("//", "/*", "*", "#")) or not declaration.search(line):
                continue
            depth = 0
            opened = False
            for end in range(index, len(lines)):
                depth += lines[end].count("{") - lines[end].count("}")
                opened |= "{" in lines[end]
                if opened and depth == 0:
                    matches.append((index + 1, end + 1))
                    break
            else:
                raise ContextPackError(f"cannot determine end of symbol {symbol} in {path.name}; use line ranges")
    if len(matches) != 1:
        description = "missing" if not matches else "ambiguous"
        raise ContextPackError(f"{description} symbol {symbol} in {path.name}; use exact line ranges")
    return matches[0]


def build_context_pack(
    repository: Path,
    package: dict[str, Any],
    *,
    max_tokens: int = 18_000,
    brief: str = "",
) -> str:
    """Return stable text; reject missing, unsafe, or oversized inputs."""
    if max_tokens < 1:
        raise ValueError("max_tokens must be positive")
    if "prompt" in package and "objective" not in package:
        raise ContextPackError("legacy work package cannot execute; regenerate the plan into complete work packages and approve the new revision")
    try:
        item = ImplementationTask.model_validate(package)
    except ValueError as error:
        raise ContextPackError(f"incomplete work package: {error}") from error
    root = repository.resolve()
    if len({entry.path for entry in item.files}) != len(item.files):
        raise ContextPackError("work package lists a file more than once")

    parts = ([f"Parent Brief:\n{brief}"] if brief else []) + [
        "Implement only this work package.",
        "The required file context is supplied below. Read or explore other files only if indispensable; explain why in your response.",
        f"Objective: {item.objective}",
        "Changes:",
        *(f"- {path}: {item.changes[path]}" for path in sorted(item.changes)),
        "Interfaces:",
        *(f"- {contract}" for contract in item.interfaces),
        "Constraints:",
        *(f"- {constraint}" for constraint in item.constraints),
        "Verification commands:",
        *(f"- {command}" for command in item.verification.commands),
        f"Verification succeeds when: {item.verification.success}",
        "Done when:",
        *(f"- {check}" for check in item.done_when),
        "File context:",
    ]
    for entry in sorted(item.files, key=lambda file: file.path):
        path = (root / entry.path).resolve()
        if not path.is_relative_to(root):
            raise ContextPackError(f"file is outside the project: {entry.path}")
        if not path.exists():
            if entry.mode != "create":
                raise ContextPackError(f"file is missing: {entry.path}")
            parts.append(f"File: {entry.path} (create; not yet present)")
            continue
        if not path.is_file():
            raise ContextPackError(f"not a regular file: {entry.path}")
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            raise ContextPackError(f"cannot read {entry.path}: {error}") from error
        lines = source.splitlines()
        spans = [(span.start, span.end) for span in entry.ranges]
        spans.extend(_symbol_span(path, source, symbol) for symbol in entry.symbols)
        if spans and any(end > len(lines) for _, end in spans):
            raise ContextPackError(f"range exceeds file length: {entry.path}")
        selected = (
            sorted({line for start, end in spans for line in range(start, end + 1)})
            if spans else list(range(1, len(lines) + 1))
        )
        range_label = "all lines" if not spans else ", ".join(
            f"{start}-{end}" for start, end in sorted(set(spans))
        )
        parts.append(f"File: {entry.path} ({entry.mode}; {range_label}; reason: {entry.reason})")
        parts.extend(f"{number} | {lines[number - 1]}" for number in selected)
    pack = "\n".join(parts) + "\n"
    estimated_tokens = (len(pack.encode("utf-8")) + 2) // 3
    if estimated_tokens > max_tokens:
        raise ContextPackBudgetExceeded(estimated_tokens, max_tokens)
    return pack
