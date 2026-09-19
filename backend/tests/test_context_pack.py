from pathlib import Path

import pytest

from carlo.context_pack import ContextPackBudgetExceeded, ContextPackError, build_context_pack


def package(files: list[dict]) -> dict:
    return {
        "id": "wp-1", "title": "Implement parser", "position": 0,
        "objective": "The parser accepts quoted fields.", "files": files,
        "interfaces": ["parse(text: str) -> list[str] preserves input order"],
        "changes": {item["path"]: "Handle quoted fields" for item in files if item["mode"] != "read_only"},
        "constraints": ["Do not add dependencies"],
        "verification": {"commands": ["pytest -q tests/test_parser.py --tb=short"], "success": "Tests pass"},
        "done_when": ["Quoted fields are covered"], "budget": {"max_tool_calls": 20},
    }


def test_pack_is_byte_stable_and_sorts_files_and_ranges(tmp_path: Path) -> None:
    (tmp_path / "b.py").write_text("one\ntwo\nthree\nfour\n")
    (tmp_path / "a.py").write_text("alpha\nbeta\n")
    files = [
        {"path": "b.py", "mode": "edit", "reason": "Parser", "ranges": [{"start": 3, "end": 4}, {"start": 1, "end": 1}]},
        {"path": "a.py", "mode": "read_only", "reason": "Contract"},
    ]
    first = build_context_pack(tmp_path, package(files), max_tokens=18_000)
    second = build_context_pack(tmp_path, package(list(reversed(files))), max_tokens=18_000)

    assert first == second
    assert first.index("File: a.py") < first.index("File: b.py")
    assert "1 | one" in first and "3 | three" in first and "4 | four" in first
    assert "2 | two" not in first
    assert first.index("Objective:") < first.index("File: a.py")


def test_pack_extracts_python_and_swift_symbols(tmp_path: Path) -> None:
    (tmp_path / "parser.py").write_text(
        "def unrelated():\n    return 1\n\nclass Parser:\n    def parse(self):\n        return 2\n"
    )
    (tmp_path / "Runner.swift").write_text(
        "func other() {\n    print(1)\n}\nfunc run() {\n    print(2)\n}\n"
    )
    files = [
        {"path": "parser.py", "mode": "edit", "reason": "Parser", "symbols": ["Parser.parse"]},
        {"path": "Runner.swift", "mode": "read_only", "reason": "Caller", "symbols": ["run"]},
    ]
    result = build_context_pack(tmp_path, package(files), max_tokens=18_000)

    assert "def parse(self):" in result and "return 2" in result
    assert "def unrelated():" not in result
    assert "func run()" in result and "print(2)" in result
    assert "func other()" not in result


def test_swift_symbol_lookup_ignores_commented_declarations(tmp_path: Path) -> None:
    (tmp_path / "Runner.swift").write_text(
        "// func run() { old example }\nfunc run() {\n    print(2)\n}\n"
    )
    result = build_context_pack(
        tmp_path,
        package([{"path": "Runner.swift", "mode": "edit", "reason": "Caller", "symbols": ["run"]}]),
    )
    assert "func run() {" in result
    assert "old example" not in result


def test_pack_budget_is_rejected_not_truncated(tmp_path: Path) -> None:
    (tmp_path / "large.py").write_text("value = 1\n" * 200)
    with pytest.raises(ContextPackBudgetExceeded) as error:
        build_context_pack(
            tmp_path, package([{"path": "large.py", "mode": "edit", "reason": "Parser"}]),
            max_tokens=100,
        )
    assert error.value.estimated_tokens > 100


def test_pack_rejects_project_escape_and_missing_symbols(tmp_path: Path) -> None:
    external = tmp_path.parent / "external.txt"
    external.write_text("secret")
    (tmp_path / "link.txt").symlink_to(external)
    with pytest.raises(ContextPackError, match="outside"):
        build_context_pack(
            tmp_path, package([{"path": "link.txt", "mode": "edit", "reason": "Parser"}]),
        )
    (tmp_path / "parser.py").write_text("def actual():\n    pass\n")
    with pytest.raises(ContextPackError, match="missing"):
        build_context_pack(
            tmp_path, package([{"path": "parser.py", "mode": "edit", "reason": "Parser", "symbols": ["unknown"]}]),
        )


def test_legacy_package_is_refused_with_regeneration_instruction(tmp_path: Path) -> None:
    with pytest.raises(ContextPackError, match="legacy work package.*regenerate"):
        build_context_pack(tmp_path, {"title": "Old task", "prompt": "Implement everything"})
