"""Run the repository's synthetic Python and browser-logic regression checks."""
from pathlib import Path
import os
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]


def main():
    commands = [[sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"]]
    commands.extend(["node", str(path)] for path in sorted((ROOT / "tests").glob("test_*.js")))
    with tempfile.TemporaryDirectory(prefix="job-companion-check-") as data:
        environment = dict(os.environ, JOB_COMPANION_DATA=data,
                           JOB_COMPANION_PUBLIC_ORIGIN="", JOB_COMPANION_LOCAL_PEERS="")
        for command in commands:
            print("Running:", " ".join(command), flush=True)
            result = subprocess.run(command, cwd=ROOT, env=environment)
            if result.returncode:
                return result.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
