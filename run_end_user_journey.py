"""Compatibility entry point for the isolated Studio/process/browser journey."""
from pathlib import Path
import subprocess
import sys

if __name__ == "__main__":
    script = Path(__file__).resolve().parent / "tests" / "run_studio_journey.py"
    raise SystemExit(subprocess.call([sys.executable, str(script), *sys.argv[1:]]))
