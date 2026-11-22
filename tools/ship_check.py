"""Run the essential tests and lint locally or in CI."""

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    # Older scripts supply a base ref or --full. Every change now runs the same small suite.
    parser.add_argument("base", nargs="?", help=argparse.SUPPRESS)
    parser.add_argument("--full", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--list", action="store_true", help="show selected tests without running them"
    )
    args = parser.parse_args()
    env = dict(os.environ, TEAM_API_KEY="test-only", OPENAI_API_KEY="test-only")
    pytest = [sys.executable, "-m", "pytest", "-q"]
    if args.list:
        subprocess.run([*pytest, "--collect-only"], env=env, cwd=ROOT, check=True)
        return
    subprocess.run(pytest, env=env, cwd=ROOT, check=True)
    subprocess.run(
        [sys.executable, "-m", "ruff", "check", "app", "tests", "tools"], cwd=ROOT, check=True
    )


if __name__ == "__main__":
    main()
