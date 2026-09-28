from __future__ import annotations

import re
from pathlib import Path

from .errors import ApplyError


DISPLAY_BEGIN = "# BEGIN ed-config HMI101C display configuration"
DISPLAY_END = "# END ed-config HMI101C display configuration"
LEGACY_DISPLAY_BEGIN = "# BEGIN ed-tools HMI101C display configuration"
LEGACY_DISPLAY_END = "# END ed-tools HMI101C display configuration"
HMI3020_DISPLAY_BEGIN = "# BEGIN ed-config HMI3020 display configuration"
HMI3020_DISPLAY_END = "# END ed-config HMI3020 display configuration"


def _block(
    current: str,
    body: str,
    begin: str = DISPLAY_BEGIN,
    end: str = DISPLAY_END,
    legacy_begin: str | None = LEGACY_DISPLAY_BEGIN,
    legacy_end: str | None = LEGACY_DISPLAY_END,
) -> str:
    block = f"{begin}\n{body}\n{end}"
    markers = [(begin, end)]
    if legacy_begin and legacy_end:
        markers.append((legacy_begin, legacy_end))
    found = next(((begin, end) for begin, end in markers if begin in current), None)
    if found:
        begin, end_marker = found
        start = current.find(begin)
        relative_end = current[start:].find(end_marker)
        if relative_end < 0:
            raise ApplyError(f"unterminated managed block: {begin}")
        end = start + relative_end + len(end_marker)
        result = current[:start] + block + current[end:]
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
    current: str, section: str, name: str, body: str | None
) -> str:
    begin = f"# BEGIN ed-config {name}"
    end = f"# END ed-config {name}"
    lines = current.splitlines()
    if begin in lines:
        start = lines.index(begin)
        try:
            finish = lines.index(end, start + 1)
        except ValueError as exc:
            raise ApplyError(f"unterminated Wayfire {name} block") from exc
        del lines[start:finish + 1]
    elif end in lines:
        raise ApplyError(f"unmatched Wayfire {name} block marker")
    if body is None:
        return "\n".join(lines) + ("\n" if lines else "")

    header = f"[{section}]"
    start = next((i for i, line in enumerate(lines) if line.strip() == header), None)
    if start is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend([header, begin, *body.splitlines(), end])
    else:
        finish = next(
            (i for i in range(start + 1, len(lines)) if lines[i].lstrip().startswith("[")),
            len(lines),
        )
        while finish > start + 1 and not lines[finish - 1].strip():
            finish -= 1
        lines[finish:finish] = [begin, *body.splitlines(), end]
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


def _labwc(current: str, dsi: str, touches: list[str]) -> str:
    lines = current.splitlines()
    missing: list[str] = []
    for touch in touches:
        found = False
        for index, line in enumerate(lines):
            if "<touch" not in line or touch not in line:
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
            '<?xml version="1.0"?>\n'
            '<openbox_config xmlns="http://openbox.org/3.4/rc">\n'
            + "".join(
                f'        <touch deviceName="{touch}" mapToOutput="{dsi}" mouseEmulation="yes"/>\n'
                for touch in missing
            )
            + "</openbox_config>\n"
        )
    element = "".join(
        f'        <touch deviceName="{touch}" mapToOutput="{dsi}" mouseEmulation="yes"/>\n'
        for touch in missing
    )
    if not missing:
        return "\n".join(lines) + "\n"
    for closing in ("</openbox_config>", "</labwc_config>"):
        index = "\n".join(lines).rfind(closing)
        if index >= 0:
            merged = "\n".join(lines)
            return merged[:index] + element + merged[index:]
    raise ApplyError("labwc rc.xml has no supported root closing tag")


def _labwc_touch_block(current: str, dsi: str, touches: list[str]) -> str:
    begin = "<!-- BEGIN ed-config 3020 touch mapping -->"
    end = "<!-- END ed-config 3020 touch mapping -->"
    pattern = re.compile(re.escape(begin) + r"\n.*?\n" + re.escape(end) + r"\n?", re.DOTALL)
    current, _ = pattern.subn("", current)
    if begin in current or end in current:
        raise ApplyError("malformed ed-config Labwc touch mapping block")
    if not touches:
        return current
    block = begin + "\n" + "".join(
        f'    <touch deviceName="{touch}" mapToOutput="{dsi}" mouseEmulation="yes"/>\n'
        for touch in touches
    ) + end + "\n"
    for root_close in ("</openbox_config>", "</labwc_config>"):
        index = current.rfind(root_close)
        if index >= 0:
            return current[:index] + block + current[index:]
    raise ApplyError("labwc rc.xml has no supported root closing tag")


