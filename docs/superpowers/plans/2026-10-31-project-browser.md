# Project browser implementation plan

> **For agentic workers:** Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Read project documentation and follow it to current source code.
**Architecture:** Authenticated API exposes a filtered file inventory and bounded UTF-8 text reads. A Project view owns selection, tree, file navigation and rendering.
**Tech Stack:** FastAPI, Python/Git, React/TypeScript, react-markdown, Mermaid.
**Spec:** docs/superpowers/specs/2026-10-31-project-browser-design.md

## Constraints and review focus
Read only; hidden and ignored files excluded even through links. Test traversal, symlinks, deleted files, binary/oversized content, stale asynchronous results and broken Mermaid. Preserve existing workspace changes.

### Task 1: File API
- [x] Write and run failing tests for tracked/modified/untracked files, hidden and ignored files, traversal/symlinks and unsupported content.
- [x] Implement `project_files.py::list_files(root)` and `read_file(root, path)`, plus authenticated GET `/api/projects/{project_id}/files` and `/file?path=...` in api.py.
- [x] Run the focused backend tests.

### Task 2: Reader
- [x] Write and run failing tests for explicit project selection, tree navigation, relative links, Kor symbol jumps and Mermaid failures.
- [x] Add API types/calls, ProjectView.tsx, lazily loaded MermaidDiagram.tsx and styles; insert Project after Board.
- [x] Run frontend tests and build; update semantic code indexes.

### Task 3: Verify
- [x] Review boundary handling, navigation and pending requests; run appropriate backend regression tests, frontend suite and build, and diff whitespace checks.

## Execution record
User authorized development after choosing exclusions. Execute inline without another approval round. Existing workspace contains unrelated changes; no blanket commits or resets.

Verified: backend suite 344 passed (requires local PostgreSQL access); frontend suite 47 passed; production build passed; Chrome headless rendered an actual Mermaid SVG and navigated a qualified Kor reference to the correct method. Independent review found and prompted correction of qualified symbol collisions, covered by a regression test. Medium desktop widths place navigation below the masthead to prevent overlap with runtime indicators after adding the fifth tab. npm audit findings belong to existing Vite/Vitest/jsdom dependencies; no Mermaid advisory reported. No deployment performed.
