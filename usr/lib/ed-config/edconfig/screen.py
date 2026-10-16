from __future__ import annotations

import re
from pathlib import Path

from .errors import ApplyError


DISPLAY_BEGIN = "# BEGIN ed-config HMI101C display configuration"
DISPLAY_END = "# END ed-config HMI101C display configuration"
H101C_COMMON_TOUCH_DEVICES = (
    "generic ft5x06 (79)",
    "generic ft5x06 (80)",
    "FT5406 memory based driver",
)
H101C_DEFAULT_GOODIX_TOUCH_DEVICES = (
    "10-0014 Goodix Capacitive TouchScreen",
)
H101C_UNPREFIXED_GOODIX_TOUCH_DEVICE = "Goodix Capacitive TouchScreen"
WAYFIRE_DESKTOP_SHORTCUTS = (
    "repeatable_binding_volume_up = KEY_VOLUMEUP\n"
    "command_volume_up = wfpanelctl volumepulse volu\n"
    "repeatable_binding_volume_down = KEY_VOLUMEDOWN\n"
    "command_volume_down = wfpanelctl volumepulse vold\n"
    "binding_mute = KEY_MUTE\n"
    "command_mute = wfpanelctl volumepulse mute\n"
    "binding_menu = <super>\n"
    "command_menu = wfpanelctl smenu menu\n"
    "binding_terminal = <ctrl> <alt> KEY_T\n"
    "command_terminal = lxterminal\n"
    "binding_bluetooth = <ctrl> <alt> KEY_B\n"
    "command_bluetooth = wfpanelctl bluetooth menu\n"
    "binding_netman = <ctrl> <alt> KEY_W\n"
    "command_netman = wfpanelctl netman menu\n"
    "binding_grim = KEY_SYSRQ\n"
    "command_grim = grim\n"
    "binding_orca = <ctrl> <alt> KEY_SPACE\n"
    "command_orca = gui-pkinst orca reboot\n"
    "binding_quit = <ctrl> <alt> KEY_DELETE\n"
    "command_quit = lxde-pi-shutdown-helper\n"
    "binding_power = KEY_POWER\n"
    "command_power = pwrkey"
)
WAYFIRE_KEYBOARD_LAYOUT = "xkb_model = pc105\nxkb_layout = gb\nxkb_variant ="
WAYFIRE_LXTERMINAL_RULE = 'on created if title is "LXTerminal" then maximize'


