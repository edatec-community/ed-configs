from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from .errors import ConfigError


_IDENTIFIER = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.+-]*")
_RELEASE = re.compile(r"([0-9]+)\.([0-9]+)\.([0-9]+)")
OVERLAY_DIRECTORY = "/boot/firmware/overlays"
DTC = "/usr/bin/dtc"

KernelVersion = tuple[int, int, int]


def identifier(value: Any) -> bool:
    return isinstance(value, str) and _IDENTIFIER.fullmatch(value) is not None


def kernel_version(value: str, *, running: bool = False) -> KernelVersion:
    match = _RELEASE.match(value) if running else _RELEASE.fullmatch(value)
    if match is None or (running and value[match.end():].startswith("-rc")):
        raise ConfigError(f"unsupported kernel release for overlay selection: {value!r}")
    major, minor, patch = (int(part) for part in match.groups())
    return major, minor, patch


def declared_version(value: Any) -> KernelVersion:
    if not isinstance(value, str):
        raise ConfigError(f"overlay kernel version must be a string: {value!r}")
    return kernel_version(value)


class KernelCapabilities:
    def __init__(self, document: dict[str, Any]):
        if set(document) != {"version", "baseline", "releases"} or document["version"] != 1:
            raise ConfigError("invalid kernel-capabilities.toml format")
        self.baseline = declared_version(document["baseline"])
        releases = document["releases"]
        if not isinstance(releases, dict):
            raise ConfigError("kernel capabilities releases must be a table")
        self.releases: dict[KernelVersion, dict[str, set[str]]] = {}
        known: dict[str, set[str]] = {}
        for version, table in sorted(releases.items(), key=lambda item: declared_version(item[0])):
            release = declared_version(version)
            if release < self.baseline or release in self.releases:
                raise ConfigError(f"unexpected kernel capabilities version {version}")
            if not isinstance(table, dict) or set(table) != {"overlays"}:
                raise ConfigError(f"kernel {version} must declare an overlays table")
            overlays = table["overlays"]
            if not isinstance(overlays, dict):
                raise ConfigError(f"kernel {version} overlays must be a table")
            increment = {}
            for name, values in overlays.items():
                if not identifier(name) or not isinstance(values, list) or not all(
                    identifier(value) for value in values
                ):
                    raise ConfigError(f"invalid overlay capabilities for {name} in kernel {version}")
                parameters = set(values)
                if len(parameters) != len(values) or (
                    name in known and (not parameters or bool(parameters & known[name]))
                ):
                    raise ConfigError(f"duplicate overlay capabilities for {name} in kernel {version}")
                known.setdefault(name, set()).update(parameters)
                increment[name] = parameters
            self.releases[release] = increment
        if self.baseline not in self.releases:
            raise ConfigError("kernel capabilities baseline is missing from releases")

    def available(self, version: KernelVersion) -> dict[str, set[str]]:
        available: dict[str, set[str]] = {}
        for release, overlays in self.releases.items():
            if release > version:
                break
            for name, parameters in overlays.items():
                available.setdefault(name, set()).update(parameters)
        return available


def overlay_request(line: str) -> tuple[str, set[str]] | None:
    value = line.partition("#")[0].strip()
    if not value.startswith("dtoverlay="):
        return None
    name, *options = value.removeprefix("dtoverlay=").split(",")
    if not name and not options:  # End the current overlay's parameter scope.
        return None
    parameters = {option.partition("=")[0].strip() for option in options}
    name = name.strip()
    if not identifier(name) or not all(identifier(parameter) for parameter in parameters):
        raise ConfigError(f"invalid overlay configuration: {line}")
    return name, parameters


def missing_capabilities(name: str, parameters: set[str], available: dict[str, set[str]]) -> str:
    if name not in available:
        return f"overlay {name} is not declared for this kernel"
    missing = parameters - available[name]
    if missing:
        return f"overlay {name} lacks parameters: {', '.join(sorted(missing))}"
    return ""


def compile_overlay(source: Path) -> bytes:
    with tempfile.TemporaryDirectory(prefix="ed-config-dtbo-") as directory:
        output = Path(directory) / "overlay.dtbo"
        try:
            result = subprocess.run(
                [DTC, "-@", "-I", "dts", "-O", "dtb", "-o", str(output), str(source)],
                capture_output=True, text=True, timeout=30, check=False,
            )
            if result.returncode:
                raise ConfigError(f"cannot compile {source.name}: {result.stderr.strip()}")
            return output.read_bytes()
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ConfigError(f"cannot compile {source.name}: {exc}") from exc
