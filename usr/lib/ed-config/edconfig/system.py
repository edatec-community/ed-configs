from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import stat
import subprocess
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
UBUNTU_DISABLED = {"dtparam=i2c_arm=on", "dtparam=spi=on"}
WIREPLUMBER_SOURCE = "/usr/share/wireplumber/main.lua.d/50-alsa-config.lua"
WIREPLUMBER_CONFIG = "/etc/wireplumber/main.lua.d/50-alsa-config.lua"
WIREPLUMBER_MODERN_CONFIG = "/etc/wireplumber/wireplumber.conf.d/51-ed-config-rt5616.conf"
ASOUND_CONFIG = "/etc/asound.conf"
ALSA_STATE = "/var/lib/alsa/asound.state"
DPKG = "/usr/bin/dpkg"
DPKG_DEB = "/usr/bin/dpkg-deb"
DPKG_QUERY = "/usr/bin/dpkg-query"


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


def _catalog_asset_contents(catalog: Catalog, field: str) -> set[str]:
    contents: set[str] = set()
    for family_id, family in catalog.families.items():
        raw = family.get(field)
        if not isinstance(raw, str):
            continue
        path = catalog.family_directories[family_id.casefold()] / raw
        try:
            contents.add(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise ConfigError(f"cannot read {field} for family {family_id}: {exc}") from exc
    return contents


def _catalog_field_values(catalog: Catalog, field: str) -> set[str]:
    return {
        str(value)
        for family in catalog.families.values()
        if isinstance((value := family.get(field)), str)
    }


def _catalog_rule_contents(catalog: Catalog) -> set[str]:
    contents: set[str] = set()
    for family_id, family in catalog.families.items():
        package_dir = catalog.family_directories[family_id.casefold()]
        for rule in family.get("udev_rules", []):
            for field in ("content_file", "default_file", "ubuntu_file"):
                raw = rule.get(field)
                if not isinstance(raw, str):
                    continue
                try:
                    contents.add((package_dir / raw).read_text(encoding="utf-8"))
                except OSError as exc:
                    raise ConfigError(f"cannot read udev rule {raw} for family {family_id}: {exc}") from exc
    return contents


def _wireplumber_content(root: Path, target: Path, audio_format: str) -> str | None:
    """Reproduce the DEB's copy-and-edit of the distro Lua configuration."""
    source = rooted(root, WIREPLUMBER_SOURCE)
    source_content = _read(source)
    target_content = _read(target)
    content = source_content if source_content is not None else target_content
    if content is None:
        # The shell postinst's cp/grep/sed sequence leaves no file when the
        # distro source and previous target are both absent.
        return None

    lines = content.splitlines()
    match_index: int | None = None
    commented = False
    for index, line in enumerate(lines):
        if "audio.format" not in line or not line:
            continue
        match_index = index
        commented = "--" in line
        if commented:
            break
    if match_index is not None:
        line = lines[match_index]
        if commented:
            line = line.replace("--", "  ")
        lines[match_index] = re.sub(r'= ".*"', f'= "{audio_format}"', line)
    else:
        insertion: int | None = None
        for index, line in enumerate(lines):
            if "apply_properties = {" in line:
                insertion = index + 1
        if insertion is not None:
            lines.insert(
                insertion,
                f'        ["audio.format"]           = "{audio_format}",',
            )
    return "\n".join(lines) + "\n"


def _modern_wireplumber_content(audio_format: str) -> str:
    """Configure the Pulse-compatible RT5616 node on WirePlumber 0.5+."""
    return (
        "# Managed by ed-config: Debian 13 compatibility for the 3020 audio package.\n"
        "monitor.alsa.rules = [\n"
        "  {\n"
        "    matches = [\n"
        "      {\n"
        "        node.name = \"~alsa_output.*\"\n"
        "        alsa.card_name = \"rt5616-codec\"\n"
        "      }\n"
        "    ]\n"
        "    actions = {\n"
        "      update-props = {\n"
        "        priority.session = 1200\n"
        f'        audio.format = "{audio_format}"\n'
        "      }\n"
        "    }\n"
        "  }\n"
        "]\n"
    )


def _deb_field(path: Path, field: str) -> str:
    try:
        result = subprocess.run(
            [DPKG_DEB, "-f", str(path), field],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ApplyError(f"cannot inspect local package {path}: {exc}") from exc
    value = result.stdout.strip()
    if not value:
        raise ApplyError(f"local package {path} has no {field} metadata")
    return value


def _package_missing(path: Path) -> bool:
    name = _deb_field(path, "Package")
    version = _deb_field(path, "Version")
    try:
        result = subprocess.run(
            [DPKG_QUERY, "-W", "-f=" + "${Status} ${Version}", name],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise ApplyError(f"cannot inspect installed package {name}: {exc}") from exc
    return result.returncode != 0 or result.stdout.strip() != f"install ok installed {version}"


def _install_local_packages(paths: list[str]) -> None:
    try:
        subprocess.run(
            [DPKG, "--install", *paths], check=True, capture_output=True, text=True
        )
    except OSError as exc:
        raise ApplyError(f"cannot run local package installer: {exc}") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        raise ApplyError(
            "local audio package installation failed; no network dependency repair was attempted"
            + (f": {detail}" if detail else "")
        ) from exc


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

    # The Pulse ALSA bridge is shipped with the image as an offline asset.  On
    # the live root, install it only when the exact catalog versions are absent;
    # never invoke apt or reach the network.  Image-root plans remain
    # declarative because dpkg must run in the target root's own environment.
    package_paths = [Path(item) for item in resolution.get("audio_packages", [])]
    if root == Path("/") and package_paths:
        missing = [path for path in package_paths if _package_missing(path)]
        if missing:
            operations.append({
                "kind": "packages",
                "action": "install",
                "path": "/var/lib/ed-config/packages/audio",
                "changed": True,
                "_packages": [str(path) for path in missing],
            })

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
    managed_rule_contents = _catalog_rule_contents(catalog)
    for rule in resolution["rules"]:
        selected_rules.add(rule["path"])
        target = rooted(root, rule["path"])
        current = _read(target)
        if current is None or current in managed_rule_contents:
            write_operation("udev", target, _rule_content(rule, ubuntu), 0o644)
    for path in sorted(catalog.known_rule_paths() - selected_rules):
        target = rooted(root, path)
        if _read(target) in managed_rule_contents:
            operations.append({
                "kind": "udev",
                "action": "delete",
                "path": path,
                "changed": True,
                "_target": target,
                "_original": _read(target),
            })

    if catalog.has_wireplumber_config():
        wp_format = resolution.get("wireplumber_audio_format")
        wp_source = rooted(root, WIREPLUMBER_SOURCE)
        managed_legacy_wireplumber: set[str] = set()
        if wp_source.is_file():
            for audio_format in _catalog_field_values(catalog, "wireplumber_audio_format"):
                content = _wireplumber_content(
                    root, rooted(root, WIREPLUMBER_CONFIG), audio_format
                )
                if content is not None:
                    managed_legacy_wireplumber.add(content)
        if wp_source.is_file():
            # Debian 12/WirePlumber 0.4 compatibility for the old DEB image.
            wp_target = rooted(root, WIREPLUMBER_CONFIG)
            wp_content = (
                _wireplumber_content(root, wp_target, str(wp_format))
                if wp_format is not None
                else None
            )
        else:
            # Debian 13/WirePlumber 0.5 no longer ships the old Lua path.
            wp_target = rooted(root, WIREPLUMBER_MODERN_CONFIG)
            wp_content = (
                str(resolution.get("wireplumber_modern"))
                if resolution.get("wireplumber_modern") is not None
                else _modern_wireplumber_content(str(wp_format))
                if wp_format is not None
                else None
            )
        # Remove stale files from the earlier experimental hardware profile.
        if wp_content is not None:
            current_wp = _read(wp_target)
            managed_wp = (
                managed_legacy_wireplumber
                if wp_target == rooted(root, WIREPLUMBER_CONFIG)
                else _catalog_asset_contents(catalog, "wireplumber_modern_file")
            )
            if current_wp is None or current_wp in managed_wp:
                write_operation("wireplumber", wp_target, str(wp_content), 0o644)
        if wp_target != rooted(root, WIREPLUMBER_MODERN_CONFIG):
            modern_target = rooted(root, WIREPLUMBER_MODERN_CONFIG)
            if _read(modern_target) in _catalog_asset_contents(catalog, "wireplumber_modern_file"):
                operations.append({
                    "kind": "wireplumber",
                    "action": "delete",
                    "path": WIREPLUMBER_MODERN_CONFIG,
                    "changed": True,
                    "_target": modern_target,
                    "_original": _read(modern_target),
                })
        elif wp_content is None and wp_target == rooted(root, WIREPLUMBER_MODERN_CONFIG) and _read(wp_target) in _catalog_asset_contents(catalog, "wireplumber_modern_file"):
            operations.append({
                "kind": "wireplumber",
                "action": "delete",
                "path": WIREPLUMBER_MODERN_CONFIG,
                "changed": True,
                "_target": wp_target,
                "_original": _read(wp_target),
            })
        # The old postrm did not remove this copied WirePlumber file when the
        # package was removed or another model was selected.

    if catalog.has_alsa_config():
        asound_target = rooted(root, ASOUND_CONFIG)
        asound_content = resolution.get("alsa_config")
        managed_asound = _catalog_asset_contents(catalog, "alsa_config_file")
        current_asound = _read(asound_target)
        if asound_content:
            if current_asound is None or current_asound in managed_asound:
                write_operation("alsa_config", asound_target, str(asound_content), 0o644)
        elif current_asound in managed_asound:
            operations.append({
                "kind": "alsa_config",
                "action": "delete",
                "path": ASOUND_CONFIG,
                "changed": True,
                "_target": asound_target,
                "_original": _read(asound_target),
            })

    if catalog.has_alsa_config():
        state_target = rooted(root, ALSA_STATE)
        state_content = resolution.get("alsa_state")
        managed_state = _catalog_asset_contents(catalog, "alsa_state_file")
        current_state = _read(state_target)
        if state_content:
            if current_state is None or current_state in managed_state:
                write_operation("alsa", state_target, str(state_content), 0o644)
        elif current_state in managed_state:
            operations.append({
                "kind": "alsa",
                "action": "delete",
                "path": ALSA_STATE,
                "changed": True,
                "_target": state_target,
                "_original": _read(state_target),
            })

    for operation in screen_operations(
        root, resolution["identity"]["platform"], resolution.get("screen_config")
    ):
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
        "model": resolution["model"],
        "options": resolution["options"],
        "product_code": resolution["product_code"],
        "reboot_required": any(
            op["changed"] and op["kind"] in {
                "config", "udev", "wireplumber", "alsa_profile", "alsa_config", "alsa"
            }
            for op in operations
        ),
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
    changed = [
        op for op in operations
        if op["changed"] and op["kind"] != "packages" and op["_target"].exists()
    ]
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
                if operation["kind"] == "packages":
                    _install_local_packages(operation["_packages"])
                    completed.append(operation)
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
                if applied["kind"] == "packages":
                    continue
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
