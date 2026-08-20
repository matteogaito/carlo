import json
import os
import signal
import subprocess
import sys
from pathlib import Path

from .actions import minimal_environment, parse_dotenv


def main() -> None:
    runtime = Path(sys.argv[1])
    workspace = Path(sys.argv[2])
    env_file = Path(sys.argv[3]) if sys.argv[3] else None
    command = json.loads(sys.argv[4])
    runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
    runtime.chmod(0o700)
    signal.signal(signal.SIGTERM, lambda *_: None)
    values = parse_dotenv(env_file.read_text()) if env_file else {}
    with (runtime / "raw.log").open("wb") as output:
        try:
            process = subprocess.Popen(
                command,
                cwd=workspace,
                env=minimal_environment(values),
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