def _labwc_window_rule(current: str, enabled: bool) -> str:
    begin = "<!-- BEGIN ed-config 3020 LXTerminal rule -->"
    end = "<!-- END ed-config 3020 LXTerminal rule -->"
    pattern = re.compile(
        r"(?m)^[ \t]*" + re.escape(begin) + r"\n.*?^[ \t]*" + re.escape(end) + r"\n?",
        re.DOTALL,
    )
    current, _ = pattern.subn("", current)
    if begin in current or end in current:
        raise ApplyError("malformed ed-config Labwc LXTerminal rule block")
    if not enabled:
        return current
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


def _labwc_environment(current: str, enabled: bool) -> str:
    begin = "# BEGIN ed-config 3020 keyboard layout"
    end = "# END ed-config 3020 keyboard layout"
    if not enabled:
        lines = current.splitlines()
        if begin not in lines and end not in lines:
            return current
        if begin in lines:
            start = lines.index(begin)
            try:
                finish = lines.index(end, start + 1)
            except ValueError as exc:
                raise ApplyError("unterminated ed-config Labwc keyboard block") from exc
            del lines[start:finish + 1]
        elif end in lines:
            raise ApplyError("unmatched ed-config Labwc keyboard block marker")
        return "\n".join(lines) + ("\n" if lines else "")
    return _block(
        current,
        "XKB_DEFAULT_LAYOUT=gb",
        begin,
        end,
        None,
        None,
    )


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


