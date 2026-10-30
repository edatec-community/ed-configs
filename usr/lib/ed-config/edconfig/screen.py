from __future__ import annotations

import re
from pathlib import Path

from .errors import ApplyError


_H101C_SETTINGS = {
    "desktop_name": "HMI101C",
    "desktop_mode": "800x1280@40000",
    "desktop_kanshi_mode": "800x1280",
    "desktop_transform": "90",
    "desktop_hdmi_output": "HDMI-A-1",
    "desktop_hdmi_position": "1280,0",
    "desktop_touch_devices": [
        "generic ft5x06 (79)",
        "generic ft5x06 (80)",
        "FT5406 memory based driver",
    ],
    "desktop_default_goodix": ["10-0014 Goodix Capacitive TouchScreen"],
    "desktop_unprefixed_goodix": "Goodix Capacitive TouchScreen",
    "desktop_shortcuts": "\n".join((
        "repeatable_binding_volume_up = KEY_VOLUMEUP",
        "command_volume_up = wfpanelctl volumepulse volu",
        "repeatable_binding_volume_down = KEY_VOLUMEDOWN",
        "command_volume_down = wfpanelctl volumepulse vold",
        "binding_mute = KEY_MUTE",
        "command_mute = wfpanelctl volumepulse mute",
        "binding_menu = <super>",
        "command_menu = wfpanelctl smenu menu",
        "binding_terminal = <ctrl> <alt> KEY_T",
        "command_terminal = lxterminal",
        "binding_bluetooth = <ctrl> <alt> KEY_B",
        "command_bluetooth = wfpanelctl bluetooth menu",
        "binding_netman = <ctrl> <alt> KEY_W",
        "command_netman = wfpanelctl netman menu",
        "binding_grim = KEY_SYSRQ",
        "command_grim = grim",
        "binding_orca = <ctrl> <alt> KEY_SPACE",
        "command_orca = gui-pkinst orca reboot",
        "binding_quit = <ctrl> <alt> KEY_DELETE",
        "command_quit = lxde-pi-shutdown-helper",
        "binding_power = KEY_POWER",
        "command_power = pwrkey",
    )),
    "desktop_keyboard": "\n".join((
        "xkb_model = pc105",
        "xkb_layout = gb",
        "xkb_variant =",
    )),
    "desktop_window_rule": "on created if title is \"LXTerminal\" then maximize",
    "desktop_window_rule_label": "LXTerminal maximize",
    "desktop_legacy_labwc_window_label": "LXTerminal rule",
    "desktop_wayfire_path": ".config/wayfire.ini",
    "desktop_kanshi_path": ".config/kanshi/config",
    "desktop_labwc_path": ".config/labwc/rc.xml",
    "desktop_autostart_path": "/etc/xdg/labwc/autostart",
}
_H101C_DSI_OUTPUTS = {
    "PI4": "DSI-1",
    "CM4": "DSI-1",
    "PI5": "DSI-2",
    "CM5": "DSI-2",
}


def _block(
    current: str,
    body: str,
    begin: str,
    end: str,
) -> str:
    block = f"{begin}\n{body}\n{end}"
    if begin in current:
        start = current.find(begin)
        relative_end = current[start:].find(end)
        if relative_end < 0:
            raise ApplyError(f"unterminated managed block: {begin}")
        finish = start + relative_end + len(end)
        result = current[:start] + block + current[finish:]
    elif end in current:
        raise ApplyError(f"unmatched managed block marker: {end}")
    elif current.strip():
        result = current.rstrip() + "\n\n" + block + "\n"
    else:
        result = block + "\n"
    return result if result.endswith("\n") else result + "\n"


def _remove_block(current: str, begin: str, end: str) -> str:
    lines = current.splitlines()
    if begin in lines:
        start = lines.index(begin)
        try:
            finish = lines.index(end, start + 1)
        except ValueError as exc:
            raise ApplyError(f"unterminated managed block: {begin}") from exc
        del lines[start:finish + 1]
    elif end in lines:
        raise ApplyError(f"unmatched managed block marker: {end}")
    return "\n".join(lines) + ("\n" if lines else "")