def _block(
    current: str,
    body: str,
    begin: str = DISPLAY_BEGIN,
    end: str = DISPLAY_END,
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
    lines = current.splitlines()
    missing: list[str] = []
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
                lines[index] = line[:value_start] + dsi + line[value_end:]
            elif "/>" in line:
                lines[index] = line.replace("/>", f' mapToOutput="{dsi}"/>')
        if not found:
            missing.append(touch)
    if not current.strip():
        return (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<labwc_config>\n'
            + "".join(
                f'  <touch deviceName="{touch}" mapToOutput="{dsi}" />\n'
                for touch in missing
            )
            + "</labwc_config>\n"
        )
    element = "".join(
        f'  <touch deviceName="{touch}" mapToOutput="{dsi}" />\n'
        for touch in missing
    )
    if not missing:
        return "\n".join(lines) + "\n"
    for closing in ("</openbox_config>", "</labwc_config>"):
        index = "\n".join(lines).rfind(closing)
        if index >= 0:
            merged = "\n".join(lines)
            return merged[:index] + element + merged[index:] + "\n"
    raise ApplyError("labwc rc.xml has no supported root closing tag")


def _labwc_touch_block(current: str, dsi: str, touches: list[str]) -> str:
    current = _remove_labwc_managed_block(current, "HMI101C touch mapping")
    if not touches:
        return current
    begin = "<!-- BEGIN ed-config HMI101C touch mapping -->"
    end = "<!-- END ed-config HMI101C touch mapping -->"
    block = begin + "\n" + "".join(
        f'    <touch deviceName="{touch}" mapToOutput="{dsi}" mouseEmulation="yes"/>\n'
        for touch in touches
    ) + end + "\n"
    for root_close in ("</openbox_config>", "</labwc_config>"):
        index = current.rfind(root_close)
        if index >= 0:
            return current[:index] + block + current[index:]
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


def _labwc_window_rule(current: str, enabled: bool) -> str:
    current = _remove_labwc_managed_block(current, "HMI101C LXTerminal rule")
    if not enabled:
        return current
    begin = "<!-- BEGIN ed-config HMI101C LXTerminal rule -->"
    end = "<!-- END ed-config HMI101C LXTerminal rule -->"
    block = (
        f"  {begin}\n"
        '    <windowRule identifier="LXTerminal"><action name="Maximize" /></windowRule>\n'
        f"  {end}\n"
    )
    closing = "</windowRules>"
    index = current.rfind(closing)
    if index >= 0:
        line_start = current.rfind("\n", 0, index) + 1
        return current[:line_start] + block + current[line_start:]
    for root_close in ("</openbox_config>", "</labwc_config>"):
        index = current.rfind(root_close)
        if index >= 0:
            section = f"  <windowRules>\n{block}  </windowRules>\n"
            return current[:index] + section + current[index:]
    raise ApplyError("labwc rc.xml has no supported root closing tag")


def _squeekboard(current: str, dsi: str) -> str:
    setting = f"export SQUEEKBOARD_PREFERRED_OUTPUT={dsi}"
    result: list[str] = []
    replaced = False
    for line in current.splitlines():
        if line.lstrip().startswith("export SQUEEKBOARD_PREFERRED_OUTPUT="):
            if not replaced:
                result.append(setting)
                replaced = True
        else:
            result.append(line)
    if not replaced:
        result.insert(0, setting)
    return "\n".join(result) + "\n"


DISPLAY_SETUP = '''#!/bin/sh

D_DSI=$(xrandr | awk '/^DSI.* connected/ {print $1; exit}')
D_HDMI=$(xrandr | awk '/^HDMI.* connected/ {print $1; exit}')
D_TOUCH=$(xinput | awk -F'id=' '/Goodix Capacitive TouchScreen/ && /pointer/ {gsub(/^.*↳ /,"",$1); sub(/[ \\t]+$/,"",$1); print $1; exit}')

if [ -n "$D_DSI" ]; then
    if [ -n "$D_HDMI" ] && xrandr --output "$D_HDMI" --auto --pos 1280x0 --output "$D_DSI" --primary --mode 800x1280 --pos 0x0 --rotate left --dryrun; then
        xrandr --output "$D_HDMI" --auto --pos 1280x0 --output "$D_DSI" --primary --mode 800x1280 --pos 0x0 --rotate left
    elif xrandr --output "$D_DSI" --primary --mode 800x1280 --pos 0x0 --rotate left --dryrun; then
        xrandr --output "$D_DSI" --primary --mode 800x1280 --pos 0x0 --rotate left
    fi
    if [ -n "$D_TOUCH" ]; then
        xinput --map-to-output "$D_TOUCH" "$D_DSI"
    fi
fi

if [ -e /usr/share/ovscsetup.sh ]; then
    /usr/share/ovscsetup.sh
fi
exit 0
'''


def operations(
    root: Path,
    platform: str,
    profile: str,
    configured_goodix_touches: list[str] | None = None,
) -> list[dict[str, object]]:
    if profile != "h101c":
        raise ApplyError(f"unsupported desktop configuration profile: {profile}")
    dsi = "DSI-1" if platform.casefold() in {"cm4", "pi4"} else "DSI-2"
    goodix_touches = (
        [H101C_UNPREFIXED_GOODIX_TOUCH_DEVICE, *configured_goodix_touches]
        if configured_goodix_touches is not None
        else [
            H101C_UNPREFIXED_GOODIX_TOUCH_DEVICE,
            *H101C_DEFAULT_GOODIX_TOUCH_DEVICES,
        ]
    )
    wayfire_touches = [*H101C_COMMON_TOUCH_DEVICES, *goodix_touches]
    labwc_touches = list(goodix_touches)

    result: list[dict[str, object]] = []
    home_root = root / "home"
    if home_root.is_dir():
        for home in sorted(path for path in home_root.iterdir() if path.is_dir()):
            owner = home.stat()
            config = home / ".config"
            wayfire = config / "wayfire.ini"
            current_wayfire = _text(wayfire)
            kanshi = config / "kanshi/config"
            current_kanshi = _text(kanshi)
            wayfire_body = (
                f"[output:{dsi}]\nmode = 800x1280@40000\nposition = 0,0\ntransform = 90\nprimary = true\n"
                + "\n".join(
                    f"[input-device:{touch}]\noutput = {dsi}" for touch in wayfire_touches
                )
            )
            current_wayfire = _block(current_wayfire, wayfire_body)

            name = "HMI101C"
            current_wayfire = _managed_ini_section(
                current_wayfire,
                "command",
                f"{name} desktop shortcuts",
                WAYFIRE_DESKTOP_SHORTCUTS,
                preserve_existing=True,
            )
            current_wayfire = _managed_ini_section(
                current_wayfire,
                "input",
                f"{name} keyboard layout",
                WAYFIRE_KEYBOARD_LAYOUT,
                preserve_existing=True,
            )
            rule_name = f"{name} LXTerminal maximize"
            current_wayfire = _managed_ini_section(
                current_wayfire, "window-rules", rule_name, None
            )
            if not _has_window_rule(current_wayfire, WAYFIRE_LXTERMINAL_RULE):
                rule_key = _window_rule_key(current_wayfire, rule_name)
                current_wayfire = _managed_ini_section(
                    current_wayfire,
                    "window-rules",
                    rule_name,
                    f"{rule_key} = {WAYFIRE_LXTERMINAL_RULE}",
                )

            kanshi_body = (
                f"profile {{\n        output {dsi} mode 800x1280 position 0,0 transform 90\n}}\n"
                f"profile {{\n        output {dsi} mode 800x1280 position 0,0 transform 90\n"
                "        output HDMI-A-1 enable position 1280,0 transform normal\n}"
            )
            result.extend([
                {"kind": "screen", "path": kanshi, "content": _block(current_kanshi, kanshi_body), "mode": 0o644, "uid": owner.st_uid, "gid": owner.st_gid},
            ])

            result.append({"kind": "screen", "path": wayfire, "content": current_wayfire, "mode": 0o644, "uid": owner.st_uid, "gid": owner.st_gid})
            labwc = config / "labwc/rc.xml"
            current_labwc = _text(labwc)
            labwc_base = _labwc_touch_block(current_labwc, dsi, []) if current_labwc else current_labwc
            labwc_content = _labwc(labwc_base, dsi, labwc_touches)
            labwc_content = _labwc_window_rule(labwc_content, False)
            result.append({"kind": "screen", "path": labwc, "content": labwc_content, "mode": 0o644, "uid": owner.st_uid, "gid": owner.st_gid})

    autostart = root / "etc/xdg/labwc/autostart"
    if autostart.is_file():
        result.append({"kind": "screen", "path": autostart, "content": _squeekboard(_text(autostart), dsi), "mode": None})
    result.append({"kind": "screen", "path": root / "usr/share/dispsetup.sh", "content": DISPLAY_SETUP, "mode": 0o755})
    return result


def _text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""
    except OSError as exc:
        raise ApplyError(f"cannot read {path}: {exc}") from exc
