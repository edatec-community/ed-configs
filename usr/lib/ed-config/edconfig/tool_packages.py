from __future__ import annotations

import gzip
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zlib
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlsplit, urlunsplit

from .errors import ApplyError
from .terminal import colorize


APT_CACHE = "/usr/bin/apt-cache"
APT_GET = "/usr/bin/apt-get"
DPKG = "/usr/bin/dpkg"
DPKG_DEB = "/usr/bin/dpkg-deb"
DPKG_QUERY = "/usr/bin/dpkg-query"
GETCONF = "/usr/bin/getconf"
WGET = "/usr/bin/wget"
EDATEC_ROOT = "https://apt.edatec.cn/raspbian"
EDATEC_POOL = f"{EDATEC_ROOT}/pool/main/e/"
_PACKAGE_NAME = re.compile(r"^[a-z0-9][a-z0-9+.-]*$")


def _command(
    arguments: list[str],
    description: str,
    *,
    check: bool = True,
    text: bool = True,
    stream_output: bool = False,
) -> subprocess.CompletedProcess[Any]:
    environment = os.environ.copy()
    environment["LC_ALL"] = "C"
    try:
        run_options: dict[str, Any] = {
            "text": text,
            "check": False,
            "env": environment,
        }
        if stream_output:
            run_options["stdout"] = sys.stderr
            run_options["stderr"] = sys.stderr
        else:
            run_options["capture_output"] = True
        result = subprocess.run(arguments, **run_options)
    except OSError as exc:
        raise ApplyError(f"cannot {description}: {exc}") from exc
    if check and result.returncode != 0:
        output = result.stderr or result.stdout or ""
        detail = output.decode(errors="replace") if isinstance(output, bytes) else output
        detail = detail.strip()
        raise ApplyError(f"cannot {description}" + (f": {detail}" if detail else ""))
    return result


def _validated_packages(packages: list[str]) -> list[str]:
    if not isinstance(packages, list) or any(
        not isinstance(package, str) or not _PACKAGE_NAME.fullmatch(package)
        for package in packages
    ):
        raise ApplyError("tool package list contains an invalid package name")
    if len(set(packages)) != len(packages):
        raise ApplyError("tool package list contains duplicate package names")
    return list(packages)


def missing_packages(packages: list[str]) -> list[str]:
    requested = _validated_packages(packages)
    missing = []
    for package in requested:
        result = _command(
            [DPKG_QUERY, "-W", "-f=${Status}", package],
            f"inspect installed package {package}",
            check=False,
        )
        if result.returncode != 0 or result.stdout.strip() != "install ok installed":
            missing.append(package)
    return missing


def _native_candidate(package: str) -> bool:
    result = _command(
        [APT_CACHE, "policy", package],
        f"search configured package sources for {package}",
    )
    match = re.search(r"(?m)^\s*Candidate:\s*(\S+)\s*$", result.stdout)
    return bool(match and match.group(1) != "(none)")


