from __future__ import annotations

import fcntl
import hashlib
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
ALSA_PROFILE_SET_DIR = "/etc/alsa-card-profile/mixer/profile-sets"
ALSA_PROFILE_PATH_DIR = "/etc/alsa-card-profile/mixer/paths"
DPKG = "/usr/bin/dpkg"
DPKG_DEB = "/usr/bin/dpkg-deb"
DPKG_QUERY = "/usr/bin/dpkg-query"
ALSACTL = "/usr/sbin/alsactl"
_ALSA_STATE_HEADER = re.compile(r"^state\.([^\s{]+)\s*\{\s*$")
_USB_ID = re.compile(r"^[0-9a-f]{4}:[0-9a-f]{4}$", re.IGNORECASE)


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


def _detect_sata_usb_storage_ids(root: Path) -> list[str]:
    """Find SATA USB disk VID:PID pairs, matching the old firmware package."""
    lsusb = shutil.which("lsusb")
    udevadm = shutil.which("udevadm")
    if not lsusb or not udevadm:
        return []
    try:
        usb_result = subprocess.run(
            [lsusb], capture_output=True, text=True, check=False
        )
    except OSError:
        return []
    if usb_result.returncode != 0:
        return []

    sata_ids: set[str] = set()
    for line in usb_result.stdout.splitlines():
        match = re.search(
            r"\bID\s+([0-9a-f]{4}):([0-9a-f]{4})\b(.*)$", line, re.IGNORECASE
        )
        if match and "sata" in match.group(3).casefold():
            sata_ids.add(f"{match.group(1)}:{match.group(2)}".casefold())
    if not sata_ids:
        return []

    found: set[str] = set()
    for device in sorted((root / "dev").glob("sd*")):
        if not re.fullmatch(r"sd[a-z]+", device.name):
            continue
        try:
            result = subprocess.run(
                [udevadm, "info", "--query=property", f"--name={device}"],
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError:
            continue
        if result.returncode != 0:
            continue
        properties = {}
        for line in result.stdout.splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                properties[key] = value
        bus = properties.get("ID_BUS", "").casefold()
        if bus != "usb" and not properties.get("ID_USB_DRIVER"):
            continue
        vendor = properties.get("ID_VENDOR_ID") or properties.get("ID_USB_VENDOR_ID")
        product = properties.get("ID_MODEL_ID") or properties.get("ID_USB_MODEL_ID")
        if not vendor or not product:
            continue
        device_id = f"{vendor}:{product}".casefold()
        if _USB_ID.fullmatch(device_id) and device_id in sata_ids:
            found.add(device_id)
    return sorted(found)


def _managed_usb_storage_quirks(root: Path) -> set[str]:
    status = read_status(root)
    current = status.get("current")
    if not isinstance(current, dict):
        return set()
    values = current.get("managed_usb_storage_quirks", [])
    if not isinstance(values, list):
        return set()
    return {
        str(value).casefold()
        for value in values
        if isinstance(value, str) and _USB_ID.fullmatch(value)
    }


def _cmdline_with_usb_storage_quirks(
    current: str,
    previously_managed: set[str],
    desired: set[str],
) -> tuple[str, list[str]]:
    """Update only ed-config-owned quirk entries and retain customer entries."""
    ending_match = re.search(r"[\r\n]*$", current)
    ending = ending_match.group(0) if ending_match else ""
    body = current[: len(current) - len(ending)] if ending else current
    tokens = body.split()
    parameter_indexes = [
        index for index, token in enumerate(tokens)
        if token.startswith("usb-storage.quirks=")
    ]
    entries: list[str] = []
    for index in parameter_indexes:
        entries.extend(
            value
            for value in tokens[index].split("=", 1)[1].split(",")
            if value
        )

    def entry_id(value: str) -> str | None:
        if not value.endswith(":u"):
            return None
        candidate = value[:-2].casefold()
        return candidate if _USB_ID.fullmatch(candidate) else None

    removed = previously_managed - desired
    removals_left = {device_id: 1 for device_id in removed}
    updated_entries = []
    for item in entries:
        device_id = entry_id(item)
        if device_id in removals_left and removals_left[device_id] > 0:
            removals_left[device_id] -= 1
            continue
        updated_entries.append(item)
    present_pairs = {
        pair for item in updated_entries if (pair := entry_id(item))
    }
    newly_managed: set[str] = set()
    for device_id in sorted(desired):
        if not _USB_ID.fullmatch(device_id):
            continue
        item = f"{device_id}:u"
        if device_id not in present_pairs:
            updated_entries.append(item)
            present_pairs.add(device_id)
            newly_managed.add(device_id)
    still_managed = previously_managed & desired & present_pairs
    managed_next = sorted(still_managed | newly_managed)

    old_parameters = [tokens[index] for index in parameter_indexes]
    new_parameter = (
        [f"usb-storage.quirks={','.join(updated_entries)}"]
        if updated_entries
        else []
    )
    if not entries and not desired and not removed:
        return current, managed_next
    if old_parameters == new_parameter:
        return current, managed_next

    if parameter_indexes:
        first = parameter_indexes[0]
        tokens = [
            token for index, token in enumerate(tokens)
            if index not in set(parameter_indexes)
        ]
        tokens[first:first] = new_parameter
    elif new_parameter:
        tokens.extend(new_parameter)
    return (" ".join(tokens) + ending), managed_next


def _split_managed_block(current: str) -> tuple[str, str, bool]:
    lines = current.splitlines(keepends=True)
    begin = end = None
    for index, line in enumerate(lines):
        value = line.rstrip("\r\n")
        if value == BEGIN:
            if begin is not None:
                raise ApplyError("config.txt contains duplicate EDATEC managed blocks")
            begin = index
        elif value == END:
            if begin is None or end is not None:
                raise ApplyError("config.txt contains an unmatched EDATEC managed block marker")
            end = index
    if begin is not None and end is None:
        raise ApplyError("config.txt contains an unterminated EDATEC managed block")
    if begin is None:
        return current, "", False
    return "".join(lines[:begin]), "".join(lines[end + 1:]), True


def _active_config_lines(content: str, platform: str) -> set[str]:
    active = True
    result: set[str] = set()
    for line in content.splitlines():
        value = line.strip()
        if value.startswith("[") and value.endswith("]"):
            active = value[1:-1].casefold() in {"all", platform.casefold()}
        elif active and value and not value.startswith("#"):
            result.add(value)
    return result


def _config_content(current: str | None, desired: list[str], platform: str) -> str:
    original = current or ""
    before, after, has_block = _split_managed_block(original)
    # Only the marked block belongs to ed-config. A line matching the catalog
    # outside that block may have been supplied by the OS or the user.
    existing = _active_config_lines(before + after, platform)
    managed_lines = [line for line in desired if line not in existing]
    managed = "\n".join([BEGIN, *managed_lines, END]) + "\n"
    if has_block:
        return before + managed + after
    if not original:
        return managed
    if original.endswith("\n\n"):
        separator = ""
    elif original.endswith("\n"):
        separator = "\n"
    else:
        separator = "\n\n"
    return original + separator + managed


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
    content = rule.get("ubuntu_content") if ubuntu else None
    if content is None:
        content = rule.get("default_content")
    content = content if content is not None else rule.get("content")
    if not isinstance(content, str):
        raise ConfigError(f"udev rule {rule.get('path')} has no content for this OS")
    if rule.get("exact_content"):
        return content
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


def _catalog_named_asset_contents(
    catalog: Catalog, field: str, multiple: bool = False
) -> dict[str, set[str]]:
    contents: dict[str, set[str]] = {}
    for family_id, family in catalog.families.items():
        raw_values = family.get(field, []) if multiple else [family.get(field)]
        for raw in raw_values:
            if not isinstance(raw, str):
                continue
            path = catalog.family_directories[family_id.casefold()] / raw
            try:
                content = path.read_text(encoding="utf-8")
            except OSError as exc:
                raise ConfigError(f"cannot read {field} for family {family_id}: {exc}") from exc
            contents.setdefault(Path(raw).name, set()).add(content)
    return contents


def _alsa_brace_delta(line: str) -> int:
    """Count ALSA state braces outside quoted strings and comments."""
    delta = 0
    quote: str | None = None
    escaped = False
    for character in line:
        if escaped:
            escaped = False
            continue
        if quote is not None:
            if character == "\\":
                escaped = True
            elif character == quote:
                quote = None
            continue
        if character in {"'", '"'}:
            quote = character
        elif character == "#":
            break
        elif character == "{":
            delta += 1
        elif character == "}":
            delta -= 1
    return delta


def _alsa_state_blocks(content: str) -> dict[str, tuple[int, int]]:
    """Return top-level ALSA state block spans keyed by card name."""
    blocks: dict[str, tuple[int, int]] = {}
    active_name: str | None = None
    active_start = 0
    depth = 0
    offset = 0

    for line in content.splitlines(keepends=True):
        if active_name is None:
            match = _ALSA_STATE_HEADER.match(line.rstrip("\r\n"))
            if match is None:
                offset += len(line)
                continue
            active_name = match.group(1)
            active_start = offset
            depth = 0

        depth += _alsa_brace_delta(line)
        if depth < 0:
            raise ConfigError("malformed ALSA state: unmatched closing brace")
        if depth == 0:
            if active_name in blocks:
                raise ConfigError(f"malformed ALSA state: duplicate state.{active_name} block")
            blocks[active_name] = (active_start, offset + len(line))
            active_name = None
        offset += len(line)

    if active_name is not None:
        raise ConfigError(f"malformed ALSA state: unterminated state.{active_name} block")
    return blocks


def _merge_alsa_state(
    current: str | None,
    desired: str,
    selected_names: set[str] | None = None,
    managed_names: set[str] | None = None,
) -> str:
    """Replace selected codec blocks while preserving unrelated card state."""
    desired_blocks = _alsa_state_blocks(desired)
    if not desired_blocks:
        raise ConfigError("ALSA state asset contains no state blocks")
    selected = set(desired_blocks) if selected_names is None else set(selected_names)
    missing = selected - set(desired_blocks)
    if missing:
        names = ", ".join(sorted(missing))
        raise ConfigError(f"ALSA state asset is missing selected block(s): {names}")
    managed = set(managed_names or ())

    def selected_content() -> str:
        content = "\n\n".join(
            desired[desired_blocks[name][0]:desired_blocks[name][1]].rstrip("\r\n")
            for name in desired_blocks
            if name in selected
        )
        return content + "\n" if content else ""

    if current is None or not current.strip():
        return selected_content()

    current_blocks = _alsa_state_blocks(current)
    if not current_blocks:
        # An unrelated or empty state file has no recognizable ALSA card
        # blocks to merge. Keep it intact as unmanaged customer content.
        return current

    output: list[str] = []
    cursor = 0
    for name, (start, end) in sorted(current_blocks.items(), key=lambda item: item[1][0]):
        output.append(current[cursor:start])
        desired_span = desired_blocks.get(name)
        if name in selected and desired_span is not None:
            output.append(desired[desired_span[0]:desired_span[1]])
        elif name in managed and name not in selected:
            pass
        else:
            output.append(current[start:end])
        cursor = end
    output.append(current[cursor:])
    merged = "".join(output)

    missing = [
        name for name, (start, _end) in desired_blocks.items()
        if name in selected and name not in current_blocks
    ]
    if missing:
        appended = "\n\n".join(
            desired[desired_blocks[name][0]:desired_blocks[name][1]].rstrip("\r\n")
            for name in missing
        )
        merged = merged.rstrip("\r\n") + "\n\n" + appended + "\n"
    return merged


def _remove_alsa_state_blocks(current: str, names: set[str]) -> str:
    """Remove only blocks owned by ed-config from a global ALSA state file."""
    current_blocks = _alsa_state_blocks(current)
    if not current_blocks:
        return current

    output: list[str] = []
    cursor = 0
    for name, (start, end) in sorted(current_blocks.items(), key=lambda item: item[1][0]):
        output.append(current[cursor:start])
        if name not in names:
            output.append(current[start:end])
        cursor = end
    output.append(current[cursor:])
    return "".join(output)


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


def _catalog_managed_file_contents(catalog: Catalog) -> dict[str, set[str]]:
    contents: dict[str, set[str]] = {}
    for family_id, family in catalog.families.items():
        package_dir = catalog.family_directories[family_id.casefold()]
        for asset in family.get("managed_files", []):
            path = package_dir / str(asset["content_file"])
            try:
                content = path.read_bytes().decode("utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                raise ConfigError(f"cannot read managed file {path}: {exc}") from exc
            contents.setdefault(str(asset["path"]), set()).add(content)
    return contents


def _tracked_managed_files(root: Path, known_paths: set[str]) -> dict[str, str]:
    try:
        current = read_status(root).get("current")
    except ConfigError:
        return {}
    if not isinstance(current, dict):
        return {}
    hashes = current.get("managed_file_hashes", {})
    if not isinstance(hashes, dict):
        return {}
    return {
        path: digest
        for path, digest in hashes.items()
        if isinstance(path, str)
        and path in known_paths
        and isinstance(digest, str)
        and re.fullmatch(r"[0-9a-f]{64}", digest)
    }


def _tracked_systemd_services(root: Path, known_services: set[str]) -> set[str]:
    if not known_services:
        return set()
    try:
        current = read_status(root).get("current")
    except ConfigError:
        # Legacy models do not depend on systemd ownership metadata. Preserve
        # their ability to recover from an invalid state file as before.
        return set()
    if not isinstance(current, dict):
        return set()
    services = current.get("managed_systemd_services", [])
    if not isinstance(services, list):
        return set()
    return {service for service in services if isinstance(service, str) and service in known_services}


def _symlink_target(path: Path) -> str | None:
    try:
        return os.readlink(path) if path.is_symlink() else None
    except OSError:
        return None


def _wireplumber_content(root: Path, audio_format: str) -> str | None:
    """Reproduce the DEB's copy-and-edit of the distro Lua configuration."""
    source = rooted(root, WIREPLUMBER_SOURCE)
    content = _read(source)
    if content is None:
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


def _restore_alsa_state() -> None:
    command = [
        ALSACTL,
        "-E",
        "HOME=/run/alsa",
        "-E",
        "XDG_RUNTIME_DIR=/run/alsa/runtime",
        "restore",
    ]
    try:
        subprocess.run(command, check=True, capture_output=True, text=True)
    except OSError:
        raise
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        raise OSError(detail or "alsactl restore failed") from exc


def _alsa_state_card_present(root: Path, names: set[str]) -> bool:
    """Check whether one of the managed ALSA cards is currently enumerated."""
    if not names:
        return False
    proc_asound = root / "proc/asound"
    try:
        card_ids = {
            re.sub(r"[^a-z0-9]", "", path.read_text(encoding="utf-8").strip().casefold())
            for path in proc_asound.glob("card*/id")
        }
    except OSError:
        return False
    wanted = {
        re.sub(r"[^a-z0-9]", "", name.casefold())
        for name in names
    }
    return bool(card_ids & wanted)


def build_plan(
    catalog: Catalog,
    resolution: dict[str, Any],
    root: Path,
    config_file: str | None = None,
    os_name: str = "auto",
    detected_usb_storage_quirks: list[str] | None = None,
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
        try:
            original_mode = stat.S_IMODE(path.stat().st_mode)
        except OSError:
            original_mode = None
        mode_changed = (
            kind == "managed_file"
            and mode is not None
            and original_mode is not None
            and original_mode != mode
        )
        operations.append({
            "kind": kind,
            "action": "write",
            "path": "/" + path.relative_to(root).as_posix(),
            "changed": current != content or mode_changed,
            "_target": path,
            "_content": content,
            "_original": current,
            "_original_mode": original_mode,
            "_mode": mode,
            "_uid": uid,
            "_gid": gid,
        })

    config_content = _config_content(
        _read(config_path), desired, str(resolution["identity"]["platform"])
    )
    write_operation("config", config_path, config_content)

    ddr_selected = "ddr" in resolution["options"]
    try:
        previous_usb_quirks = _managed_usb_storage_quirks(root)
    except ConfigError:
        if ddr_selected:
            raise
        # A legacy model does not depend on DDR tracking. If an old or
        # malformed state file cannot be read, leave cmdline.txt untouched.
        previous_usb_quirks = set()
    if detected_usb_storage_quirks is None:
        detected_usb_storage_quirks = (
            _detect_sata_usb_storage_ids(root)
            if ddr_selected and root == Path("/")
            else []
        )
    detected_ids = {
        str(value).casefold()
        for value in detected_usb_storage_quirks
        if isinstance(value, str) and _USB_ID.fullmatch(value)
    }
    desired_usb_quirks = (
        detected_ids or previous_usb_quirks if ddr_selected else set()
    )
    managed_usb_quirks = set(previous_usb_quirks)
    if ddr_selected or previous_usb_quirks:
        cmdline_path = config_path.with_name("cmdline.txt")
        current_cmdline = _read(cmdline_path)
        if current_cmdline is not None:
            cmdline_content, managed_usb_quirks_list = _cmdline_with_usb_storage_quirks(
                current_cmdline,
                previous_usb_quirks,
                desired_usb_quirks,
            )
            managed_usb_quirks = set(managed_usb_quirks_list)
            if cmdline_content != current_cmdline:
                write_operation("cmdline", cmdline_path, cmdline_content)
        elif not ddr_selected:
            managed_usb_quirks = set()

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

    managed_file_contents = _catalog_managed_file_contents(catalog)
    previously_managed_files = _tracked_managed_files(root, set(managed_file_contents))
    managed_files_after: dict[str, str] = {}
    managed_executables: set[str] = set()

    def known_managed_content(path: str, current: str | None) -> bool:
        if current is None:
            return True
        prior_digest = previously_managed_files.get(path)
        return prior_digest is not None and hashlib.sha256(current.encode("utf-8")).hexdigest() == prior_digest

    selected_managed_paths: set[str] = set()
    for asset in resolution.get("managed_files", []):
        path = str(asset["path"])
        selected_managed_paths.add(path)
        target = rooted(root, path)
        current = _read(target)
        # Do not follow or replace customer-created symlinks at managed paths.
        if target.is_symlink():
            continue
        owned_by_ed_config = known_managed_content(path, current)
        asset_content = str(asset["content"])
        asset_mode = int(asset["mode"])
        try:
            current_mode = stat.S_IMODE(target.stat().st_mode)
        except OSError:
            current_mode = None
        already_matches_asset = current == asset_content and current_mode == asset_mode
        if owned_by_ed_config:
            write_operation("managed_file", target, asset_content, asset_mode)
            managed_files_after[path] = hashlib.sha256(asset_content.encode("utf-8")).hexdigest()
        if path.startswith("/usr/bin/") and (owned_by_ed_config or already_matches_asset):
            managed_executables.add(path)
    for path in sorted(managed_file_contents):
        if path in selected_managed_paths or path not in previously_managed_files:
            continue
        target = rooted(root, path)
        current = _read(target)
        if not target.is_symlink() and current is not None and known_managed_content(path, current):
            try:
                original_mode = stat.S_IMODE(target.stat().st_mode)
            except OSError:
                original_mode = None
            operations.append({
                "kind": "managed_file",
                "action": "delete",
                "path": path,
                "changed": True,
                "_target": target,
                "_original": current,
                "_original_mode": original_mode,
            })

    selected_file_assets = {
        str(asset["path"]): asset for asset in resolution.get("managed_files", [])
    }
    selected_services = set()
    known_executables = {
        path for path in managed_file_contents if path.startswith("/usr/bin/")
    }
    for service in resolution.get("systemd_services", []):
        unit_path = f"/usr/lib/systemd/system/{service}"
        asset = selected_file_assets.get(unit_path)
        if asset is None:
            continue
        required_executables = {
            path for path in known_executables if path in str(asset["content"])
        }
        if not required_executables <= managed_executables:
            continue
        target = rooted(root, unit_path)
        current = _read(target)
        try:
            current_mode = stat.S_IMODE(target.stat().st_mode)
        except OSError:
            current_mode = None
        unit_matches_asset = (
            current == str(asset["content"])
            and current_mode == int(asset["mode"])
        )
        if not target.is_symlink() and (
            known_managed_content(unit_path, current) or unit_matches_asset
        ):
            selected_services.add(str(service))
    known_service_targets = resolution.get("systemd_service_targets", {})
    known_services = set(known_service_targets)
    previously_managed_services = _tracked_systemd_services(root, known_services)
    managed_services_after: set[str] = set()
    for service in sorted(selected_services | previously_managed_services):
        target_name = known_service_targets[service]
        link_path = f"/etc/systemd/system/{target_name}.wants/{service}"
        target = rooted(root, link_path)
        desired_link = f"/usr/lib/systemd/system/{service}"
        unit_path = f"/usr/lib/systemd/system/{service}"
        unit_target = rooted(root, unit_path)
        unit_current = _read(unit_target)
        unit_is_managed = (
            not unit_target.is_symlink()
            and known_managed_content(unit_path, unit_current)
        )
        current_link = _symlink_target(target)
        exists = os.path.lexists(target)
        if service in selected_services:
            if current_link == desired_link:
                if service in previously_managed_services:
                    managed_services_after.add(service)
            elif not exists:
                operations.append({
                    "kind": "systemd",
                    "action": "symlink",
                    "path": link_path,
                    "changed": True,
                    "_target": target,
                    "_symlink_target": desired_link,
                    "_original_symlink_target": None,
                })
                managed_services_after.add(service)
        elif current_link == desired_link and unit_is_managed:
            operations.append({
                "kind": "systemd",
                "action": "delete",
                "path": link_path,
                "changed": True,
                "_target": target,
                "_original_symlink_target": current_link,
            })

    if catalog.has_wireplumber_config():
        wp_format = resolution.get("wireplumber_audio_format")
        wp_source = rooted(root, WIREPLUMBER_SOURCE)
        managed_wireplumber_contents: set[str] = set()
        if wp_source.is_file():
            for audio_format in _catalog_field_values(catalog, "wireplumber_audio_format"):
                content = _wireplumber_content(root, audio_format)
                if content is not None:
                    managed_wireplumber_contents.add(content)
        if wp_source.is_file():
            # Debian 12/WirePlumber 0.4 uses the distro Lua configuration path.
            wp_target = rooted(root, WIREPLUMBER_CONFIG)
            wp_content = (
                _wireplumber_content(root, str(wp_format))
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
                managed_wireplumber_contents
                if wp_target == rooted(root, WIREPLUMBER_CONFIG)
                else _catalog_asset_contents(catalog, "wireplumber_modern_file")
            )
            if current_wp is None or current_wp in managed_wp:
                write_operation("wireplumber", wp_target, str(wp_content), 0o644)
        if (
            wp_target == rooted(root, WIREPLUMBER_MODERN_CONFIG)
            and wp_content is None
            and _read(wp_target)
            in _catalog_asset_contents(catalog, "wireplumber_modern_file")
        ):
            operations.append({
                "kind": "wireplumber",
                "action": "delete",
                "path": WIREPLUMBER_MODERN_CONFIG,
                "changed": True,
                "_target": wp_target,
                "_original": _read(wp_target),
            })
    alsa_restore_needed = False
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
        managed_names = {
            str(block)
            for family in catalog.families.values()
            for block in family.get("alsa_state_blocks", [])
        }
        current_state = _read(state_target)
        if state_content:
            selected_names = {
                str(block) for block in resolution.get("alsa_state_blocks", [])
            } or None
            merged_state = _merge_alsa_state(
                current_state,
                str(state_content),
                selected_names,
                managed_names,
            )
            if current_state != merged_state:
                write_operation("alsa", state_target, merged_state, 0o644)
                alsa_restore_needed = True
        elif current_state is not None:
            remaining_state = _remove_alsa_state_blocks(current_state, managed_names)
            if remaining_state != current_state:
                if remaining_state.strip():
                    write_operation("alsa", state_target, remaining_state, 0o644)
                else:
                    operations.append({
                        "kind": "alsa",
                        "action": "delete",
                        "path": ALSA_STATE,
                        "changed": True,
                        "_target": state_target,
                        "_original": current_state,
                    })
                alsa_restore_needed = True

    managed_profile_sets = _catalog_named_asset_contents(
        catalog, "alsa_profile_set_file"
    )
    managed_profile_paths = _catalog_named_asset_contents(
        catalog, "alsa_profile_path_files", multiple=True
    )
    selected_profile_set_file = resolution.get("alsa_profile_set_file")
    selected_profile_set_name = (
        Path(str(selected_profile_set_file)).name
        if selected_profile_set_file
        else None
    )
    selected_profile_set = resolution.get("alsa_profile_set")
    if selected_profile_set_name and isinstance(selected_profile_set, str):
        profile_target = rooted(
            root, f"{ALSA_PROFILE_SET_DIR}/{selected_profile_set_name}"
        )
        current_profile = _read(profile_target)
        if current_profile is None or current_profile in managed_profile_sets.get(
            selected_profile_set_name, set()
        ):
            write_operation("alsa_profile", profile_target, selected_profile_set, 0o644)
    for name, managed in managed_profile_sets.items():
        if name == selected_profile_set_name:
            continue
        profile_target = rooted(root, f"{ALSA_PROFILE_SET_DIR}/{name}")
        if _read(profile_target) in managed:
            operations.append({
                "kind": "alsa_profile",
                "action": "delete",
                "path": f"{ALSA_PROFILE_SET_DIR}/{name}",
                "changed": True,
                "_target": profile_target,
                "_original": _read(profile_target),
            })

    selected_profile_path_names: set[str] = set()
    for profile_path in resolution.get("alsa_profile_path_files", []):
        if not isinstance(profile_path, dict):
            continue
        name = Path(str(profile_path.get("name", ""))).name
        content = profile_path.get("content")
        if not name or not isinstance(content, str):
            continue
        selected_profile_path_names.add(name)
        path_target = rooted(root, f"{ALSA_PROFILE_PATH_DIR}/{name}")
        current_path = _read(path_target)
        if current_path is None or current_path in managed_profile_paths.get(name, set()):
            write_operation("alsa_profile", path_target, content, 0o644)
    for name, managed in managed_profile_paths.items():
        if name in selected_profile_path_names:
            continue
        path_target = rooted(root, f"{ALSA_PROFILE_PATH_DIR}/{name}")
        if _read(path_target) in managed:
            operations.append({
                "kind": "alsa_profile",
                "action": "delete",
                "path": f"{ALSA_PROFILE_PATH_DIR}/{name}",
                "changed": True,
                "_target": path_target,
                "_original": _read(path_target),
            })

    config_changed = any(
        operation["changed"] and operation["kind"] == "config"
        for operation in operations
    )
    selected_alsa_blocks = {
        str(block) for block in resolution.get("alsa_state_blocks", [])
    }
    # A newly written config.txt cannot enable the codec until the next boot.
    # Do not run alsactl against a state file whose managed card is not yet
    # enumerated; the state file is already staged for alsa-restore.service.
    alsa_card_ready = _alsa_state_card_present(root, selected_alsa_blocks)
    if (
        alsa_restore_needed
        and root == Path("/")
        and not config_changed
        and alsa_card_ready
        and Path(ALSACTL).is_file()
        and (root / "dev/snd").is_dir()
    ):
        operations.append({
            "kind": "alsa_restore",
            "action": "restore",
            "path": ALSA_STATE,
            "changed": True,
            "_target": None,
        })

    screen_changes = []
    if resolution.get("screen_config") is not None:
        screen_changes = screen_operations(
            root,
            resolution["identity"]["platform"],
            resolution.get("screen_config"),
            resolution.get("screen_goodix_touch_devices") or None,
        )
    for operation in screen_changes:
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
                "config", "cmdline", "udev", "managed_file", "systemd", "wireplumber", "alsa_profile", "alsa_config", "alsa",
            }
            for op in operations
        ),
        "operations": operations,
        "_managed_usb_storage_quirks": sorted(managed_usb_quirks),
        "_managed_file_hashes": dict(sorted(managed_files_after.items())),
        "_managed_systemd_services": sorted(managed_services_after),
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
    force_mode: bool = False,
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
    previous_mode = stat.S_IMODE(previous.st_mode) if previous else None
    write_mode = mode if force_mode and mode is not None else previous_mode
    if previous is None:
        write_mode = mode
    owner_uid = previous.st_uid if previous else uid
    owner_gid = previous.st_gid if previous else gid
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if write_mode is not None:
            os.chmod(temporary, write_mode)
        if owner_uid is not None or owner_gid is not None:
            os.chown(temporary, owner_uid if owner_uid is not None else -1, owner_gid if owner_gid is not None else -1)
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _atomic_symlink(path: Path, target: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        temporary.unlink()
        os.symlink(target, temporary)
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
        if (
            op["changed"]
            and op["kind"] != "packages"
            and op.get("_target") is not None
            and (os.path.lexists(op["_target"]) if op["kind"] == "systemd" else op["_target"].exists())
        )
    ]
    if not changed:
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    directory = root / "var/lib/ed-config/backups" / stamp
    for operation in changed:
        source: Path = operation["_target"]
        destination = directory / source.relative_to(root)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination, follow_symlinks=operation["kind"] != "systemd")
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
                if operation["kind"] == "alsa_restore":
                    _restore_alsa_state()
                    completed.append(operation)
                    continue
                target: Path = operation["_target"]
                if operation["action"] == "delete":
                    target.unlink(missing_ok=True)
                elif operation["action"] == "symlink":
                    _atomic_symlink(target, operation["_symlink_target"])
                else:
                    _atomic_write(
                        target,
                        operation["_content"],
                        operation.get("_mode"),
                        operation.get("_uid"),
                        operation.get("_gid"),
                        force_mode=operation["kind"] == "managed_file",
                    )
                completed.append(operation)
        except OSError as exc:
            rollback_errors: list[str] = []
            for applied in reversed(completed):
                if applied["kind"] == "packages" or applied.get("_target") is None:
                    continue
                target = applied["_target"]
                original = applied.get("_original")
                try:
                    if applied.get("kind") == "systemd":
                        old_target = applied.get("_original_symlink_target")
                        if old_target is None:
                            target.unlink(missing_ok=True)
                        else:
                            _atomic_symlink(target, old_target)
                    elif original is None:
                        target.unlink(missing_ok=True)
                    else:
                        _atomic_write(
                            target,
                            original,
                            applied.get("_original_mode"),
                            applied.get("_uid"),
                            applied.get("_gid"),
                            force_mode=applied["kind"] == "managed_file",
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
        managed_usb_quirks = plan.get("_managed_usb_storage_quirks", [])
        if managed_usb_quirks:
            state["current"]["managed_usb_storage_quirks"] = managed_usb_quirks
        managed_files = plan.get("_managed_file_hashes", {})
        if managed_files:
            state["current"]["managed_file_hashes"] = managed_files
        managed_services = plan.get("_managed_systemd_services", [])
        if managed_services:
            state["current"]["managed_systemd_services"] = managed_services
        state_path = root / "var/lib/ed-config/state.json"
        _atomic_write(state_path, json.dumps(state, indent=2, ensure_ascii=False) + "\n", 0o644)
        return {
            "success": True,
            "changed": any(op["changed"] for op in plan["operations"]),
            "model": plan["model"],
            "options": plan["options"],
            "product_code": plan["product_code"],
            "product_code_hex": f"0x{plan['product_code']:08x}",
            "changed_files": [
                op["path"]
                for op in plan["operations"]
                if op["changed"] and op["kind"] != "alsa_restore"
            ],
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

    if state.get("format") != "ed-config-state" or state.get("schema_version") != 2:
        raise ConfigError(f"invalid status file {path}: unsupported state format or schema")
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
