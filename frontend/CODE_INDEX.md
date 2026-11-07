# Frontend code index

## Project files and documentation

Project reads the selected project's working copy through a folder tree. Markdown links and Kor references open local source; Mermaid diagrams render in an isolated preview.

- Selection, navigation, symbol lookup and tree: `src/ProjectView.tsx::ProjectView`, `src/ProjectView.tsx::ProjectReader`
- Mermaid rendering and error handling: `src/MermaidDiagram.tsx::MermaidDiagram`
- Read-only file API: `src/api.ts::httpApi`

## Technical planning progress

The task detail view shows live technical planning events and the time since Pi last reported activity.

- Entry point: `src/App.tsx::TaskDetail`
- Event labels: `src/App.tsx::planningEventLabel`
- Elapsed time: `src/App.tsx::formatElapsed`

## Technical slices

The task plan keeps the active execution package in `implementation_tasks` and the ordered full set in `technical_packages`. The detail view lists the full set and marks the current slice.

- Plan rendering: `src/App.tsx::StructuredPlan`
- Metadata shape: `src/api.ts::Plan`

## Technical replan action

The child task Actions menu uses `technical_replan_allowed` from the approved revision, so a newer unapproved revision does not hide the action. The API action queues planning and archives unverified checkout changes before the new plan runs.

- Button: `src/App.tsx::TaskDetail`
- API call: `src/api.ts::replanTechnicalTask`

## Agent context limits

The Coding agents section shows a compact model default or override context window. Opening it offers power-of-two caps that fit the selected model's reported window and output limit. The cap is saved through the existing agent profile API.

- Controls and display: `src/SettingsView.tsx::AgentProfiles`
- API shape: `src/api.ts::AgentProfileSettings`
