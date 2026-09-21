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
    """Validated product profiles, exact model identities and features."""

    def __init__(self, directory: Path):
        self.directory = directory.resolve()
        self.manifest_path = self.directory / "catalog.toml"
        self.manifest = _read_toml(self.manifest_path)
        if self.manifest.get("format") != "ed-config-catalog" or self.manifest.get("version") != 1:
            raise ConfigError("unsupported catalog format or version")

        def load(field: str) -> tuple[Path, dict[str, Any]]:
            raw = self.manifest.get(field)
            if not isinstance(raw, str) or not raw or Path(raw).is_absolute():
                raise ConfigError(f"catalog field {field} must be a relative path")
            path = (self.directory / raw).resolve()
            if self.directory not in path.parents:
                raise ConfigError(f"catalog field {field} escapes the catalog directory")
            return path, _read_toml(path)

        self.profiles_path, profiles = load("profiles_file")
        self.models_path, models = load("models_file")
        self.components_path, self.components_document = load("components_file")
        self.options_path, self.options_document = load("options_file")
        self.codes_path, self.codes = load("codes_file")
        for path, document in (
            (self.profiles_path, profiles),
            (self.models_path, models),
            (self.components_path, self.components_document),
            (self.options_path, self.options_document),
            (self.codes_path, self.codes),
        ):
            if document.get("version") != 1:
                raise ConfigError(f"{path}: unsupported version")

        self.profiles = _entries(profiles, "profiles", self.profiles_path)
        self.models = _entries(models, "models", self.models_path)
        self.components = self._table(self.components_document, "components", self.components_path)
        self.rule_profiles = self._table(self.components_document, "rule_profiles", self.components_path)
        self.options = self._table(self.options_document, "options", self.options_path)
        self.profile_index = self._index(self.profiles, "id", "profile")
        self.model_index = self._index(self.models, "name", "model")
        self.option_index = {str(name).casefold(): (str(name), value) for name, value in self.options.items()}
        self._validate()

    @staticmethod
    def _table(document: dict[str, Any], key: str, path: Path) -> dict[str, dict[str, Any]]:
        value = document.get(key, {})
        if not isinstance(value, dict) or not all(isinstance(item, dict) for item in value.values()):
            raise ConfigError(f"{path}: {key} must be a table")
        return value

    @staticmethod
    def _index(entries: list[dict[str, Any]], field: str, kind: str) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for entry in entries:
            name = entry.get(field)
            if not isinstance(name, str) or not name:
                raise ConfigError(f"every {kind} must have a non-empty {field}")
            key = name.casefold()
            if key in result:
                raise ConfigError(f"duplicate {kind}: {name}")
            result[key] = entry
        return result

    @property
    def digest(self) -> str:
        digest = hashlib.sha256()
        paths = sorted(
            [self.manifest_path, self.profiles_path, self.models_path, self.components_path,
             self.options_path, self.codes_path],
            key=lambda item: str(item),
        )
        for path in paths:
            digest.update(path.relative_to(self.directory).as_posix().encode())
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
        return f"sha256:{digest.hexdigest()}"

    def _validate(self) -> None:
        self._validate_code_tables()
        identities: set[tuple[str, str, str, str]] = set()
        product_codes: dict[int, str] = {}
        for model in self.models:
            profile_name = model.get("profile")
            if not isinstance(profile_name, str) or profile_name.casefold() not in self.profile_index:
                raise ConfigError(f"model {model['name']} references unknown profile {profile_name!r}")
            identity = tuple(str(model.get(key, "")) for key in ("platform", "board", "subboard", "screen"))
            if not all(identity):
                raise ConfigError(f"model {model['name']} has an incomplete hardware identity")
            folded = tuple(item.casefold() for item in identity)
            if folded in identities:
                raise ConfigError(f"duplicate hardware identity: {identity}")
            identities.add(folded)
            product_code = self._identity_code(model, [])
            previous = product_codes.get(product_code)
            if previous is not None:
                raise ConfigError(
                    f"models {previous} and {model['name']} produce the same product code "
                    f"0x{product_code:08x}"
                )
            product_codes[product_code] = str(model["name"])

        for profile in self.profiles:
            default = profile.get("default_model")
            model = self.model_index.get(str(default).casefold())
            if model is None:
                raise ConfigError(f"profile {profile['id']} has unknown default model {default!r}")
            if str(model.get("profile", "")).casefold() != str(profile["id"]).casefold():
                raise ConfigError(f"profile {profile['id']} default model belongs to another profile")
            components = profile.get("components")
            if not isinstance(components, list) or not components:
                raise ConfigError(f"profile {profile['id']} must contain components")
            if len(set(components)) != len(components):
                raise ConfigError(f"profile {profile['id']} contains duplicate components")
            self._resolve_components(profile)

        for name, option in self.options.items():
            has_bit = "code_bit" in option
            has_field = all(key in option for key in ("code_shift", "code_width", "code_value"))
            if has_bit == has_field:
                raise ConfigError(f"option {name} must define exactly one code bit or field")
            if has_bit and not 0 <= int(option["code_bit"]) <= 31:
                raise ConfigError(f"option {name} has an invalid code bit")
            if has_field:
                width, value = int(option["code_width"]), int(option["code_value"])
                if width <= 0 or value < 0 or value >= (1 << width):
                    raise ConfigError(f"option {name} has an invalid code field")
            for key in ("config_lines", "cm4_config_lines", "cm5_config_lines"):
                lines = option.get(key, [])
                if not isinstance(lines, list) or not all(
                    isinstance(line, str) and line.strip() and "\n" not in line for line in lines
                ):
                    raise ConfigError(f"option {name} has invalid {key}")

        option_masks: list[tuple[str, int, str | None]] = []
        for name, option in self.options.items():
            if "code_bit" in option:
                mask = 1 << int(option["code_bit"])
            else:
                mask = ((1 << int(option["code_width"])) - 1) << int(option["code_shift"])
            if mask & (1 << 31):
                raise ConfigError(f"option {name} uses reserved product-code bit 31")
            for previous_name, previous_mask, previous_group in option_masks:
                if mask & previous_mask and (
                    not option.get("exclusive_group")
                    or option.get("exclusive_group") != previous_group
                    or mask != previous_mask
                ):
                    raise ConfigError(f"options {previous_name} and {name} have overlapping code fields")
            option_masks.append((name, mask, option.get("exclusive_group")))

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
                    raise ConfigError(f"hardware code {table}.{name} is outside its {width}-bit field")
                if raw in used:
                    raise ConfigError(f"hardware codes {table}.{used[raw]} and {table}.{name} are duplicated")
                used[raw] = str(name)

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
                    raise ConfigError(f"options {groups[str(group)]} and {canonical} conflict")
                groups[str(group)] = canonical
            if "code_bit" in option:
                code |= 1 << int(option["code_bit"])
            else:
                shift, width, value = (int(option[key]) for key in ("code_shift", "code_width", "code_value"))
                mask = ((1 << width) - 1) << shift
                if code & mask:
                    raise ConfigError(f"option {canonical} overlaps an existing product-code field")
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

    def select(self, requested: str) -> tuple[dict[str, Any], dict[str, Any]]:
        model = self.model_index.get(requested.casefold())
        if model is not None:
            return self.profile_index[str(model["profile"]).casefold()], model
        profile = self.profile_index.get(requested.casefold())
        if profile is None:
            raise ConfigError(f"unknown model or profile: {requested}")
        return profile, self.model_index[str(profile["default_model"]).casefold()]

    def _resolve_components(self, profile: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        sequence = 0
        pending: list[tuple[int, int, str]] = []
        rules: dict[str, dict[str, Any]] = {}
        for component_name in profile["components"]:
            component = self.components.get(component_name)
            if component is None:
                raise ConfigError(f"profile {profile['id']} references unknown component {component_name}")
            blocks = component.get("config_blocks", [])
            if not isinstance(blocks, list):
                raise ConfigError(f"component {component_name} config_blocks must be an array")
            for block in blocks:
                if not isinstance(block, dict) or not isinstance(block.get("lines", []), list):
                    raise ConfigError(f"component {component_name} has an invalid config block")
                order = int(block.get("order", 0))
                for line in block.get("lines", []):
                    if not isinstance(line, str) or not line.strip() or "\n" in line:
                        raise ConfigError(f"component {component_name} contains an invalid config line")
                    pending.append((order, sequence, line))
                    sequence += 1
            for rule_name in component.get("rule_profiles", []):
                rule_profile = self.rule_profiles.get(rule_name)
                if rule_profile is None:
                    raise ConfigError(f"component {component_name} references unknown rule profile {rule_name}")
                files = rule_profile.get("files", [])
                if not isinstance(files, list):
                    raise ConfigError(f"rule profile {rule_name} files must be an array")
                for rule in files:
                    if not isinstance(rule, dict):
                        raise ConfigError(f"rule profile {rule_name} contains an invalid file")
                    path = rule.get("path")
                    if not isinstance(path, str) or not path.startswith("/etc/udev/rules.d/") or ".." in Path(path).parts:
                        raise ConfigError(f"unsafe udev rule path: {path!r}")
                    old = rules.get(path)
                    if old is not None and old != rule:
                        raise ConfigError(f"conflicting udev rules at {path}")
                    rules[path] = dict(rule)
        lines: list[dict[str, Any]] = []
        seen: set[str] = set()
        for order, _, line in sorted(pending):
            if line not in seen:
                lines.append({"order": order, "value": line})
                seen.add(line)
        if not lines:
            raise ConfigError(f"profile {profile['id']} resolves to no config lines")
        self._validate_config_lines(str(profile["id"]), [item["value"] for item in lines])
        return lines, [rules[path] for path in sorted(rules)]

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
                raise ConfigError(f"{owner} has conflicting config values: {previous!r} and {line!r}")
            scalar[key] = line

    def resolve(self, requested: str, option_names: list[str]) -> dict[str, Any]:
        profile, model = self.select(requested)
        options = self.canonical_options(option_names)
        lines, rules = self._resolve_components(profile)
        values = {item["value"] for item in lines}
        next_order = max(item["order"] for item in lines) + 10
        for name in options:
            option = self.options[name]
            option_lines = list(option.get("config_lines", []))
            option_lines += list(option.get(f"{str(model['platform']).casefold()}_config_lines", []))
            for line in option_lines:
                if line not in values:
                    lines.append({"order": next_order, "value": line})
                    values.add(line)
                    next_order += 1
        self._validate_config_lines(str(profile["id"]), [item["value"] for item in lines])
        return {
            "format": "ed-config-resolution",
            "schema_version": 1,
            "catalog_digest": self.digest,
            "requested": requested,
            "profile": profile["id"],
            "model": model["name"],
            "identity": {key: model[key] for key in ("platform", "board", "subboard", "screen")},
            "options": options,
            "product_code": self._identity_code(model, options),
            "disable_config_on_ubuntu": bool(profile.get("disable_config_on_ubuntu", False)),
            "screen_config": profile.get("screen_config"),
            "config_lines": lines,
            "rules": rules,
        }

    def resolve_model(self, requested: str, option_names: list[str]) -> dict[str, Any]:
        model = self.model_index.get(requested.casefold())
        if model is None:
            raise ConfigError(
                f"unsupported product model: {requested}; run 'ed-config list' to see supported models"
            )
        return self.resolve(str(model["name"]), option_names)

    def known_config_lines(self) -> set[str]:
        result: set[str] = set()
        for component in self.components.values():
            for block in component.get("config_blocks", []):
                result.update(str(line) for line in block.get("lines", []))
        for option in self.options.values():
            for key in ("config_lines", "cm4_config_lines", "cm5_config_lines"):
                result.update(str(line) for line in option.get(key, []))
        return result

    def known_rule_paths(self) -> set[str]:
        return {
            str(rule["path"])
            for profile in self.profiles
            for rule in self._resolve_components(profile)[1]
        }

    def profile_summary(self) -> list[dict[str, Any]]:
        return [
            {"profile": profile["id"], "default_model": profile["default_model"]}
            for profile in self.profiles
        ]

    def model_summary(self) -> list[dict[str, Any]]:
        return [
            {
                "model": model["name"],
                "profile": model["profile"],
                "product_code": f"0x{self._identity_code(model, []):08x}",
            }
            for model in self.models
        ]
