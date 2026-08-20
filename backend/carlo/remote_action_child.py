import json
import os
import signal
import subprocess
import sys
from pathlib import Path


def main() -> None:
    runtime = Path(sys.argv[1])
    workspace = Path(sys.argv[2])
    values = json.loads(Path(sys.argv[3]).read_text())
    command = json.loads(sys.argv[4])
    os.setsid()
    signal.signal(signal.SIGTERM, lambda *_: None)
    (runtime / "process-group").write_text(str(os.getpgrp()))
    allowed = ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "TERM")
    environment = {name: os.environ[name] for name in allowed if name in os.environ}
    environment.update(values)
    with (runtime / "raw.log").open("wb") as output:
        try:
            process = subprocess.Popen(
                command,
                cwd=workspace,
                env=environment,
                stdout=output,
                stderr=subprocess.STDOUT,
            )
            exit_code = process.wait()
        except FileNotFoundError:
            output.write(f"[CARLO] executable not found: {command[0]}\n".encode())
            exit_code = 127
    temporary = runtime / "state.tmp"
    temporary.write_text(json.dumps({"exit_code": exit_code}))
    os.replace(temporary, runtime / "state")


if __name__ == "__main__":
    main()
