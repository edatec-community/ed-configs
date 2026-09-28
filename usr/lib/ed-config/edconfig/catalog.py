from __future__ import annotations

import hashlib
import tomllib
from pathlib import Path
from typing import Any

from .errors import ConfigError


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as stream:
            value = tomllib.load(stream)
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ConfigError(f"{path} must contain a TOML table")
    return value


def _entries(document: dict[str, Any], key: str, path: Path) -> list[dict[str, Any]]:
    value = document.get(key)
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ConfigError(f"{path}: {key} must be an array of tables")
    return value


class Catalog:
    """Validated catalog of exact models and self-contained configuration families."""

    _FAMILY_FIELDS = {
        "format",
        "version",
        "id",
        "platform",
        "config_lines",
        "udev_rules",
        "disable_config_on_ubuntu",
        "desktop_config",
        "wireplumber_audio_format",
        "wireplumber_modern_file",
        "alsa_config_file",
        "alsa_state_file",
        "audio_packages",
    }
    _RULE_FIELDS = {"path", "content_file", "default_file", "ubuntu_file"}

    def __init__(self, directory: Path):
        self.directory = directory.resolve()
        self.manifest_path = self.directory / "catalog.toml"
        self.manifest = _read_toml(self.manifest_path)
        if (
            self.manifest.get("format") != "ed-config-catalog"
            or self.manifest.get("version") != 3
        ):
            raise ConfigError("unsupported catalog format or version")

        self.data_paths: set[Path] = {self.manifest_path}
        self.family_directories: dict[str, Path] = {}
        self.models_path, models = self._load_file("models_file")
        self.options_path, options = self._load_file("options_file")
        self.codes_path, self.codes = self._load_file("codes_file")
        for path, document in (
            (self.models_path, models),
            (self.options_path, options),
            (self.codes_path, self.codes),
        ):
            if document.get("version") != 1:
                raise ConfigError(f"{path}: unsupported version")

        self.models = _entries(models, "models", self.models_path)
        self.options = self._table(options, "options", self.options_path)
        self.families = self._load_families()
        self.legacy_config_lines = self.manifest.get("legacy_config_lines", [])
        self.model_index = self._entry_index(self.models, "name", "model")
        self.family_index = {
            family_id.casefold(): (family_id, family)
            for family_id, family in self.families.items()
        }
        self.option_index = {
            str(name).casefold(): (str(name), value) for name, value in self.options.items()
        }
        self._validate()

    def _manifest_location(self, field: str, kind: str) -> Path:
        raw = self.manifest.get(field)
        if not isinstance(raw, str) or not raw or Path(raw).is_absolute():
            raise ConfigError(f"catalog field {field} must be a relative {kind} path")
        path = (self.directory / raw).resolve()
        if self.directory not in path.parents:
            raise ConfigError(f"catalog field {field} escapes the catalog directory")
        return path

    def _load_file(self, field: str) -> tuple[Path, dict[str, Any]]:
        path = self._manifest_location(field, "file")
        self.data_paths.add(path)
        return path, _read_toml(path)

    def _load_families(self) -> dict[str, dict[str, Any]]:
        root = self._manifest_location("families_dir", "directory")
        if not root.is_dir():
            raise ConfigError(f"catalog family directory does not exist: {root}")
        result: dict[str, dict[str, Any]] = {}
        index: set[str] = set()
        children = sorted(
            (path for path in root.iterdir() if path.is_dir()),
            key=lambda path: path.name,
        )
        if not children:
            raise ConfigError(f"catalog family directory is empty: {root}")
        for package_dir in children:
            config_path = package_dir / "config.toml"
            if not config_path.is_file():
                raise ConfigError(f"family package {package_dir.name} has no config.toml")
            family = _read_toml(config_path)
            if family.get("format") != "ed-config-family" or family.get("version") != 1:
                raise ConfigError(f"{config_path}: unsupported format or version")
            family_id = family.get("id")
            if not isinstance(family_id, str) or not family_id:
                raise ConfigError(f"{config_path}: id must be a non-empty string")
            folded = family_id.casefold()
            if folded in index:
                raise ConfigError(f"duplicate family package id: {family_id}")
            index.add(folded)
            result[family_id] = family
            self.family_directories[folded] = package_dir.resolve()
            self.data_paths.add(config_path.resolve())
        return result

    @staticmethod
    def _table(
        document: dict[str, Any], key: str, path: Path
    ) -> dict[str, dict[str, Any]]:
        value = document.get(key, {})
        if not isinstance(value, dict) or not all(
            isinstance(item, dict) for item in value.values()
        ):
            raise ConfigError(f"{path}: {key} must be a table")
        return value

    @staticmethod
    def _entry_index(
        entries: list[dict[str, Any]], field: str, kind: str
    ) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for entry in entries:
            name = entry.get(field)
            if not isinstance(name, str) or not name:
                raise ConfigError(f"every {kind} must have a non-empty {field}")
            folded = name.casefold()
            if folded in result:
                raise ConfigError(f"duplicate {kind}: {name}")
            result[folded] = entry
        return result

    @property
    def digest(self) -> str:
        digest = hashlib.sha256()
        for path in sorted(self.data_paths, key=lambda item: str(item)):
            digest.update(path.relative_to(self.directory).as_posix().encode())
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
        return f"sha256:{digest.hexdigest()}"

    def _validate(self) -> None:
        self._validate_code_tables()
        if not isinstance(self.legacy_config_lines, list) or not all(
            isinstance(line, str) and line.strip() and "\n" not in line
            for line in self.legacy_config_lines
        ):
            raise ConfigError("catalog legacy_config_lines is invalid")
        for family_id, family in self.families.items():
            self._validate_family(family_id, family)
        self._validate_options()

        identities: set[tuple[str, str, str, str]] = set()
        product_codes: dict[int, str] = {}
        for model in self.models:
            identity = tuple(
                str(model.get(key, ""))
                for key in ("platform", "board", "subboard", "screen")
            )
            if not all(identity):
                raise ConfigError(f"model {model['name']} has an incomplete hardware identity")
            folded = tuple(item.casefold() for item in identity)
            if folded in identities:
                raise ConfigError(f"duplicate hardware identity: {identity}")
            identities.add(folded)
            for table, value in zip(
                ("platforms", "boards", "subboards", "screens"), identity
            ):
                self._code_value(table, value)
            self._family_for_model(model)
            product_code = self._identity_code(model, [])
            previous = product_codes.get(product_code)
            if previous is not None:
                raise ConfigError(
                    f"models {previous} and {model['name']} produce the same product code "
                    f"0x{product_code:08x}"
                )
            product_codes[product_code] = str(model["name"])

    def _validate_code_tables(self) -> None:
        specifications = {
            "platforms": 4,
            "lte": 2,
            "screens": 6,
            "boards": 9,
            "subboards": 7,
        }
        for table, width in specifications.items():
            mapping = self.codes.get(table)
            if not isinstance(mapping, dict) or not mapping:
                raise ConfigError(f"hardware code table {table} is missing or empty")
            used: dict[int, str] = {}
            for name, raw in mapping.items():
                if not isinstance(raw, int) or raw < 0 or raw >= (1 << width):
                    raise ConfigError(
                        f"hardware code {table}.{name} is outside its {width}-bit field"
                    )
                if raw in used:
                    raise ConfigError(
                        f"hardware codes {table}.{used[raw]} and {table}.{name} are duplicated"
                    )
                used[raw] = str(name)

    def _validate_family(self, family_id: str, family: dict[str, Any]) -> None:
        unknown = sorted(str(key) for key in family if key not in self._FAMILY_FIELDS)
        if unknown:
            raise ConfigError(
                f"family package {family_id} has unknown fields: {', '.join(unknown)}"
            )
        platform = family.get("platform")
        if not isinstance(platform, str) or not platform:
            raise ConfigError(f"family package {family_id} must select one platform")
        self._code_value("platforms", platform)
        lines = family.get("config_lines")
        if not isinstance(lines, list) or not all(
            isinstance(line, str) and line.strip() and "\n" not in line for line in lines
        ):
            raise ConfigError(f"family package {family_id} has invalid config_lines")
        self._validate_config_lines(family_id, lines)
        disable = family.get("disable_config_on_ubuntu", False)
        if not isinstance(disable, bool):
            raise ConfigError(
                f"family package {family_id} has invalid disable_config_on_ubuntu"
            )
        desktop = family.get("desktop_config")
        if desktop is not None and not isinstance(desktop, str):
            raise ConfigError(f"family package {family_id} has invalid desktop_config")
        if "wireplumber_audio_format" in family:
            if family["wireplumber_audio_format"] != "S16LE":
                raise ConfigError(
                    f"family package {family_id} wireplumber_audio_format must be S16LE"
                )
        for field in ("alsa_config_file", "alsa_state_file"):
            if field in family:
                self._rule_file(
                    self.family_directories[family_id.casefold()],
                    family[field],
                    f"family package {family_id} {field}",
                )
        if "wireplumber_modern_file" in family:
            self._rule_file(
                self.family_directories[family_id.casefold()],
                family["wireplumber_modern_file"],
                f"family package {family_id} wireplumber_modern_file",
            )
        if "audio_packages" in family:
            packages = family["audio_packages"]
            if not isinstance(packages, list) or not packages or not all(
                isinstance(item, str) and item and not Path(item).is_absolute()
                for item in packages
            ):
                raise ConfigError(
                    f"family package {family_id} audio_packages must be non-empty relative paths"
                )
            for package in packages:
                self._rule_file(
                    self.family_directories[family_id.casefold()],
                    package,
                    f"family package {family_id} audio package",
                )
        rules = family.get("udev_rules", [])
        if not isinstance(rules, list):
            raise ConfigError(f"family package {family_id} udev_rules must be an array")
        targets: set[str] = set()
        for rule in rules:
            self._validate_rule(family_id, rule, targets)

    def _validate_rule(
        self, family_id: str, rule: Any, targets: set[str]
    ) -> None:
        if not isinstance(rule, dict):
            raise ConfigError(f"family package {family_id} contains an invalid udev rule")
        unknown = sorted(str(key) for key in rule if key not in self._RULE_FIELDS)
        if unknown:
            raise ConfigError(
                f"family package {family_id} udev rule has unknown fields: {', '.join(unknown)}"
            )
        target = rule.get("path")
        if not isinstance(target, str) or Path(target).parent != Path("/etc/udev/rules.d"):
            raise ConfigError(f"unsafe udev rule path: {target!r}")
        if target in targets:
            raise ConfigError(f"family package {family_id} duplicates udev rule {target}")
        targets.add(target)
        has_content = "content_file" in rule
        has_default = "default_file" in rule
        if has_content == has_default or ("ubuntu_file" in rule and not has_default):
            raise ConfigError(f"family package {family_id} has invalid files for {target}")
        package_dir = self.family_directories[family_id.casefold()]
        for field in ("content_file", "default_file", "ubuntu_file"):
            if field in rule:
                self._rule_file(package_dir, rule[field], f"family package {family_id} {field}")

    def _rule_file(self, package_dir: Path, raw: Any, owner: str) -> Path:
        if not isinstance(raw, str) or not raw or Path(raw).is_absolute():
            raise ConfigError(f"{owner} must be a relative file path")
        path = (package_dir / raw).resolve()
        if package_dir not in path.parents or not path.is_file():
            raise ConfigError(f"{owner} is missing or escapes its package: {raw}")
        self.data_paths.add(path)
        return path

    def _materialize_rule(
        self, family_id: str, rule: dict[str, Any]
    ) -> dict[str, Any]:
        package_dir = self.family_directories[family_id.casefold()]
        result: dict[str, Any] = {"path": str(rule["path"])}
        for source_field, content_field in (
            ("content_file", "content"),
            ("default_file", "default_content"),
            ("ubuntu_file", "ubuntu_content"),
        ):
            if source_field in rule:
                path = self._rule_file(
                    package_dir, rule[source_field], f"family package {family_id}"
                )
                try:
                    result[content_field] = path.read_text(encoding="utf-8")
                except OSError as exc:
                    raise ConfigError(f"cannot read {path}: {exc}") from exc
        return result

    def _validate_options(self) -> None:
        for name, option in self.options.items():
            has_bit = "code_bit" in option
            has_field = all(
                key in option for key in ("code_shift", "code_width", "code_value")
            )
            if has_bit == has_field:
                raise ConfigError(
                    f"option {name} must define exactly one code bit or field"
                )
            if has_bit and not 0 <= int(option["code_bit"]) <= 31:
                raise ConfigError(f"option {name} has an invalid code bit")
            if has_field:
                width, value = int(option["code_width"]), int(option["code_value"])
                if width <= 0 or value < 0 or value >= (1 << width):
                    raise ConfigError(f"option {name} has an invalid code field")
            for key in ("config_lines", "cm4_config_lines", "cm5_config_lines"):
                lines = option.get(key, [])
                if not isinstance(lines, list) or not all(
                    isinstance(line, str) and line.strip() and "\n" not in line
                    for line in lines
                ):
                    raise ConfigError(f"option {name} has invalid {key}")

        option_masks: list[tuple[str, int, str | None]] = []
        for name, option in self.options.items():
            if "code_bit" in option:
                mask = 1 << int(option["code_bit"])
            else:
                mask = ((1 << int(option["code_width"])) - 1) << int(
                    option["code_shift"]
                )
            if mask & (1 << 31):
                raise ConfigError(f"option {name} uses reserved product-code bit 31")
            for previous_name, previous_mask, previous_group in option_masks:
                if mask & previous_mask and (
                    not option.get("exclusive_group")
                    or option.get("exclusive_group") != previous_group
                    or mask != previous_mask
                ):
                    raise ConfigError(
                        f"options {previous_name} and {name} have overlapping code fields"
                    )
            option_masks.append((name, mask, option.get("exclusive_group")))

    def _code_value(self, table: str, name: str) -> int:
        mapping = self.codes.get(table)
        if not isinstance(mapping, dict):
            raise ConfigError(f"hardware code table {table} is missing")
        for key, value in mapping.items():
            if str(key).casefold() == name.casefold():
                return int(value)
        raise ConfigError(f"unknown {table} value: {name}")

    def _identity_code(self, model: dict[str, Any], option_names: list[str]) -> int:
        code = (
            self._code_value("platforms", str(model["platform"]))
            | self._code_value("screens", str(model["screen"])) << 7
            | self._code_value("boards", str(model["board"])) << 13
            | self._code_value("subboards", str(model["subboard"])) << 22
        )
        groups: dict[str, str] = {}
        for canonical in option_names:
            option = self.options[canonical]
            group = option.get("exclusive_group")
            if group:
                if str(group) in groups:
                    raise ConfigError(
                        f"options {groups[str(group)]} and {canonical} conflict"
                    )
                groups[str(group)] = canonical
            if "code_bit" in option:
                code |= 1 << int(option["code_bit"])
            else:
                shift, width, value = (
                    int(option[key])
                    for key in ("code_shift", "code_width", "code_value")
                )
                mask = ((1 << width) - 1) << shift
                if code & mask:
                    raise ConfigError(
                        f"option {canonical} overlaps an existing product-code field"
                    )
                code |= value << shift
        return code

    def canonical_options(self, names: list[str]) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for raw in names:
            key = raw.strip().casefold()
            if not key:
                continue
            found = self.option_index.get(key)
            if found is None:
                raise ConfigError(f"unknown option: {raw}")
            canonical = found[0]
            if canonical.casefold() not in seen:
                result.append(canonical)
                seen.add(canonical.casefold())
        return sorted(result, key=str.casefold)

    def _family_for_model(
        self, model: dict[str, Any]
    ) -> tuple[str, dict[str, Any]]:
        requested = model.get("config")
        if not isinstance(requested, str) or not requested:
            raise ConfigError(f"model {model['name']} has no configuration family")
        found = self.family_index.get(requested.casefold())
        if found is None:
            raise ConfigError(
                f"model {model['name']} references unknown configuration family {requested}"
            )
        family_id, family = found
        if str(family["platform"]).casefold() != str(model["platform"]).casefold():
            raise ConfigError(
                f"model {model['name']} and family {family_id} select different platforms"
            )
        return family_id, family

    @staticmethod
    def _validate_config_lines(owner: str, lines: list[str]) -> None:
        scalar: dict[str, str] = {}
        for line in lines:
            if line.startswith("dtoverlay=") or "=" not in line:
                continue
            key, remainder = line.split("=", 1)
            if key == "dtparam" and "=" in remainder:
                key = f"dtparam:{remainder.split('=', 1)[0]}"
            elif key == "gpio" and "=" in remainder:
                key = f"gpio:{remainder.split('=', 1)[0]}"
            previous = scalar.get(key)
            if previous is not None and previous != line:
                raise ConfigError(
                    f"{owner} has conflicting config values: {previous!r} and {line!r}"
                )
            scalar[key] = line

    def resolve(self, requested: str, option_names: list[str]) -> dict[str, Any]:
        model = self.model_index.get(requested.casefold())
        if model is None:
            raise ConfigError(f"unknown model: {requested}")
        family_id, family = self._family_for_model(model)
        options = self.canonical_options(option_names)
        lines = [
            {"order": index + 10, "value": str(value)}
            for index, value in enumerate(family["config_lines"])
        ]
        values = {item["value"] for item in lines}
        next_order = max((item["order"] for item in lines), default=0) + 10
        for name in options:
            option = self.options[name]
            option_lines = list(option.get("config_lines", []))
            option_lines += list(
                option.get(f"{str(model['platform']).casefold()}_config_lines", [])
            )
            for line in option_lines:
                if line not in values:
                    lines.append({"order": next_order, "value": line})
                    values.add(line)
                    next_order += 1
        self._validate_config_lines(str(model["name"]), [item["value"] for item in lines])
        rules = [
            self._materialize_rule(family_id, rule)
            for rule in family.get("udev_rules", [])
        ]
        rules.sort(key=lambda rule: str(rule["path"]))
        package_dir = self.family_directories[family_id.casefold()]

        def family_text(field: str) -> str | None:
            raw = family.get(field)
            if raw is None:
                return None
            try:
                return (package_dir / str(raw)).read_text(encoding="utf-8")
            except OSError as exc:
                raise ConfigError(f"cannot read {field} for family {family_id}: {exc}") from exc

        def family_path_list(field: str) -> list[str]:
            values = family.get(field, [])
            result: list[str] = []
            for raw in values:
                path = self._rule_file(
                    package_dir, raw, f"family package {family_id} {field}"
                )
                result.append(str(path))
            return result

        return {
            "format": "ed-config-resolution",
            "schema_version": 3,
            "catalog_digest": self.digest,
            "requested": requested,
            "model": model["name"],
            "identity": {
                key: model[key] for key in ("platform", "board", "subboard", "screen")
            },
            "options": options,
            "product_code": self._identity_code(model, options),
            "disable_config_on_ubuntu": bool(
                family.get("disable_config_on_ubuntu", False)
            ),
            "screen_config": family.get("desktop_config"),
            "wireplumber_audio_format": family.get("wireplumber_audio_format"),
            "wireplumber_modern": family_text("wireplumber_modern_file"),
            "alsa_config": family_text("alsa_config_file"),
            "alsa_state": family_text("alsa_state_file"),
            "audio_packages": family_path_list("audio_packages"),
            "config_lines": lines,
            "rules": rules,
        }

    def resolve_model(self, requested: str, option_names: list[str]) -> dict[str, Any]:
        model = self.model_index.get(requested.casefold())
        if model is None:
            raise ConfigError(
                f"unsupported product model: {requested}; "
                "run 'ed-config list' to see supported models"
            )
        return self.resolve(str(model["name"]), option_names)

    def known_config_lines(self) -> set[str]:
        result = {str(line) for line in self.legacy_config_lines}
        for family in self.families.values():
            result.update(str(line) for line in family.get("config_lines", []))
        for option in self.options.values():
            for key in ("config_lines", "cm4_config_lines", "cm5_config_lines"):
                result.update(str(line) for line in option.get(key, []))
        return result

    def known_rule_paths(self) -> set[str]:
        return {
            str(rule["path"])
            for family in self.families.values()
            for rule in family.get("udev_rules", [])
        }

    def has_wireplumber_config(self) -> bool:
        return any("wireplumber_audio_format" in family for family in self.families.values())

    def has_alsa_config(self) -> bool:
        return any("alsa_config_file" in family for family in self.families.values())

    def model_summary(self) -> list[dict[str, Any]]:
        return [
            {
                "model": model["name"],
                "product_code": f"0x{self._identity_code(model, []):08x}",
            }
            for model in self.models
        ]
