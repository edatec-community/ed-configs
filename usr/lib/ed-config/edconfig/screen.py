from __future__ import annotations

from pathlib import Path

from .errors import ApplyError


DISPLAY_BEGIN = "# BEGIN ed-config HMI101C display configuration"
DISPLAY_END = "# END ed-config HMI101C display configuration"
LEGACY_DISPLAY_BEGIN = "# BEGIN ed-tools HMI101C display configuration"
LEGACY_DISPLAY_END = "# END ed-tools HMI101C display configuration"


def _block(current: str, body: str) -> str:
    block = f"{DISPLAY_BEGIN}\n{body}\n{DISPLAY_END}"
    markers = (
        (DISPLAY_BEGIN, DISPLAY_END),
        (LEGACY_DISPLAY_BEGIN, LEGACY_DISPLAY_END),
    )
    found = next(((begin, end) for begin, end in markers if begin in current), None)
    if found:
        begin, end_marker = found
        start = current.find(begin)
        relative_end = current[start:].find(end_marker)
        if relative_end < 0:
            raise ApplyError("unterminated HMI101C display configuration block")
        end = start + relative_end + len(end_marker)
        result = current[:start] + block + current[end:]
    elif current.strip():
        result = current.rstrip() + "\n\n" + block + "\n"
    else:
        result = block + "\n"
    return result if result.endswith("\n") else result + "\n"


def _labwc(current: str, dsi: str, touch: str) -> str:
    lines = current.splitlines()
    found = False
    for index, line in enumerate(lines):
        if "<touch" not in line or "Goodix Capacitive TouchScreen" not in line:
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
    if found:
        return "\n".join(lines) + "\n"

    element = f'        <touch deviceName="{touch}" mapToOutput="{dsi}" mouseEmulation="yes"/>\n'
    if not current.strip():
        return (
            '<?xml version="1.0"?>\n'
            '<openbox_config xmlns="http://openbox.org/3.4/rc">\n'
            f"{element}</openbox_config>\n"
        )
    for closing in ("</openbox_config>", "</labwc_config>"):
        index = current.rfind(closing)
        if index >= 0:
            return current[:index] + element + current[index:]
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


def operations(root: Path, platform: str) -> list[dict[str, object]]:
    dsi = "DSI-1" if platform.casefold() == "cm4" else "DSI-2"
    i2c_device = "10-0014"
    result: list[dict[str, object]] = []
    home_root = root / "home"
    if home_root.is_dir():
        for home in sorted(path for path in home_root.iterdir() if path.is_dir()):
            owner = home.stat()
            config = home / ".config"
            wayfire = config / "wayfire.ini"
            kanshi = config / "kanshi/config"
            labwc = config / "labwc/rc.xml"
            touch = f"{i2c_device} Goodix Capacitive TouchScreen"
            wayfire_body = (
                f"[output:{dsi}]\nmode = 800x1280@40000\nposition = 0,0\ntransform = 90\nprimary = true\n\n"
                f"[input-device:generic ft5x06 (79)]\noutput = {dsi}\n\n"
                f"[input-device:generic ft5x06 (80)]\noutput = {dsi}\n\n"
                f"[input-device:FT5406 memory based driver]\noutput = {dsi}\n\n"
                f"[input-device:Goodix Capacitive TouchScreen]\noutput = {dsi}\n\n"
                f"[input-device:{touch}]\noutput = {dsi}"
            )
            kanshi_body = (
                f"profile {{\n        output {dsi} mode 800x1280 position 0,0 transform 90\n}}\n"
                f"profile {{\n        output {dsi} mode 800x1280 position 0,0 transform 90\n"
                "        output HDMI-A-1 enable position 1280,0 transform normal\n}"
            )
            result.extend([
                {"kind": "screen", "path": wayfire, "content": _block(_text(wayfire), wayfire_body), "mode": 0o644, "uid": owner.st_uid, "gid": owner.st_gid},
                {"kind": "screen", "path": kanshi, "content": _block(_text(kanshi), kanshi_body), "mode": 0o644, "uid": owner.st_uid, "gid": owner.st_gid},
                {"kind": "screen", "path": labwc, "content": _labwc(_text(labwc), dsi, touch), "mode": 0o644, "uid": owner.st_uid, "gid": owner.st_gid},
            ])
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
