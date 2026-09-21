from __future__ import annotations

import fcntl
import json
import os
import shutil
import stat
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .catalog import Catalog
from .errors import ApplyError, ConfigError
from .screen import operations as screen_operations


BEGIN = "# BEGIN EDATEC MANAGED CONFIG"
END = "# END EDATEC MANAGED CONFIG"
UBUNTU_DISABLED = {"dtparam=i2c_vc=on", "dtparam=i2c_arm=on", "dtparam=spi=on"}


def rooted(root: Path, absolute: str) -> Path:
    path = Path(absolute)
    if not path.is_absolute() or ".." in path.parts:
        raise ConfigError(f"managed path must be absolute and normalized: {absolute!r}")
    return root.joinpath(*path.parts[1:])


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ApplyError(f"cannot read {path}: {exc}") from exc


def _select_config(root: Path, requested: str | None) -> Path:
    if requested:
        return rooted(root, requested) if Path(requested).is_absolute() else root / requested
    firmware = root / "boot/firmware/config.txt"
    legacy = root / "boot/config.txt"
    if firmware.is_file() or firmware.parent.is_dir():
        return firmware
    if legacy.is_file() or legacy.parent.is_dir():
        return legacy
    raise ApplyError("cannot locate config.txt; create /boot/firmware or /boot, or pass --config-file")


def _detect_ubuntu(root: Path) -> bool:
    content = _read(root / "etc/os-release") or ""
    values: dict[str, str] = {}
    for line in content.splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"')
    return values.get("ID", "").casefold() == "ubuntu"


def _without_managed_block(lines: list[str]) -> list[str]:
    result: list[str] = []
    inside = False
    found_begin = False
    for line in lines:
        if line == BEGIN:
            if inside or found_begin:
                raise ApplyError("config.txt contains duplicate EDATEC managed blocks")
            inside = True
            found_begin = True
            continue
        if line == END:
            if not inside:
                raise ApplyError("config.txt contains an unmatched EDATEC managed block marker")
            inside = False
            continue
        if not inside:
            result.append(line)
    if inside:
        raise ApplyError("config.txt contains an unterminated EDATEC managed block")
    return result


def _config_content(current: str | None, desired: list[str], known: set[str]) -> str:
    lines = _without_managed_block((current or "").splitlines())
    # Migrate exact lines written by older ed-tools/ed-config versions into the
    # managed block, while retaining all unrelated customer configuration.
    lines = [line for line in lines if line not in known]
    while lines and not lines[-1].strip():
        lines.pop()
    if lines:
        lines.append("")
    lines.extend([BEGIN, *desired, END])
    return "\n".join(lines) + "\n"


def _release_content(current: str | None, code: int) -> str:
    replacement = f"PRODUCT=0x{code:08x}"
    result: list[str] = []
    replaced = False
    for line in (current or "").splitlines():
        if line.startswith("PRODUCT="):
            if not replaced:
                result.append(replacement)
                replaced = True
        else:
            result.append(line)
    if not replaced:
        result.append(replacement)
    return "\n".join(result) + "\n"


def _rule_content(rule: dict[str, Any], ubuntu: bool) -> str:
    content = rule.get("ubuntu_content") if ubuntu else rule.get("default_content")
    content = content if content is not None else rule.get("content")
    if not isinstance(content, str):
        raise ConfigError(f"udev rule {rule.get('path')} has no content for this OS")
    return content if content.endswith("\n") else content + "\n"


