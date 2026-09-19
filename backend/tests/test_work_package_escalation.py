from carlo.work_package_escalation import within_approved_scope, within_approved_scope_split


def package() -> dict:
    return {
        "id": "wp-1", "title": "Fix parser", "position": 0,
        "objective": "Parse the input", "files": [{"path": "src/parser.py", "mode": "edit", "reason": "Parser"}],
        "interfaces": ["parse(text) -> dict"],
        "changes": {"src/parser.py": "Handle valid input"},
        "constraints": ["No dependencies"],
        "verification": {"commands": ["pytest -q tests/test_parser.py"], "success": "Pass"},
        "done_when": ["Parser passes tests"], "budget": {"max_tool_calls": 20},
    }


def test_corrected_changes_in_same_files_stay_in_scope() -> None:
    original = package()
    revised = {**original, "changes": {"src/parser.py": "Handle empty input too"}}
    ok, reason = within_approved_scope(original, revised)
    assert ok
    assert reason is None


def test_new_edit_file_or_removed_constraint_requires_approval() -> None:
    original = package()
    revised = {**original, "files": [*original["files"], {"path": "src/new.py", "mode": "create", "reason": "New"}],
               "changes": {**original["changes"], "src/new.py": "Create"}}
    ok, reason = within_approved_scope(original, revised)
    assert not ok
    assert "outside the approved scope" in reason
    assert not within_approved_scope(original, {**original, "constraints": []})[0]
    assert not within_approved_scope(original, {**original, "budget": {"max_tool_calls": 200}})[0]


def test_split_into_same_file_packages_stays_in_scope() -> None:
    original = package()
    first = {**original, "id": "wp-1a", "budget": {"max_tool_calls": 10}}
    second = {**original, "id": "wp-1b", "budget": {"max_tool_calls": 10}}
    ok, reason = within_approved_scope_split(original, [first, second])
    assert ok
    assert reason is None


def test_split_touching_new_file_or_raising_budget_requires_approval() -> None:
    original = package()
    with_new_file = {
        **original,
        "files": [*original["files"], {"path": "src/new.py", "mode": "create", "reason": "New"}],
        "changes": {**original["changes"], "src/new.py": "Create"},
    }
    ok, reason = within_approved_scope_split(original, [with_new_file])
    assert not ok
    assert "outside the approved scope" in reason
    over_budget = {**original, "budget": {"max_tool_calls": 21}}
    ok, reason = within_approved_scope_split(original, [over_budget])
    assert not ok
    assert "more tool calls" in reason
    ok, reason = within_approved_scope_split(original, [])
    assert not ok
    assert reason == "no packages were proposed"


def test_split_with_unknown_field_names_is_rejected_with_a_clear_reason() -> None:
    # Regression: an escalation model that invents field names (e.g. from a
    # different planning vocabulary) must not silently fail to auto-apply —
    # the rejection reason must say *why*, not just "not automatic".
    original = package()
    first = {
        **original, "id": "wp-1a", "budget": {"max_tool_calls": 10},
        "depends_on": [], "deliverable": "does the thing", "interface_contracts": ["x"],
    }
    ok, reason = within_approved_scope_split(original, [first])
    assert not ok
    assert "do not match the work package schema" in reason
