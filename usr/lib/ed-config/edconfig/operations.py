from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

from .errors import ConfigError


def parse_family_commands(document: dict[str, Any], path: Path) -> list[str]:
    if set(document) != {"commands"}:
        raise ConfigError(f"{path}: expected only a commands array")
    commands = document["commands"]
    if (
        not isinstance(commands, list)
        or not commands
        or any(not isinstance(command, str) or not command.strip() for command in commands)
    ):
        raise ConfigError(f"{path}: commands must be a non-empty array of command strings")
    if len(set(commands)) != len(commands):
        raise ConfigError(f"{path}: commands must not contain duplicates")
    return list(commands)


def commands_digest(commands: list[str], scope: str) -> str:
    payload = json.dumps(
        {"scope": scope, "commands": commands},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def run_family_commands(commands: list[str], root: Path) -> None:
    root = root.resolve()
    for index, command in enumerate(commands, start=1):
        try:
            if root == Path("/"):
                subprocess.run(command, shell=True, executable="/bin/sh", check=True)
            else:
                subprocess.run(
                    ["chroot", str(root), "/bin/sh", "-c", command],
                    check=True,
                )
        except subprocess.CalledProcessError as exc:
            raise OSError(
                f"family command {index} failed with exit status {exc.returncode}"
            ) from exc
