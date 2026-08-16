"""Worker entrypoint is wired after the concrete task runner is configured."""


def main() -> None:
    raise SystemExit("Configure CARLO profiles and worktree root before starting worker")


if __name__ == "__main__":
    main()