def _managed_ini_section(
    current: str,
    section: str,
    name: str,
    body: str | None,
    preserve_existing: bool = False,
) -> str:
    begin = f"# BEGIN ed-config {name}"
    managed_end = f"# END ed-config {name}"
    lines = current.splitlines()
    while True:
        start = next((index for index, line in enumerate(lines) if line == begin), None)
        if start is None:
            if managed_end in lines:
                raise ApplyError(f"unmatched Wayfire {name} block marker")
            break
        try:
            finish = lines.index(managed_end, start + 1)
        except ValueError as exc:
            raise ApplyError(f"unterminated Wayfire {name} block") from exc
        del lines[start:finish + 1]
    if body is None:
        return "\n".join(lines) + ("\n" if lines else "")

    header = f"[{section}]"
    start = next((i for i, line in enumerate(lines) if line.strip() == header), None)
    if preserve_existing and start is not None:
        finish = next(
            (i for i in range(start + 1, len(lines)) if lines[i].lstrip().startswith("[")),
            len(lines),
        )
        existing_keys = {
            match.group(1)
            for line in lines[start + 1:finish]
            if (match := re.match(r"\s*([^#;=\s]+)\s*=", line))
        }
        body_lines = [
            line
            for line in body.splitlines()
            if not (match := re.match(r"\s*([^#;=\s]+)\s*=", line))
            or match.group(1) not in existing_keys
        ]
        if not body_lines:
            return "\n".join(lines) + ("\n" if lines else "")
        body = "\n".join(body_lines)
    if start is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend([header, begin, *body.splitlines(), managed_end])
    else:
        finish = next(
            (i for i in range(start + 1, len(lines)) if lines[i].lstrip().startswith("[")),
            len(lines),
        )
        while finish > start + 1 and not lines[finish - 1].strip():
            finish -= 1
        lines[finish:finish] = [begin, *body.splitlines(), managed_end]
    return "\n".join(lines) + "\n"


def _window_rule_key(current: str, name: str) -> str:
    begin = f"# BEGIN ed-config {name}"
    end = f"# END ed-config {name}"
    lines = current.splitlines()
    if begin in lines:
        start = lines.index(begin)
        try:
            finish = lines.index(end, start + 1)
        except ValueError as exc:
            raise ApplyError(f"unterminated Wayfire {name} block") from exc
        for line in lines[start + 1:finish]:
            match = re.match(r"\s*(rule-\d+)\s*=", line)
            if match:
                return match.group(1)
    section_start = next(
        (i for i, line in enumerate(lines) if line.strip() == "[window-rules]"), None
    )
    if section_start is not None:
        finish = next(
            (i for i in range(section_start + 1, len(lines)) if lines[i].lstrip().startswith("[")),
            len(lines),
        )
        used = {
            int(match.group(1))
            for line in lines[section_start + 1:finish]
            if (match := re.match(r"\s*rule-(\d+)\s*=", line))
        }
    else:
        used = set()
    index = 1
    while index in used:
        index += 1
    return f"rule-{index}"


def _has_window_rule(current: str, expected: str) -> bool:
    lines = current.splitlines()
    section_start = next(
        (i for i, line in enumerate(lines) if line.strip() == "[window-rules]"), None
    )
    if section_start is None:
        return False
    section_end = next(
        (
            i
            for i in range(section_start + 1, len(lines))
            if lines[i].lstrip().startswith("[")
        ),
        len(lines),
    )
    return any(
        (match := re.match(r"\s*rule-\d+\s*=\s*(.*?)\s*$", line))
        and match.group(1) == expected
        for line in lines[section_start + 1:section_end]
    )