def build_plan(
    catalog: Catalog,
    resolution: dict[str, Any],
    root: Path,
    config_file: str | None = None,
    os_name: str = "auto",
) -> dict[str, Any]:
    root = root.resolve()
    if os_name not in {"auto", "ubuntu", "raspberrypi"}:
        raise ConfigError(f"unsupported OS selection: {os_name}")
    ubuntu = _detect_ubuntu(root) if os_name == "auto" else os_name == "ubuntu"
    config_path = _select_config(root, config_file)
    desired = [item["value"] for item in resolution["config_lines"]]
    if ubuntu and resolution["disable_config_on_ubuntu"]:
        desired = [line for line in desired if line not in UBUNTU_DISABLED]

    operations: list[dict[str, Any]] = []

    def write_operation(
        kind: str,
        path: Path,
        content: str,
        mode: int | None = None,
        uid: int | None = None,
        gid: int | None = None,
    ) -> None:
        current = _read(path)
        operations.append({
            "kind": kind,
            "action": "write",
            "path": "/" + path.relative_to(root).as_posix(),
            "changed": current != content,
            "_target": path,
            "_content": content,
            "_original": current,
            "_mode": mode,
            "_uid": uid,
            "_gid": gid,
        })

    config_content = _config_content(_read(config_path), desired, catalog.known_config_lines())
    write_operation("config", config_path, config_content)

    selected_rules: set[str] = set()
    for rule in resolution["rules"]:
        selected_rules.add(rule["path"])
        write_operation("udev", rooted(root, rule["path"]), _rule_content(rule, ubuntu), 0o644)
    for path in sorted(catalog.known_rule_paths() - selected_rules):
        target = rooted(root, path)
        operations.append({
            "kind": "udev",
            "action": "delete",
            "path": path,
            "changed": target.exists(),
            "_target": target,
            "_original": _read(target),
        })

    if resolution.get("screen_config") == "h101c":
        for operation in screen_operations(root, resolution["identity"]["platform"]):
            write_operation(
                str(operation["kind"]),
                operation["path"],
                str(operation["content"]),
                operation.get("mode"),
                operation.get("uid"),
                operation.get("gid"),
            )

    release = root / "etc/ed-release"
    write_operation("release", release, _release_content(_read(release), resolution["product_code"]), 0o644)

    return {
        "format": "ed-config-plan",
        "schema_version": 1,
        "root": str(root),
        "os": "ubuntu" if ubuntu else "raspberrypi",
        "profile": resolution["profile"],
        "model": resolution["model"],
        "options": resolution["options"],
        "product_code": resolution["product_code"],
        "reboot_required": any(op["changed"] and op["kind"] in {"config", "udev"} for op in operations),
        "operations": operations,
        "_resolution": resolution,
    }


def public_plan(plan: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in plan.items()
        if not key.startswith("_") and key != "operations"
    } | {
        "product_code_hex": f"0x{plan['product_code']:08x}",
        "operations": [
            {key: value for key, value in operation.items() if not key.startswith("_")}
            for operation in plan["operations"]
        ],
    }


def _atomic_write(
    path: Path,
    content: str,
    mode: int | None,
    uid: int | None = None,
    gid: int | None = None,
) -> None:
    missing: list[Path] = []
    parent = path.parent
    while not parent.exists():
        missing.append(parent)
        parent = parent.parent
    path.parent.mkdir(parents=True, exist_ok=True)
    if uid is not None or gid is not None:
        for directory in reversed(missing):
            os.chown(directory, uid if uid is not None else -1, gid if gid is not None else -1)
    previous = path.stat() if path.exists() else None
    previous_mode = stat.S_IMODE(previous.st_mode) if previous else mode
    owner_uid = previous.st_uid if previous else uid
    owner_gid = previous.st_gid if previous else gid
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if previous_mode is not None:
            os.chmod(temporary, previous_mode)
        if owner_uid is not None or owner_gid is not None:
            os.chown(temporary, owner_uid if owner_uid is not None else -1, owner_gid if owner_gid is not None else -1)
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


@contextmanager
def _lock(root: Path) -> Iterator[None]:
    path = root / "run/lock/ed-config.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("a+", encoding="utf-8") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield
    except BlockingIOError as exc:
        raise ApplyError("another ed-config process is already applying configuration") from exc


def _backup(root: Path, operations: list[dict[str, Any]]) -> Path | None:
    changed = [op for op in operations if op["changed"] and op["_target"].exists()]
    if not changed:
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    directory = root / "var/lib/ed-config/backups" / stamp
    for operation in changed:
        source: Path = operation["_target"]
        destination = directory / source.relative_to(root)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    return directory