class _DirectoryLinks(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() != "a":
            return
        href = dict(attrs).get("href")
        if href:
            self.links.append(unquote(href))


def _compare_versions(left: str, right: str) -> int:
    if left == right:
        return 0
    result = _command(
        [DPKG, "--compare-versions", left, "gt", right],
        "compare Debian package versions",
        check=False,
    )
    if result.returncode == 0:
        return 1
    result = _command(
        [DPKG, "--compare-versions", right, "gt", left],
        "compare Debian package versions",
        check=False,
    )
    if result.returncode == 0:
        return -1
    raise ApplyError(f"cannot compare Debian package versions {left!r} and {right!r}")


def _filename_candidate(filename: str, package: str, architecture: str) -> tuple[str, str] | None:
    if not filename.startswith(f"{package}_") or not filename.endswith(".deb"):
        return None
    parts = filename[:-4].rsplit("_", 1)
    if len(parts) != 2 or parts[1] not in {architecture, "all"}:
        return None
    version = parts[0][len(package) + 1 :]
    if not version:
        return None
    return version, parts[1]


def _latest_filename(filenames: list[str], package: str, architecture: str) -> tuple[str, str] | None:
    best: tuple[str, str, str] | None = None
    for filename in filenames:
        if Path(filename).name != filename:
            continue
        candidate = _filename_candidate(filename, package, architecture)
        if candidate is None:
            continue
        version, candidate_arch = candidate
        if best is None or _compare_versions(version, best[1]) > 0:
            best = (filename, version, candidate_arch)
        elif version == best[1] and candidate_arch == architecture and best[2] == "all":
            best = (filename, version, candidate_arch)
    return (best[0], best[1]) if best else None


def _download_bytes(url: str) -> bytes:
    result = _command(
        [WGET, "--quiet", "--timeout=30", "--tries=2", "-O", "-", url],
        f"download package metadata from {url}",
        check=False,
        text=False,
    )
    if result.returncode != 0:
        detail = result.stderr.decode(errors="replace").strip()
        raise ApplyError(
            f"cannot download package metadata from {url}"
            + (f": {detail}" if detail else "")
        )
    return result.stdout


def _packages_index_entry(
    package: str, architecture: str
) -> tuple[str, str, str] | None:
    index_url = f"{EDATEC_ROOT}/dists/stable/main/binary-{architecture}/Packages.gz"
    raw = _download_bytes(index_url)
    try:
        content = gzip.decompress(raw).decode("utf-8", errors="replace")
    except (OSError, EOFError, zlib.error) as exc:
        raise ApplyError(f"invalid package index from {index_url}: {exc}") from exc
    for paragraph in re.split(r"\n\s*\n", content):
        fields: dict[str, str] = {}
        for line in paragraph.splitlines():
            if not line or line[0].isspace() or ": " not in line:
                continue
            key, value = line.split(": ", 1)
            fields[key] = value
        if fields.get("Package") != package:
            continue
        if fields.get("Architecture") not in {architecture, "all"}:
            continue
        filename = fields.get("Filename", "")
        relative = Path(filename)
        if (
            not filename.startswith("pool/main/e/")
            or relative.is_absolute()
            or ".." in relative.parts
        ):
            raise ApplyError(f"package index has an unsafe Filename for {package}")
        version = fields.get("Version")
        if version:
            return filename, version, fields["Architecture"]
    return None


def _edatec_filename(package: str, architecture: str) -> tuple[str, str, str]:
    listing_error: str | None = None
    try:
        listing = _download_bytes(EDATEC_POOL).decode("utf-8", errors="replace")
        parser = _DirectoryLinks()
        parser.feed(listing)
        candidate = _latest_filename(parser.links, package, architecture)
        if candidate:
            filename, version = candidate
            arch = _filename_candidate(filename, package, architecture)
            return f"pool/main/e/{filename}", version, arch[1] if arch else architecture
    except ApplyError as exc:
        listing_error = str(exc)

    try:
        entry = _packages_index_entry(package, architecture)
    except ApplyError as exc:
        detail = f"; directory listing failed: {listing_error}" if listing_error else ""
        raise ApplyError(f"cannot locate {package} under {EDATEC_POOL}{detail}: {exc}") from exc
    if entry is None:
        raise ApplyError(f"package {package} was not found under {EDATEC_POOL}")
    return entry


def _deb_field(path: Path, field: str) -> str:
    result = _command(
        [DPKG_DEB, "-f", str(path), field],
        f"inspect downloaded package {path.name}",
    )
    value = result.stdout.strip()
    if not value:
        raise ApplyError(f"downloaded package {path.name} has no {field} metadata")
    return value


def _download_edatec_package(
    package: str, architecture: str, directory: Path
) -> Path:
    filename, expected_version, expected_arch = _edatec_filename(package, architecture)
    relative = Path(filename)
    target_url = f"{EDATEC_ROOT}/{quote(filename, safe='/._+-:~')}"
    target = directory / relative.name
    print(f"Downloading {package} from the EDATEC package pool", file=sys.stderr, flush=True)
    _command(
        [
            WGET,
            "--timeout=30",
            "--tries=2",
            "--progress=bar:force",
            "-O",
            str(target),
            target_url,
        ],
        f"download {package} from the EDATEC package pool",
        stream_output=True,
    )
    actual_package = _deb_field(target, "Package")
    actual_version = _deb_field(target, "Version")
    actual_arch = _deb_field(target, "Architecture")
    if actual_package != package or actual_version != expected_version:
        raise ApplyError(f"downloaded DEB metadata does not match package index for {package}")
    if actual_arch not in {architecture, "all"} or actual_arch != expected_arch:
        raise ApplyError(f"downloaded DEB architecture does not match package index for {package}")
    return target


def _confirm_install(assume_yes: bool) -> None:
    if assume_yes:
        print("Confirmation accepted by -y.", file=sys.stderr, flush=True)
        return
    if not sys.stdin.isatty():
        raise ApplyError(
            "tool package installation needs interactive confirmation; rerun with -y"
        )
    print(
        "Proceed with installing these tool packages? [y/N] ",
        end="",
        file=sys.stderr,
        flush=True,
    )
    answer = sys.stdin.readline().strip().casefold()
    if answer not in {"y", "yes"}:
        raise ApplyError(
            "tool package installation cancelled; configuration was not changed"
        )


def install_tool_packages(packages: list[str], *, assume_yes: bool = False) -> list[str]:
    requested = _validated_packages(packages)
    if not requested:
        return []
    if os.geteuid() != 0:
        raise ApplyError("tool packages must be installed as root")
    print(f"Tool packages declared: {', '.join(requested)}", file=sys.stderr, flush=True)
    missing = missing_packages(requested)
    missing_set = set(missing)
    for package in requested:
        state = "not installed" if package in missing_set else "already installed (skip)"
        print(f"  {package}: {state}", file=sys.stderr, flush=True)
    if not missing:
        print("All declared tool packages are already installed.", file=sys.stderr, flush=True)
        print(file=sys.stderr, flush=True)
        print(
            colorize(
                "Tool package installation completed: nothing to install",
                "green",
                sys.stderr,
            ),
            file=sys.stderr,
            flush=True,
        )
        print(file=sys.stderr, flush=True)
        return []
    print("Packages to install: " + ", ".join(missing), file=sys.stderr, flush=True)
    print(file=sys.stderr, flush=True)
    print("Checking configured APT sources...", file=sys.stderr, flush=True)
    native = [package for package in missing if _native_candidate(package)]
    fallback = [package for package in missing if package not in native]
    if native:
        print(
            "  Configured APT sources: " + ", ".join(native),
            file=sys.stderr,
            flush=True,
        )
    if fallback:
        print(
            "  EDATEC package pool fallback: " + ", ".join(fallback),
            file=sys.stderr,
            flush=True,
        )
    print(file=sys.stderr, flush=True)
    _confirm_install(assume_yes)
    print(file=sys.stderr, flush=True)

    if native:
        print(
            "Installing from configured APT sources: " + ", ".join(native),
            file=sys.stderr,
            flush=True,
        )
        _command(
            [APT_GET, "install", "--yes", "--no-install-recommends", *native],
            "install packages from the device's configured APT sources",
            stream_output=True,
        )
    if fallback:
        print(
            "No configured APT candidate; using EDATEC package pool for: "
            + ", ".join(fallback),
            file=sys.stderr,
            flush=True,
        )
        architecture = _command(
            [DPKG, "--print-architecture"], "detect device package architecture"
        ).stdout.strip()
        if architecture not in {"arm64", "armhf"}:
            raise ApplyError(f"unsupported architecture for EDATEC packages: {architecture}")
        with tempfile.TemporaryDirectory(prefix="ed-config-tool-debs-") as temporary:
            directory = Path(temporary)
            downloaded = [
                _download_edatec_package(package, architecture, directory)
                for package in fallback
            ]
            _command(
                [
                    APT_GET,
                    "install",
                    "--yes",
                    "--no-install-recommends",
                    *(str(path) for path in downloaded),
                ],
                "install downloaded EDATEC packages",
                stream_output=True,
            )
    print(file=sys.stderr, flush=True)
    print(
        colorize(
            "Tool package installation completed: " + ", ".join(missing),
            "green",
            sys.stderr,
        ),
        file=sys.stderr,
        flush=True,
    )
    print(file=sys.stderr, flush=True)
    return missing


def _package_directory_url(base_url: str, architecture: str) -> str:
    if not isinstance(base_url, str) or not base_url.strip():
        raise ApplyError("package base URL must not be empty")
    try:
        parsed = urlsplit(base_url.strip())
        _ = parsed.port
    except ValueError as exc:
        raise ApplyError(f"invalid package base URL: {exc}") from exc
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or any(part == ".." for part in unquote(parsed.path).split("/"))
    ):
        raise ApplyError("package base URL must be an HTTP(S) directory URL")
    path = parsed.path.rstrip("/")
    return urlunsplit((parsed.scheme, parsed.netloc, f"{path}/{architecture}/", "", ""))