def _labwc(current: str, dsi: str, touches: list[str]) -> str:
    managed_name = "HMI101C touch mapping"
    lines = current.splitlines()
    marked_lines = _labwc_marked_lines(lines, managed_name)
    missing: list[str] = []
    managed_lines: set[int] = set()
    for touch in touches:
        found = False
        for index, line in enumerate(lines):
            match = re.search(r'\bdeviceName\s*=\s*"([^"]*)"', line)
            if "<touch" not in line or match is None or match.group(1) != touch:
                continue
            found = True
            marker = 'mapToOutput="'
            start = line.find(marker)
            if start >= 0:
                value_start = start + len(marker)
                value_end = line.find('"', value_start)
                updated = line[:value_start] + dsi + line[value_end:]
                if updated != line:
                    lines[index] = updated
                    if index not in marked_lines:
                        managed_lines.add(index)
            elif "/>" in line:
                updated = line.replace("/>", f' mapToOutput="{dsi}"/>')
                if updated != line:
                    lines[index] = updated
                    if index not in marked_lines:
                        managed_lines.add(index)
        if not found:
            missing.append(touch)
    if not current.strip():
        return (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<labwc_config>\n'
            + "".join(
                f'  <!-- BEGIN ed-config {managed_name} -->\n'
                f'  <touch deviceName="{touch}" mapToOutput="{dsi}" />\n'
                f'  <!-- END ed-config {managed_name} -->\n'
                for touch in missing
            )
            + "</labwc_config>\n"
        )
    element = "".join(
        f'  <!-- BEGIN ed-config {managed_name} -->\n'
        f'  <touch deviceName="{touch}" mapToOutput="{dsi}" />\n'
        f'  <!-- END ed-config {managed_name} -->\n'
        for touch in missing
    )
    for index in sorted(managed_lines, reverse=True):
        line = lines[index]
        indent = line[:len(line) - len(line.lstrip())]
        lines[index] = (
            f"{indent}<!-- BEGIN ed-config {managed_name} -->\n"
            f"{line}\n"
            f"{indent}<!-- END ed-config {managed_name} -->"
        )
    if not missing:
        return "\n".join(lines) + "\n"
    for closing in ("</openbox_config>", "</labwc_config>"):
        index = "\n".join(lines).rfind(closing)
        if index >= 0:
            merged = "\n".join(lines)
            return merged[:index] + element + merged[index:] + "\n"
    raise ApplyError("labwc rc.xml has no supported root closing tag")


def _remove_labwc_managed_block(current: str, name: str) -> str:
    begin = f"<!-- BEGIN ed-config {name} -->"
    end = f"<!-- END ed-config {name} -->"
    pattern = re.compile(
        rf"(?m)^[ \t]*{re.escape(begin)}\n"
        rf".*?^[ \t]*{re.escape(end)}\n?",
        re.DOTALL,
    )
    current, _ = pattern.subn("", current)
    remaining = re.compile(
        rf"(?m)^[ \t]*<!-- (?:BEGIN|END) ed-config {re.escape(name)} -->$"
    )
    if remaining.search(current):
        raise ApplyError(f"malformed ed-config Labwc {name} block")
    return current


def _labwc_marked_lines(lines: list[str], name: str) -> set[int]:
    begin = f"<!-- BEGIN ed-config {name} -->"
    end = f"<!-- END ed-config {name} -->"
    inside = False
    marked: set[int] = set()
    for index, line in enumerate(lines):
        marker = line.strip()
        if marker == begin:
            if inside:
                raise ApplyError(f"nested Labwc {name} block marker")
            inside = True
        elif marker == end:
            if not inside:
                raise ApplyError(f"unmatched Labwc {name} block marker")
            inside = False
        elif inside and "<touch" in line:
            marked.add(index)
    if inside:
        raise ApplyError(f"unterminated Labwc {name} block")
    return marked


def _squeekboard(current: str, dsi: str) -> str:
    name = "HMI101C squeekboard output"
    begin = f"# BEGIN ed-config {name}"
    end = f"# END ed-config {name}"
    lines = current.splitlines()
    while True:
        start = next((index for index, line in enumerate(lines) if line.strip() == begin), None)
        if start is None:
            if end in lines:
                raise ApplyError(f"unmatched autostart {name} block marker")
            break
        try:
            finish = lines.index(end, start + 1)
        except ValueError as exc:
            raise ApplyError(f"unterminated autostart {name} block") from exc
        del lines[finish]
        del lines[start]

    setting = f"export SQUEEKBOARD_PREFERRED_OUTPUT={dsi}"
    result: list[str] = []
    replaced = False
    for line in lines:
        if line.lstrip().startswith("export SQUEEKBOARD_PREFERRED_OUTPUT="):
            if not replaced:
                result.extend((begin, setting, end))
                replaced = True
        else:
            result.append(line)
    if not replaced:
        result[0:0] = (begin, setting, end)
    return "\n".join(result) + "\n"