def apply_plan(plan: dict[str, Any]) -> dict[str, Any]:
    root = Path(plan["root"])
    with _lock(root):
        _backup(root, plan["operations"])
        # ed-release is the commit marker consumed by the separate EEPROM
        # writer, so it is deliberately applied after config and udev files.
        ordered = sorted(plan["operations"], key=lambda op: op["kind"] == "release")
        completed: list[dict[str, Any]] = []
        try:
            for operation in ordered:
                if not operation["changed"]:
                    continue
                target: Path = operation["_target"]
                if operation["action"] == "delete":
                    target.unlink(missing_ok=True)
                else:
                    _atomic_write(
                        target,
                        operation["_content"],
                        operation.get("_mode"),
                        operation.get("_uid"),
                        operation.get("_gid"),
                    )
                completed.append(operation)
        except OSError as exc:
            rollback_errors: list[str] = []
            for applied in reversed(completed):
                target = applied["_target"]
                original = applied.get("_original")
                try:
                    if original is None:
                        target.unlink(missing_ok=True)
                    else:
                        _atomic_write(
                            target,
                            original,
                            applied.get("_mode"),
                            applied.get("_uid"),
                            applied.get("_gid"),
                        )
                except OSError as rollback_error:
                    rollback_errors.append(f"{applied['path']}: {rollback_error}")
            message = f"failed to apply {operation['path']}: {exc}"
            if rollback_errors:
                message += "; rollback also failed: " + "; ".join(rollback_errors)
            raise ApplyError(message) from exc

        attempted_at = datetime.now(timezone.utc).isoformat()
        state = {
            "format": "ed-config-state",
            "schema_version": 2,
            "configured": True,
            "current": {
                "model": plan["model"],
                "options": plan["options"],
                "product_code": plan["product_code"],
                "applied_at": attempted_at,
                "reboot_required": plan["reboot_required"],
                "catalog_digest": plan["_resolution"]["catalog_digest"],
                "managed_rule_files": sorted(
                    op["path"] for op in plan["operations"]
                    if op["kind"] == "udev" and op["action"] == "write"
                ),
            },
            "last_apply": {
                "success": True,
                "requested_model": plan["model"],
                "attempted_at": attempted_at,
                "error": None,
            },
        }
        state_path = root / "var/lib/ed-config/state.json"
        _atomic_write(state_path, json.dumps(state, indent=2, ensure_ascii=False) + "\n", 0o644)
        return {
            "success": True,
            "changed": any(op["changed"] for op in plan["operations"]),
            "model": plan["model"],
            "options": plan["options"],
            "product_code": plan["product_code"],
            "product_code_hex": f"0x{plan['product_code']:08x}",
            "changed_files": [op["path"] for op in plan["operations"] if op["changed"]],
            "reboot_required": plan["reboot_required"],
        }


def read_status(root: Path) -> dict[str, Any]:
    path = root.resolve() / "var/lib/ed-config/state.json"
    content = _read(path)
    if content is None:
        return {
            "format": "ed-config-status",
            "schema_version": 1,
            "configured": False,
            "current": None,
            "last_apply": None,
        }
    try:
        state = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"invalid status file {path}: {exc}") from exc
    if not isinstance(state, dict):
        raise ConfigError(f"invalid status file {path}: expected a JSON object")

    # One-time compatibility with state written by the initial implementation.
    if state.get("schema_version") == 1 and "model" in state:
        applied_at = state.get("applied_at")
        state = {
            "configured": True,
            "current": {
                "model": state.get("model"),
                "options": state.get("options", []),
                "product_code": state.get("product_code"),
                "applied_at": applied_at,
                "reboot_required": False,
            },
            "last_apply": {
                "success": True,
                "requested_model": state.get("model"),
                "attempted_at": applied_at,
                "error": None,
            },
        }
    configured = bool(state.get("configured", False))
    current = state.get("current") if configured else None
    last_apply = state.get("last_apply")
    if current is not None and not isinstance(current, dict):
        raise ConfigError(f"invalid status file {path}: current must be an object")
    if last_apply is not None and not isinstance(last_apply, dict):
        raise ConfigError(f"invalid status file {path}: last_apply must be an object")
    return {
        "format": "ed-config-status",
        "schema_version": 1,
        "configured": configured,
        "current": current,
        "last_apply": last_apply,
    }


def record_apply_failure(root: Path, requested_model: str, error: str) -> None:
    root = root.resolve()
    try:
        status = read_status(root)
        state = {
            "format": "ed-config-state",
            "schema_version": 2,
            "configured": status["configured"],
            "current": status["current"],
            "last_apply": {
                "success": False,
                "requested_model": requested_model,
                "attempted_at": datetime.now(timezone.utc).isoformat(),
                "error": error,
            },
        }
        path = root / "var/lib/ed-config/state.json"
        _atomic_write(path, json.dumps(state, indent=2, ensure_ascii=False) + "\n", 0o644)
    except (ConfigError, OSError):
        # Failure reporting must never replace the original apply error.
        return
