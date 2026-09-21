#!/usr/bin/env python3
from pathlib import Path

from edconfig.cli import main


if __name__ == "__main__":
    raise SystemExit(main(program_dir=Path(__file__).resolve().parent))