def operations(
    root: Path,
    platform: str,
    configured_goodix_touches: list[str] | None = None,
    original_contents: dict[str, str | None] | None = None,
) -> list[dict[str, object]]:
    def source_text(path: Path) -> str:
        if original_contents is not None and str(path) in original_contents:
            return original_contents[str(path)] or ""
        return _text(path)

    settings = _H101C_SETTINGS
    try:
        dsi = _H101C_DSI_OUTPUTS[platform]
    except KeyError as exc:
        raise ApplyError(f"unsupported H101C platform: {platform}") from exc
    name = str(settings["desktop_name"])
    goodix_touches = (
        [str(settings["desktop_unprefixed_goodix"]), *configured_goodix_touches]
        if configured_goodix_touches is not None
        else [
            str(settings["desktop_unprefixed_goodix"]),
            *settings["desktop_default_goodix"],
        ]
    )
    wayfire_touches = [*settings["desktop_touch_devices"], *goodix_touches]
    labwc_touches = list(goodix_touches)

    result: list[dict[str, object]] = []
    home_root = root / "home"
    if home_root.is_dir():
        for home in sorted(path for path in home_root.iterdir() if path.is_dir()):
            owner = home.stat()
            wayfire = home / str(settings["desktop_wayfire_path"])
            current_wayfire = source_text(wayfire)
            kanshi = home / str(settings["desktop_kanshi_path"])
            current_kanshi = source_text(kanshi)
            wayfire_body = (
                f"[output:{dsi}]\nmode = {settings['desktop_mode']}\nposition = 0,0\ntransform = {settings['desktop_transform']}\nprimary = true\n"
                + "\n".join(
                    f"[input-device:{touch}]\noutput = {dsi}" for touch in wayfire_touches
                )
            )
            marker_begin = f"# BEGIN ed-config {name} display configuration"
            marker_end = f"# END ed-config {name} display configuration"
            current_wayfire = _block(current_wayfire, wayfire_body, marker_begin, marker_end)

            current_wayfire = _managed_ini_section(
                current_wayfire,
                "command",
                f"{name} desktop shortcuts",
                str(settings["desktop_shortcuts"]),
                preserve_existing=True,
            )
            current_wayfire = _managed_ini_section(
                current_wayfire,
                "input",
                f"{name} keyboard layout",
                str(settings["desktop_keyboard"]),
                preserve_existing=True,
            )
            rule_name = f"{name} {settings['desktop_window_rule_label']}"
            current_wayfire = _managed_ini_section(
                current_wayfire, "window-rules", rule_name, None
            )
            window_rule = str(settings["desktop_window_rule"])
            if not _has_window_rule(current_wayfire, window_rule):
                rule_key = _window_rule_key(current_wayfire, rule_name)
                current_wayfire = _managed_ini_section(
                    current_wayfire,
                    "window-rules",
                    rule_name,
                    f"{rule_key} = {window_rule}",
                )

            kanshi_body = (
                f"profile {{\n        output {dsi} mode {settings['desktop_kanshi_mode']} position 0,0 transform {settings['desktop_transform']}\n}}\n"
                f"profile {{\n        output {dsi} mode {settings['desktop_kanshi_mode']} position 0,0 transform {settings['desktop_transform']}\n"
                f"        output {settings['desktop_hdmi_output']} enable position {settings['desktop_hdmi_position']} transform normal\n}}"
            )
            result.extend([
                {"kind": "screen", "path": kanshi, "content": _block(current_kanshi, kanshi_body, marker_begin, marker_end), "mode": 0o644, "uid": owner.st_uid, "gid": owner.st_gid},
            ])

            result.append({"kind": "screen", "path": wayfire, "content": current_wayfire, "mode": 0o644, "uid": owner.st_uid, "gid": owner.st_gid})
            labwc = home / str(settings["desktop_labwc_path"])
            current_labwc = source_text(labwc)
            labwc_content = _labwc(current_labwc, dsi, labwc_touches)
            labwc_content = _remove_labwc_managed_block(
                labwc_content, f"{name} {settings['desktop_legacy_labwc_window_label']}"
            )
            result.append({"kind": "screen", "path": labwc, "content": labwc_content, "mode": 0o644, "uid": owner.st_uid, "gid": owner.st_gid})

    autostart = root.joinpath(*Path(str(settings["desktop_autostart_path"])).parts[1:])
    if autostart.is_file():
        result.append({"kind": "screen", "path": autostart, "content": _squeekboard(source_text(autostart), dsi), "mode": None})
    return result


def _text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""
    except OSError as exc:
        raise ApplyError(f"cannot read {path}: {exc}") from exc


def managed_paths(root: Path) -> set[str]:
    settings = _H101C_SETTINGS
    user_paths = {
        "/" + (home / str(settings[key])).relative_to(root).as_posix()
        for home in (root / "home").iterdir()
        if home.is_dir()
        for key in (
            "desktop_wayfire_path",
            "desktop_kanshi_path",
            "desktop_labwc_path",
        )
    } if (root / "home").is_dir() else set()
    return user_paths | {str(settings["desktop_autostart_path"])}
