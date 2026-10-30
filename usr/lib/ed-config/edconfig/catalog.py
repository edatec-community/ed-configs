from __future__ import annotations

import hashlib
import re
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
    _FAMILY_FIELDS = {
        "format",
        "version",
        "id",
        "platform",
        "config_lines",
        "udev_rules",
        "managed_files",
        "systemd_services",
        "systemd_service_links",
        "option_config_lines",
        "unsupported_options",
        "desktop_config",
        "goodix_touch_devices",
        "wireplumber_modern_file",
        "wireplumber_modern_target",
        "alsa_config_file",
        "alsa_config_target",
        "alsa_state_file",
        "alsa_state_target",
        "alsa_state_blocks",
        "alsa_restore_skip_ucm_blocks",
        "wireplumber_legacy_hashes",
        "alsa_profile_set_file",
        "alsa_profile_set_target",
        "alsa_profile_path_files",
        "alsa_profile_path_targets",
        "audio_packages",
    }
    _RULE_FIELDS = {"path", "content_file", "default_file", "ubuntu_file", "exact_content"}
    _MANAGED_FILE_FIELDS = {"path", "content_file", "mode", "option"}
    _MANAGED_FILE_DIRECTORIES = {"/usr/bin", "/usr/lib/systemd/system"}

    def __init__(self, directory: Path):
        self.directory = directory.resolve()
        self.manifest_path = self.directory / "catalog.toml"
        self.manifest = _read_toml(self.manifest_path)
        if (
            self.manifest.get("format") != "ed-config-catalog"
            or self.manifest.get("version") != 3
        ):
            raise ConfigError("unsupported catalog format or version")

        common = self.manifest.get("common")
        if not isinstance(common, dict):
            raise ConfigError("catalog common must be a table")
        disabled_lines = common.get("ubuntu_disabled_lines")
        if (
            not isinstance(disabled_lines, list)
            or not disabled_lines
            or any(not isinstance(line, str) or not line or "\n" in line for line in disabled_lines)
            or len(set(disabled_lines)) != len(disabled_lines)
        ):
            raise ConfigError("catalog common has invalid ubuntu_disabled_lines")
        self.ubuntu_disabled_lines = tuple(disabled_lines)
        self._validate_config_lines("catalog common ubuntu_disabled_lines", disabled_lines)

        self.data_paths: set[Path] = {self.manifest_path}
        self.family_directories: dict[str, Path] = {}
        self.systemd_service_targets: dict[str, str] = {}
        self.systemd_service_links: dict[str, str] = {}
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
        path_fields = {
            "wireplumber_modern_target": "/etc/wireplumber/wireplumber.conf.d",
            "alsa_config_target": "/etc",
            "alsa_state_target": "/var/lib/alsa",
            "alsa_profile_set_target": "/etc/alsa-card-profile/mixer/profile-sets",
        }
        for field, directory in path_fields.items():
            target = family.get(field)
            if target is not None and (
                not isinstance(target, str)
                or Path(target).parent.as_posix() != directory
                or Path(target).name in {"", ".", ".."}
            ):
                raise ConfigError(f"family package {family_id} has unsafe {field}: {target!r}")
        for source_field, target_fields in {
            "wireplumber_modern_file": ("wireplumber_modern_target",),
            "alsa_config_file": ("alsa_config_target",),
            "alsa_state_file": ("alsa_state_target",),
            "alsa_profile_set_file": ("alsa_profile_set_target",),
        }.items():
            if source_field in family and any(field not in family for field in target_fields):
                raise ConfigError(f"family package {family_id} is missing target for {source_field}")
        profile_targets = family.get("alsa_profile_path_targets", [])
        profile_files = family.get("alsa_profile_path_files", [])
        if len(profile_targets) != len(profile_files) or any(
            not isinstance(path, str)
            or Path(path).parent.as_posix() != "/etc/alsa-card-profile/mixer/paths"
            or Path(path).name in {"", ".", ".."}
            for path in profile_targets
        ):
            raise ConfigError(f"family package {family_id} has invalid alsa_profile_path_targets")
        lines = family.get("config_lines")
        if not isinstance(lines, list) or not all(
            isinstance(line, str) and line.strip() and "\n" not in line for line in lines
        ):
            raise ConfigError(f"family package {family_id} has invalid config_lines")
        self._validate_config_lines(family_id, lines)
        option_names = {str(name).casefold() for name in self.options}
        option_config_lines = family.get("option_config_lines", {})
        if not isinstance(option_config_lines, dict):
            raise ConfigError(
                f"family package {family_id} option_config_lines must be a table"
            )
        for option_name, option_lines in option_config_lines.items():
            if str(option_name).casefold() not in option_names:
                raise ConfigError(
                    f"family package {family_id} references unknown option {option_name}"
                )
            if not isinstance(option_lines, list) or not all(
                isinstance(line, str) and line.strip() and "\n" not in line
                for line in option_lines
            ):
                raise ConfigError(
                    f"family package {family_id} has invalid config lines for {option_name}"
                )
            self._validate_config_lines(f"{family_id}/{option_name}", option_lines)
        unsupported_options = family.get("unsupported_options", [])
        if (
            not isinstance(unsupported_options, list)
            or any(
                not isinstance(option, str)
                or option.casefold() not in option_names
                for option in unsupported_options
            )
            or len({option.casefold() for option in unsupported_options})
            != len(unsupported_options)
        ):
            raise ConfigError(f"family package {family_id} has invalid unsupported_options")
        desktop = family.get("desktop_config")
        if desktop is not None and desktop != "h101c":
            raise ConfigError(f"family package {family_id} has invalid desktop_config")
        goodix_touch_devices = family.get("goodix_touch_devices", [])
        if (
            not isinstance(goodix_touch_devices, list)
            or any(
                not isinstance(device, str)
                or not device.strip()
                or any(character in device for character in '\n\r"<>')
                for device in goodix_touch_devices
            )
            or len(set(goodix_touch_devices)) != len(goodix_touch_devices)
        ):
            raise ConfigError(
                f"family package {family_id} has invalid goodix_touch_devices"
            )
        for field in ("alsa_config_file", "alsa_state_file"):
            if field in family:
                self._rule_file(
                    self.family_directories[family_id.casefold()],
                    family[field],
                    f"family package {family_id} {field}",
                )
        if "alsa_state_blocks" in family:
            blocks = family["alsa_state_blocks"]
            valid_blocks = isinstance(blocks, list) and bool(blocks) and all(
                isinstance(block, str)
                and block
                and not any(character.isspace() for character in block)
                and "{" not in block
                and "}" not in block
                for block in blocks
            )
            if not valid_blocks or len(set(blocks)) != len(blocks):
                raise ConfigError(
                    f"family package {family_id} has invalid alsa_state_blocks"
                )
        if "alsa_state_blocks" in family and "alsa_state_file" not in family:
            raise ConfigError(
                f"family package {family_id} has alsa_state_blocks without alsa_state_file"
            )
        if "alsa_state_file" in family and "alsa_state_blocks" not in family:
            raise ConfigError(
                f"family package {family_id} is missing alsa_state_blocks"
            )
        if not set(family.get("alsa_restore_skip_ucm_blocks", [])) <= set(family.get("alsa_state_blocks", [])):
            raise ConfigError(f"family package {family_id} has invalid alsa_restore_skip_ucm_blocks")
        hashes = family.get("wireplumber_legacy_hashes", [])
        if not isinstance(hashes, list) or any(
            not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value)
            for value in hashes
        ):
            raise ConfigError(f"family package {family_id} has invalid wireplumber_legacy_hashes")
        if "alsa_profile_set_file" in family:
            self._rule_file(
                self.family_directories[family_id.casefold()],
                family["alsa_profile_set_file"],
                f"family package {family_id} alsa_profile_set_file",
            )
        if "alsa_profile_path_files" in family:
            paths = family["alsa_profile_path_files"]
            if not isinstance(paths, list) or not paths or not all(
                isinstance(item, str) and item and not Path(item).is_absolute()
                for item in paths
            ):
                raise ConfigError(
                    f"family package {family_id} alsa_profile_path_files must be non-empty relative paths"
                )
            if "alsa_profile_set_file" not in family:
                raise ConfigError(
                    f"family package {family_id} has alsa_profile_path_files without alsa_profile_set_file"
                )
            for path in paths:
                self._rule_file(
                    self.family_directories[family_id.casefold()],
                    path,
                    f"family package {family_id} alsa_profile_path_files",
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
        managed_files = family.get("managed_files", [])
        if not isinstance(managed_files, list):
            raise ConfigError(f"family package {family_id} managed_files must be an array")
        package_dir = self.family_directories[family_id.casefold()]
        managed_targets: set[str] = set()
        unit_targets: set[str] = set()
        for asset in managed_files:
            if not isinstance(asset, dict):
                raise ConfigError(f"family package {family_id} contains an invalid managed file")
            unknown = sorted(str(key) for key in asset if key not in self._MANAGED_FILE_FIELDS)
            if unknown:
                raise ConfigError(
                    f"family package {family_id} managed file has unknown fields: {', '.join(unknown)}"
                )
            target = asset.get("path")
            if (
                not isinstance(target, str)
                or Path(target).parent.as_posix() not in self._MANAGED_FILE_DIRECTORIES
                or Path(target).name in {"", ".", ".."}
                or not re.fullmatch(r"[A-Za-z0-9_.+-]+", Path(target).name)
            ):
                raise ConfigError(f"unsafe managed file path: {target!r}")
            if target in managed_targets:
                raise ConfigError(f"family package {family_id} duplicates managed file {target}")
            managed_targets.add(target)
            mode = asset.get("mode")
            allowed_mode = 0o755 if Path(target).parent == Path("/usr/bin") else 0o644
            if mode != allowed_mode:
                raise ConfigError(
                    f"family package {family_id} managed file {target} mode must be {oct(allowed_mode)}"
                )
            content_file = asset.get("content_file")
            content_path = self._rule_file(
                package_dir, content_file, f"family package {family_id} managed file"
            )
            option_name = asset.get("option")
            if option_name is not None and (
                not isinstance(option_name, str)
                or option_name.casefold() not in option_names
            ):
                raise ConfigError(
                    f"family package {family_id} managed file has unknown option {option_name!r}"
                )
            if Path(target).parent == Path("/usr/lib/systemd/system") and target.endswith(".service"):
                service_name = Path(target).name
                unit_targets.add(service_name)
                service_target = self._service_install_target(
                    content_path, family_id, service_name
                )
                previous_target = self.systemd_service_targets.get(service_name)
                if previous_target is not None and previous_target != service_target:
                    raise ConfigError(
                        f"service {service_name} has conflicting WantedBy targets "
                        f"{previous_target} and {service_target}"
                    )
                self.systemd_service_targets[service_name] = service_target
        services = family.get("systemd_services", [])
        if (
            not isinstance(services, list)
            or any(
                not isinstance(service, str)
                or not re.fullmatch(r"[A-Za-z0-9_.@+-]+\.service", service)
                or service not in unit_targets
                for service in services
            )
            or len(set(services)) != len(services)
        ):
            raise ConfigError(
                f"family package {family_id} systemd_services must name managed service files"
            )
        service_links = family.get("systemd_service_links", {})
        if not isinstance(service_links, dict) or set(service_links) != set(services):
            raise ConfigError(f"family package {family_id} has invalid systemd_service_links")
        for service, link in service_links.items():
            expected = f"/etc/systemd/system/{self.systemd_service_targets[service]}.wants/{service}"
            if link != expected:
                raise ConfigError(f"family package {family_id} has invalid link for {service}")
            previous_link = self.systemd_service_links.get(service)
            if previous_link is not None and previous_link != link:
                raise ConfigError(f"service {service} has conflicting Family link paths")
            self.systemd_service_links[service] = link
        rules = family.get("udev_rules", [])
        if not isinstance(rules, list):
            raise ConfigError(f"family package {family_id} udev_rules must be an array")
        targets: set[str] = set()
        for rule in rules:
            self._validate_rule(family_id, rule, targets)

    @staticmethod
    def _service_install_target(path: Path, family_id: str, service: str) -> str:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError) as exc:
            raise ConfigError(
                f"cannot read systemd unit for family package {family_id}: {exc}"
            ) from exc
        section = ""
        wanted_by: list[str] = []
        for line in lines:
            value = line.strip()
            if value.startswith("[") and value.endswith("]"):
                section = value[1:-1]
            elif section == "Install" and value.startswith("WantedBy="):
                wanted_by.extend(value.partition("=")[2].split())
        if len(wanted_by) != 1 or not re.fullmatch(
            r"[A-Za-z0-9_.@:-]+\.target", wanted_by[0]
        ):
            raise ConfigError(
                f"systemd unit {service} in family package {family_id} must declare one valid WantedBy target"
            )
        return wanted_by[0]

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
        if "exact_content" in rule and not isinstance(rule["exact_content"], bool):
            raise ConfigError(f"family package {family_id} has invalid exact_content for {target}")
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
        if rule.get("exact_content"):
            result["exact_content"] = True
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
            has_any_field = any(
                key in option for key in ("code_shift", "code_width", "code_value")
            )
            config_only = option.get("config_only", False)
            if not isinstance(config_only, bool):
                raise ConfigError(f"option {name} has invalid config_only")
            if config_only and (has_bit or has_any_field):
                raise ConfigError(
                    f"config-only option {name} must not define product-code fields"
                )
            if not config_only and has_bit == has_field:
                raise ConfigError(
                    f"option {name} must define exactly one code bit or field"
                )
            if has_bit and not 0 <= int(option["code_bit"]) <= 31:
                raise ConfigError(f"option {name} has an invalid code bit")
            if has_field:
                width, value = int(option["code_width"]), int(option["code_value"])
                if width <= 0 or value < 0 or value >= (1 << width):
                    raise ConfigError(f"option {name} has an invalid code field")
            for key in (
                "config_lines",
                "cm4_config_lines",
                "pi4_config_lines",
                "cm5_config_lines",
                "pi5_config_lines",
            ):
                lines = option.get(key, [])
                if not isinstance(lines, list) or not all(
                    isinstance(line, str) and line.strip() and "\n" not in line
                    for line in lines
                ):
                    raise ConfigError(f"option {name} has invalid {key}")
            allowed_models = option.get("models")
            if allowed_models is not None:
                if not isinstance(allowed_models, list) or not allowed_models or not all(
                    isinstance(model, str) and model
                    for model in allowed_models
                ):
                    raise ConfigError(f"option {name} has invalid models")
                missing_models = [
                    model for model in allowed_models
                    if model.casefold() not in self.model_index
                ]
                if missing_models:
                    raise ConfigError(
                        f"option {name} references unknown models: {', '.join(missing_models)}"
                    )

        option_masks: list[tuple[str, int, str | None]] = []
        for name, option in self.options.items():
            if option.get("config_only", False):
                continue
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
            if option.get("config_only", False):
                continue
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
            if key == "dtparam":
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
        for name in options:
            unsupported_options = family.get("unsupported_options", [])
            if name.casefold() in {item.casefold() for item in unsupported_options}:
                raise ConfigError(f"option {name} is not supported for model {model['name']}")
            allowed_models = self.options[name].get("models")
            if allowed_models is not None and str(model["name"]).casefold() not in {
                str(item).casefold() for item in allowed_models
            }:
                raise ConfigError(
                    f"option {name} is only supported for models: {', '.join(allowed_models)}"
                )
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
            family_option_lines = family.get("option_config_lines", {})
            family_lines = next(
                (
                    lines
                    for family_option, lines in family_option_lines.items()
                    if str(family_option).casefold() == name.casefold()
                ),
                [],
            )
            option_lines += list(
                family_lines
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

        managed_files = []
        for asset in family.get("managed_files", []):
            if asset.get("option") and asset["option"].casefold() not in {
                option.casefold() for option in options
            }:
                continue
            path = self._rule_file(
                package_dir,
                asset["content_file"],
                f"family package {family_id} managed file",
            )
            try:
                content = path.read_bytes().decode("utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                raise ConfigError(f"cannot read managed file {path}: {exc}") from exc
            managed_files.append(
                {"path": str(asset["path"]), "content": content, "mode": asset["mode"]}
            )
        selected_unit_names = {
            Path(str(asset["path"])).name
            for asset in managed_files
            if str(asset["path"]).endswith(".service")
        }

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
            "screen_config": family.get("desktop_config"),
            "screen_goodix_touch_devices": list(
                family.get("goodix_touch_devices", [])
            ),
            "wireplumber_modern": family_text("wireplumber_modern_file"),
            "wireplumber_modern_target": family.get("wireplumber_modern_target"),
            "alsa_config": family_text("alsa_config_file"),
            "alsa_config_target": family.get("alsa_config_target"),
            "alsa_state": family_text("alsa_state_file"),
            "alsa_state_target": family.get("alsa_state_target"),
            "alsa_state_blocks": [
                str(block) for block in family.get("alsa_state_blocks", [])
            ],
            "alsa_restore_skip_ucm_blocks": list(family.get("alsa_restore_skip_ucm_blocks", [])),
            "wireplumber_legacy_hashes": list(family.get("wireplumber_legacy_hashes", [])),
            "alsa_profile_set_file": (
                str(family["alsa_profile_set_file"])
                if "alsa_profile_set_file" in family
                else None
            ),
            "alsa_profile_set_target": family.get("alsa_profile_set_target"),
            "alsa_profile_set": family_text("alsa_profile_set_file"),
            "alsa_profile_path_files": [
                {
                    "name": Path(str(path)).name,
                    "target": family["alsa_profile_path_targets"][index],
                    "content": (package_dir / str(path)).read_text(encoding="utf-8"),
                }
                for index, path in enumerate(family.get("alsa_profile_path_files", []))
            ],
            "audio_packages": family_path_list("audio_packages"),
            "managed_files": managed_files,
            "systemd_services": [
                service
                for service in family.get("systemd_services", [])
                if service in selected_unit_names
            ],
            "systemd_service_targets": dict(self.systemd_service_targets),
            "systemd_service_links": dict(self.systemd_service_links),
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

    def known_rule_paths(self) -> set[str]:
        return {
            str(rule["path"])
            for family in self.families.values()
            for rule in family.get("udev_rules", [])
        }

    def model_from_product_code(self, product_code: int) -> dict[str, Any] | None:
        option_mask = 0
        coded_fields: dict[tuple[int, int], set[int]] = {}
        for option in self.options.values():
            if option.get("config_only"):
                continue
            if "code_bit" in option:
                option_mask |= 1 << int(option["code_bit"])
            else:
                shift, width, value = (
                    int(option[key]) for key in ("code_shift", "code_width", "code_value")
                )
                option_mask |= ((1 << width) - 1) << shift
                coded_fields.setdefault((shift, width), {0}).add(value)
        if any(
            ((product_code >> shift) & ((1 << width) - 1)) not in allowed
            for (shift, width), allowed in coded_fields.items()
        ):
            return None
        base_code = product_code & ~option_mask
        for model in self.models:
            if self._identity_code(model, []) == base_code:
                return model
        return None

    def has_wireplumber_config(self) -> bool:
        return any(
            "wireplumber_modern_file" in family
            for family in self.families.values()
        )

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