def operations(root: Path, platform: str, profile: str | None) -> list[dict[str, object]]:
    if profile not in {None, "h101c", "hmi3020_070c", "hmi3020_101c", "ipc3020"}:
        raise ApplyError(f"unsupported desktop configuration profile: {profile}")
    has_screen = profile not in {None, "ipc3020"}
    dsi = "DSI-1" if platform.casefold() == "cm4" else "DSI-2"
    if profile == "hmi3020_070c":
        wayfire_touches = [
            "generic ft5x06 (79)",
            "generic ft5x06 (80)",
            "FT5406 memory based driver",
        ]
    elif has_screen:
        wayfire_touches = [
            "generic ft5x06 (79)",
            "generic ft5x06 (80)",
            "FT5406 memory based driver",
            "Goodix Capacitive TouchScreen",
            "10-0014 Goodix Capacitive TouchScreen",
        ]
        labwc_touches = [
            "Goodix Capacitive TouchScreen",
            "10-0014 Goodix Capacitive TouchScreen",
        ]
    else:
        wayfire_touches = []
        labwc_touches = []

    result: list[dict[str, object]] = []
    home_root = root / "home"
    if home_root.is_dir():
        for home in sorted(path for path in home_root.iterdir() if path.is_dir()):
            owner = home.stat()
            config = home / ".config"
            wayfire = config / "wayfire.ini"
            current_wayfire = _text(wayfire)
            cleanup_3020 = profile not in {"hmi3020_070c", "hmi3020_101c", "ipc3020"}
            had_3020_settings = any(
                marker in current_wayfire
                for marker in (
                    HMI3020_DISPLAY_BEGIN,
                    "# BEGIN ed-config 3020 desktop shortcuts",
                    "# BEGIN ed-config 3020 LXTerminal maximize",
                    "# BEGIN ed-config 3020 keyboard layout",
                )
            )
            if cleanup_3020 and had_3020_settings:
                if HMI3020_DISPLAY_BEGIN in current_wayfire:
                    current_wayfire = _remove_block(
                        current_wayfire, HMI3020_DISPLAY_BEGIN, HMI3020_DISPLAY_END
                    )
                current_wayfire = _managed_ini_section(current_wayfire, "command", "3020 desktop shortcuts", None)
                current_wayfire = _managed_ini_section(current_wayfire, "window-rules", "3020 LXTerminal maximize", None)
                current_wayfire = _managed_ini_section(current_wayfire, "input", "3020 keyboard layout", None)
            elif profile == "ipc3020" and HMI3020_DISPLAY_BEGIN in current_wayfire:
                current_wayfire = _remove_block(
                    current_wayfire, HMI3020_DISPLAY_BEGIN, HMI3020_DISPLAY_END
                )
            kanshi = config / "kanshi/config"
            current_kanshi = _text(kanshi)
            if profile not in {"hmi3020_070c", "hmi3020_101c"} and HMI3020_DISPLAY_BEGIN in current_kanshi:
                current_kanshi = _remove_block(
                    current_kanshi, HMI3020_DISPLAY_BEGIN, HMI3020_DISPLAY_END
                )
                result.append({"kind": "screen", "path": kanshi, "content": current_kanshi, "mode": 0o644, "uid": owner.st_uid, "gid": owner.st_gid})
            if has_screen:
                wayfire_body = (
                    f"[output:{dsi}]\nmode = 800x1280@40000\nposition = 0,0\ntransform = 90\nprimary = true\n"
                    + "\n".join(
                        f"[input-device:{touch}]\noutput = {dsi}" for touch in wayfire_touches
                    )
                )
                if profile in {"hmi3020_070c", "hmi3020_101c"}:
                    current_wayfire = _block(
                        current_wayfire,
                        wayfire_body,
                        HMI3020_DISPLAY_BEGIN,
                        HMI3020_DISPLAY_END,
                        None,
                        None,
                    )
                else:
                    current_wayfire = _block(current_wayfire, wayfire_body)

                kanshi_body = (
                    f"profile {{\n        output {dsi} mode 800x1280 position 0,0 transform 90\n}}\n"
                    f"profile {{\n        output {dsi} mode 800x1280 position 0,0 transform 90\n"
                    "        output HDMI-A-1 enable position 1280,0 transform normal\n}"
                )
                result.extend([
                    {"kind": "screen", "path": kanshi, "content": _block(current_kanshi, kanshi_body, HMI3020_DISPLAY_BEGIN, HMI3020_DISPLAY_END, None, None) if profile in {"hmi3020_070c", "hmi3020_101c"} else _block(current_kanshi, kanshi_body), "mode": 0o644, "uid": owner.st_uid, "gid": owner.st_gid},
                ])

            if profile in {"hmi3020_070c", "hmi3020_101c", "ipc3020"}:
                command_body = (
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
                current_wayfire = _managed_ini_section(
                    current_wayfire, "command", "3020 desktop shortcuts", command_body
                )
                rule_name = "3020 LXTerminal maximize"
                rule_key = _window_rule_key(current_wayfire, rule_name)
                current_wayfire = _managed_ini_section(
                    current_wayfire,
                    "window-rules",
                    rule_name,
                    f'{rule_key} = on created if title is "LXTerminal" then maximize',
                )
                current_wayfire = _managed_ini_section(
                    current_wayfire, "input", "3020 keyboard layout", "xkb_model = pc105\nxkb_layout = gb\nxkb_variant ="
                )

            if has_screen or profile in {"hmi3020_070c", "hmi3020_101c", "ipc3020"} or had_3020_settings:
                result.append({"kind": "screen", "path": wayfire, "content": current_wayfire, "mode": 0o644, "uid": owner.st_uid, "gid": owner.st_gid})
            if profile not in {"hmi3020_070c", "hmi3020_101c", "ipc3020"} and has_screen:
                labwc = config / "labwc/rc.xml"
                current_labwc = _text(labwc)
                labwc_base = _labwc_touch_block(current_labwc, dsi, []) if current_labwc else current_labwc
                labwc_content = _labwc(labwc_base, dsi, labwc_touches)
                labwc_content = _labwc_window_rule(labwc_content, False)
                result.append({"kind": "screen", "path": labwc, "content": labwc_content, "mode": 0o644, "uid": owner.st_uid, "gid": owner.st_gid})
            elif profile is None:
                labwc = config / "labwc/rc.xml"
                current_labwc = _text(labwc)
                cleaned_labwc = _labwc_touch_block(current_labwc, dsi, []) if current_labwc else current_labwc
                cleaned_labwc = _labwc_window_rule(cleaned_labwc, False) if cleaned_labwc else cleaned_labwc
                if current_labwc:
                    result.append({"kind": "screen", "path": labwc, "content": cleaned_labwc, "mode": 0o644, "uid": owner.st_uid, "gid": owner.st_gid})
                environment = config / "labwc/environment"
                current_environment = _text(environment)
                cleaned_environment = _labwc_environment(current_environment, False)
                if cleaned_environment != current_environment:
                    result.append({"kind": "screen", "path": environment, "content": cleaned_environment, "mode": 0o644, "uid": owner.st_uid, "gid": owner.st_gid})

    if has_screen:
        autostart = root / "etc/xdg/labwc/autostart"
        if autostart.is_file():
            result.append({"kind": "screen", "path": autostart, "content": _squeekboard(_text(autostart), dsi), "mode": None})
        if profile in {"h101c", "hmi3020_101c"}:
            result.append({"kind": "screen", "path": root / "usr/share/dispsetup.sh", "content": DISPLAY_SETUP, "mode": 0o755})
    return result


def _text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""
    except OSError as exc:
        raise ApplyError(f"cannot read {path}: {exc}") from exc
