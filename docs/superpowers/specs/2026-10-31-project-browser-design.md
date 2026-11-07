# Project browser

Approved in conversation: Project immediately after Board, select a project and browse its local working directory in a folder tree, including uncommitted changes. Read only. Exclude dotfiles/dotdirectories and Git-ignored paths. Render Markdown and Mermaid; display source text with line numbers. Follow relative Markdown links and Kor `path::symbol` references within the reader, jumping to a symbol when found.

## Approaches
Discussed a flat Markdown list versus a folder tree. The request to follow Kor references into source files establishes a general file tree and text reader as the implementation approach.

## Implementation decisions
Reuse react-markdown and existing app styles/authentication. Add Mermaid as a lazily loaded local dependency. Use Git's tracked/untracked inventory and ignore rules, reading current disk content rather than commits. Never serve paths outside the project, hidden/ignored files, symlinks, or special files. Binary files and text larger than 2 MiB show an explicit unsupported message. No edits, downloads, or execution of project HTML/scripts. Project selection starts with an explicit placeholder; no automatic project/file selection. Refresh reloads the tree and current file. External HTTP(S) links open a new tab.
