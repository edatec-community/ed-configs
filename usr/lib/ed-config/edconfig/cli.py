from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .catalog import Catalog
from .errors import ConfigError
from .system import apply_plan, build_plan, read_status, record_apply_failure


def _json(value: Any, stream: Any = sys.stdout) -> None:
    print(json.dumps(value, indent=2, ensure_ascii=False), file=stream)


def _options(args: argparse.Namespace) -> list[str]:
    values: list[str] = []
    if args.wifi:
        values.append("wifi")
    if args.camera:
        values.append("camera")
    if args.lte:
        values.append(f"lte.{args.lte}")
    if args.supercapacitor:
        values.append("supercapacitor")
    return values


def _option_label(options: list[str]) -> str:
    labels = {
        "wifi": "Wi-Fi",
        "camera": "Camera",
        "lte.4g": "4G LTE",
        "lte.5g": "5G LTE",
        "supercapacitor": "Supercapacitor",
    }
    return ", ".join(labels.get(item, item) for item in options) or "None"


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        prog="ed-config",
        description="Configure an EDATEC device by product model.",
    )
    sub = result.add_subparsers(
        dest="command",
        required=True,
        metavar="{list,apply,status,version}",
    )

    listing = sub.add_parser("list", help="list all supported product models")
    listing.add_argument("-j", "--json", action="store_true", help="print machine-readable JSON")

    apply = sub.add_parser("apply", help="apply configuration for a product model")
    apply.add_argument("-m", "--model", required=True, help="product model, for example IPC2130")
    apply.add_argument("-w", "--wifi", action="store_true", help="enable Wi-Fi configuration")
    apply.add_argument("-c", "--camera", action="store_true", help="enable camera configuration")
    apply.add_argument(
        "-l",
        "--lte",
        choices=("4g", "5g"),
        help="specify the LTE hardware installed in the device",
    )
    apply.add_argument(
        "-s",
        "--supercapacitor",
        action="store_true",
        help="indicate that the device includes a supercapacitor",
    )
    apply.add_argument("-j", "--json", action="store_true", help="print machine-readable JSON")
    apply.add_argument("-r", "--root", type=Path, default=Path("/"), help=argparse.SUPPRESS)

    status = sub.add_parser("status", help="show the current and last apply status")
    status.add_argument("-j", "--json", action="store_true", help="print machine-readable JSON")
    status.add_argument("-r", "--root", type=Path, default=Path("/"), help=argparse.SUPPRESS)

    version = sub.add_parser("version", help="show the installed version")
    version.add_argument("-j", "--json", action="store_true", help="print machine-readable JSON")

    return result


def _print_apply(result: dict[str, Any]) -> None:
    if result["changed"]:
        print("Configuration applied successfully.\n")
    else:
        print("Configuration is already up to date.\n")
    print(f"Model:        {result['model']}")
    print(f"Product code: {result['product_code_hex']}")
    print(f"Options:      {_option_label(result['options'])}")
    if result["changed_files"]:
        print("\nUpdated files:")
        for path in result["changed_files"]:
            print(f"  {path}")
    if result["reboot_required"]:
        print("\nA reboot is required.")
    else:
        print("\nNo reboot is required.")


def _public_status(status: dict[str, Any]) -> dict[str, Any]:
    current = status.get("current")
    if isinstance(current, dict):
        current = {
            key: current.get(key)
            for key in ("model", "product_code", "options", "applied_at", "reboot_required")
        }
        if isinstance(current.get("product_code"), int):
            current["product_code_hex"] = f"0x{current['product_code']:08x}"
    last_apply = status.get("last_apply")
    if isinstance(last_apply, dict):
        last_apply = {
            key: last_apply.get(key)
            for key in ("success", "requested_model", "attempted_at", "error")
        }
    return {
        "configured": bool(status.get("configured")),
        "current": current,
        "last_apply": last_apply,
    }


def _print_status(value: dict[str, Any]) -> None:
    print("EDATEC configuration status\n")
    print(f"Configured:      {'Yes' if value['configured'] else 'No'}")
    current = value.get("current")
    if current:
        print(f"Current model:   {current['model']}")
        print(f"Product code:    {current['product_code_hex']}")
        print(f"Options:         {_option_label(current.get('options') or [])}")
        print(f"Applied at:      {current.get('applied_at') or 'Unknown'}")
        print(f"Reboot required: {'Yes' if current.get('reboot_required') else 'No'}")
    last = value.get("last_apply")
    if last is None:
        print("Last apply:      None")
    else:
        print(f"Last apply:      {'Successful' if last.get('success') else 'Failed'}")
        if not last.get("success"):
            print(f"Requested model: {last.get('requested_model') or 'Unknown'}")
            print(f"Failure:         {last.get('error') or 'Unknown error'}")
            print(f"Attempted at:    {last.get('attempted_at') or 'Unknown'}")


def main(argv: list[str] | None = None, program_dir: Path | None = None) -> int:
    program_dir = program_dir or Path(__file__).resolve().parents[1]
    arguments = list(argv) if argv is not None else sys.argv[1:]
    if arguments and arguments[0] == "_validate":
        json_output = arguments[1:] in ([], ["-j"], ["--json"])
        if not json_output:
            print("ed-config: usage: ed-config _validate [-j]", file=sys.stderr)
            return 2
        try:
            catalog = Catalog(program_dir / "catalog")
            value = {
                "valid": True,
                "models": len(catalog.models),
                "families": len(catalog.families),
                "options": sorted(catalog.options),
                "digest": catalog.digest,
            }
            if arguments[1:]:
                _json(value)
            else:
                print(
                    f"valid: {value['models']} models, "
                    f"{value['families']} configuration families"
                )
            return 0
        except (ConfigError, OSError) as exc:
            print(f"ed-config: {exc}", file=sys.stderr)
            return 2 if isinstance(exc, ConfigError) else 7

    args = parser().parse_args(arguments)
    try:
        catalog = Catalog(program_dir / "catalog")
        if args.command == "list":
            models = sorted((item["name"] for item in catalog.models), key=str.casefold)
            if args.json:
                _json({"models": models})
            else:
                print("Supported EDATEC models:\n")
                print("\n".join(models))
        elif args.command == "apply":
            resolution = catalog.resolve_model(args.model, _options(args))
            plan = build_plan(catalog, resolution, args.root)
            value = apply_plan(plan)
            if args.json:
                _json(value)
            else:
                _print_apply(value)
        elif args.command == "status":
            value = _public_status(read_status(args.root))
            if args.json:
                _json(value)
            else:
                _print_status(value)
        elif args.command == "version":
            value = {"version": __version__, "catalog_schema_version": 3}
            if args.json:
                _json(value)
            else:
                print(f"ed-config {__version__}")
        return 0
    except (ConfigError, OSError) as exc:
        if args.command == "apply":
            record_apply_failure(args.root, args.model, str(exc))
        if getattr(args, "json", False):
            _json({"success": False, "error": str(exc)}, stream=sys.stderr)
        else:
            print(f"ed-config: {exc}", file=sys.stderr)
        return 2 if isinstance(exc, ConfigError) else 7