def _custom_source_architecture() -> str:
    long_bit = _command(
        [GETCONF, "LONG_BIT"], "detect device userspace word size"
    ).stdout.strip()
    architecture_by_bits = {"64": "arm64", "32": "armhf"}
    architecture = architecture_by_bits.get(long_bit)
    if architecture is None:
        raise ApplyError(f"unsupported userspace word size for DEB source: {long_bit!r}")
    dpkg_architecture = _command(
        [DPKG, "--print-architecture"], "detect device package architecture"
    ).stdout.strip()
    if dpkg_architecture != architecture:
        raise ApplyError(
            "device architecture does not match getconf LONG_BIT: "
            f"{long_bit} bits selects {architecture}, dpkg reports {dpkg_architecture}"
        )
    return architecture


def install_tool_packages_from_directory(
    packages: list[str], *, base_url: str, assume_yes: bool = False
) -> list[str]:
    """Install validated local DEBs from an explicit directory without source fallback."""
    requested = _validated_packages(packages)
    if not requested:
        return []
    if os.geteuid() != 0:
        raise ApplyError("tool packages must be installed as root")

    print(f"Tool packages declared: {', '.join(requested)}", file=sys.stderr, flush=True)
    missing = missing_packages(requested)
    missing_set = set(missing)
    for package in requested:
        state = "not installed" if package in missing_set else "already installed (skip)"
        print(f"  {package}: {state}", file=sys.stderr, flush=True)
    if not missing:
        print("All declared tool packages are already installed.", file=sys.stderr, flush=True)
        print(file=sys.stderr, flush=True)
        print(
            colorize(
                "Tool package installation completed: nothing to install",
                "green",
                sys.stderr,
            ),
            file=sys.stderr,
            flush=True,
        )
        print(file=sys.stderr, flush=True)
        return []

    architecture = _custom_source_architecture()
    directory_url = _package_directory_url(base_url, architecture)
    print(f"Packages to install: {', '.join(missing)}", file=sys.stderr, flush=True)
    print(file=sys.stderr, flush=True)
    print(f"Searching package directory: {directory_url}", file=sys.stderr, flush=True)
    listing = _download_bytes(directory_url).decode("utf-8", errors="replace")
    parser = _DirectoryLinks()
    parser.feed(listing)

    candidates: dict[str, tuple[str, str, str]] = {}
    not_found: list[str] = []
    for package in missing:
        candidate = _latest_filename(parser.links, package, architecture)
        if candidate is None:
            not_found.append(package)
            continue
        filename, version = candidate
        filename_metadata = _filename_candidate(filename, package, architecture)
        if filename_metadata is None:
            not_found.append(package)
            continue
        candidates[package] = (filename, version, filename_metadata[1])
    if not_found:
        raise ApplyError(
            f"packages not found under {directory_url}: {', '.join(not_found)}; "
            "no DEB files were downloaded"
        )

    for package in missing:
        filename, version, candidate_architecture = candidates[package]
        print(
            f"  {package}: {version} ({candidate_architecture})",
            file=sys.stderr,
            flush=True,
        )
    print(file=sys.stderr, flush=True)
    _confirm_install(assume_yes)
    print(file=sys.stderr, flush=True)

    try:
        staging_directory = Path(tempfile.mkdtemp(prefix="ed-config-tool-debs-"))
    except OSError as exc:
        raise ApplyError(f"cannot create temporary DEB directory: {exc}") from exc

    downloaded: list[Path] = []
    try:
        for package in missing:
            filename, expected_version, expected_architecture = candidates[package]
            target_url = f"{directory_url}{quote(filename, safe='._+~:-')}"
            target = staging_directory / filename
            print(
                f"Downloading {package} from the specified package directory",
                file=sys.stderr,
                flush=True,
            )
            _command(
                [
                    WGET,
                    "--timeout=30",
                    "--tries=2",
                    "--progress=bar:force",
                    "-O",
                    str(target),
                    target_url,
                ],
                f"download {package} from the specified package directory",
                stream_output=True,
            )
            actual_package = _deb_field(target, "Package")
            actual_version = _deb_field(target, "Version")
            actual_architecture = _deb_field(target, "Architecture")
            if actual_package != package or actual_version != expected_version:
                raise ApplyError(
                    f"downloaded DEB metadata does not match directory listing for {package}"
                )
            if (
                actual_architecture not in {architecture, "all"}
                or actual_architecture != expected_architecture
            ):
                raise ApplyError(
                    f"downloaded DEB architecture does not match directory listing for {package}"
                )
            downloaded.append(target)
        print(
            "Installing verified DEBs from the specified package directory: "
            + ", ".join(missing),
            file=sys.stderr,
            flush=True,
        )
        _command(
            [
                APT_GET,
                "install",
                "--yes",
                "--no-install-recommends",
                *(str(path) for path in downloaded),
            ],
            "install verified packages from the specified package directory",
            stream_output=True,
        )
        still_missing = missing_packages(missing)
        if still_missing:
            raise ApplyError(
                "package manager completed without installing all requested packages: "
                + ", ".join(still_missing)
            )
    except BaseException:
        shutil.rmtree(staging_directory, ignore_errors=True)
        raise

    shutil.rmtree(staging_directory, ignore_errors=True)
    print(file=sys.stderr, flush=True)
    print(
        colorize(
            "Tool package installation completed: " + ", ".join(missing),
            "green",
            sys.stderr,
        ),
        file=sys.stderr,
        flush=True,
    )
    print(file=sys.stderr, flush=True)
    return missing
