#!/usr/bin/env python3
"""Verify narrowly scoped, installer-recorded FlightSQL package provenance."""

import argparse
import ast
import base64
import csv
import hashlib
import hmac
import importlib.metadata
import io
import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Dict, NoReturn, Optional, Sequence
from urllib.parse import urlparse
from urllib.request import url2pathname

if TYPE_CHECKING:
    from packaging.version import Version

_DISTRIBUTION = "flightsql-dbapi"
_DEFAULT_VERSION = "0.2.2.1"
_DEFAULT_REPOSITORY = "https://github.com/preset-io/flightsql-dbapi.git"
_FULL_SHA = re.compile(r"[0-9a-fA-F]{40}\Z")
_SHA256 = re.compile(r"[0-9a-fA-F]{64}\Z")
_RECORD_SHA256 = re.compile(r"[A-Za-z0-9_-]{43}\Z")
_REQUIRED_PACKAGE_FILES = frozenset(
    {
        "flightsql/__init__.py",
        "flightsql/client.py",
        "flightsql/dbapi.py",
        "flightsql/exceptions.py",
        "flightsql/flightsql_pb2.py",
        "flightsql/flightsql_pb2.pyi",
        "flightsql/provenance.py",
        "flightsql/sqlalchemy.py",
        "flightsql/util.py",
    }
)
_REQUIRED_METADATA_FILES = frozenset({"INSTALLER", "METADATA", "direct_url.json"})


class ProvenanceError(RuntimeError):
    """The selected distribution does not match the asserted provenance."""


@dataclass(frozen=True)
class VerificationResult:
    """Machine-readable scope of one successful verification."""

    ok: bool
    distribution: str
    installed_version: str
    version_verified: bool
    source_mode: str
    package_source_record_sha256_verified: bool
    distribution_metadata_record_sha256_verified: bool
    direct_url_record_sha256_verified: bool
    expected_commit: str
    commit_verified: bool
    verified_commit: Optional[str]
    recorded_archive_sha256_matched: Optional[str]
    local_artifact_file_sha256_verified: Optional[str]
    installer: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class _RecordRow:
    relative_path: PurePosixPath
    installed_path: Path
    digest: Optional[str]
    size: Optional[int]


@dataclass(frozen=True)
class _InstalledDistribution:
    distribution: importlib.metadata.Distribution
    site_root: Path
    dist_info: Path
    dist_info_relative: PurePosixPath
    record_rows: Dict[str, _RecordRow]
    package_init: Path
    direct_url_path: Path


def _canonical_distribution_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _normalized_hash(value: str, label: str = "expected artifact SHA-256") -> str:
    if value.startswith("sha256=") or value.startswith("sha256:"):
        value = value[7:]
    if not _SHA256.fullmatch(value):
        raise ProvenanceError(f"{label} must contain exactly 64 hexadecimal characters")
    return value.lower()


def _archive_sha256(direct_url: Dict[str, Any]) -> Optional[str]:
    archive_info = direct_url.get("archive_info")
    if archive_info is None:
        archive_info = {}
    if not isinstance(archive_info, dict):
        raise ProvenanceError("installed direct_url.json archive_info must contain a JSON object")

    hashes = archive_info.get("hashes")
    if hashes is None:
        hashes = {}
    if not isinstance(hashes, dict):
        raise ProvenanceError("installed direct_url.json archive hashes must contain a JSON object")
    for algorithm in hashes:
        if not isinstance(algorithm, str):
            raise ProvenanceError("installed direct_url.json archive hash names must be strings")
        if algorithm.lower() in {"md5", "sha1"}:
            raise ProvenanceError(f"installed direct_url.json uses unsafe artifact hash algorithm {algorithm!r}")

    legacy_hash = archive_info.get("hash")
    legacy_value = None
    if legacy_hash is not None:
        if not isinstance(legacy_hash, str) or "=" not in legacy_hash:
            raise ProvenanceError("installed direct_url.json legacy archive hash is malformed")
        algorithm, legacy_value = legacy_hash.split("=", 1)
        if algorithm.lower() != "sha256":
            raise ProvenanceError(
                f"installed direct_url.json uses unsafe or unsupported artifact hash algorithm {algorithm!r}"
            )
    value = hashes.get("sha256")
    if value is None:
        value = legacy_value
    elif legacy_value is not None and value != legacy_value:
        raise ProvenanceError("installed direct_url.json contains conflicting SHA-256 artifact hashes")
    if value is None:
        return None
    if not isinstance(value, str):
        raise ProvenanceError("installed artifact SHA-256 must be a string")
    return _normalized_hash(value, "installed artifact SHA-256")


def _normalized_repository(url: str) -> str:
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or not parsed.hostname
    ):
        raise ProvenanceError(f"repository URL is not an uncredentialed HTTPS source: {url!r}")
    return url.rstrip("/").removesuffix(".git")


def _accepted_archive_urls(repository: str, commit: str) -> set[str]:
    base = _normalized_repository(repository)
    parsed = urlparse(base)
    if parsed.netloc != "github.com":
        return {
            f"{base}/archive/{commit}.tar.gz",
            f"{base}/archive/{commit}.zip",
        }
    return {
        f"{base}/archive/{commit}.tar.gz",
        f"{base}/archive/{commit}.zip",
        f"https://codeload.github.com{parsed.path}/tar.gz/{commit}",
        f"https://codeload.github.com{parsed.path}/zip/{commit}",
    }


def _version(value: str, label: str) -> "Version":
    try:
        from packaging.version import InvalidVersion, Version
    except Exception as error:
        raise RuntimeError("the packaging dependency is unavailable") from error
    try:
        return Version(value)
    except InvalidVersion as error:
        raise ProvenanceError(f"{label} {value!r} is not a valid PEP 440 version") from error


def _distribution_path(distribution: importlib.metadata.Distribution) -> Path:
    # All declared Python versions discover installed wheels as
    # PathDistribution instances.  Its path is the selected metadata directory,
    # unlike RECORD rows, which are installer-controlled input to this check.
    raw_path = getattr(distribution, "_path", None)
    if raw_path is None:
        raise ProvenanceError("importlib.metadata did not expose the selected distribution metadata path")
    path = Path(raw_path)
    if path.is_symlink() or not path.is_dir() or not path.name.endswith(".dist-info"):
        raise ProvenanceError("selected distribution metadata path is not one regular .dist-info directory")
    return path.resolve()


def _metadata_declares_distribution(path: Path) -> bool:
    metadata = path / "METADATA"
    try:
        text = metadata.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return False
    match = re.search(r"^Name:\s*(\S.*?)\s*$", text, flags=re.MULTILINE | re.IGNORECASE)
    return bool(match and _canonical_distribution_name(match.group(1)) == _DISTRIBUTION)


def _filename_declares_distribution(path: Path) -> bool:
    normalized = re.sub(r"[-_.]+", "_", path.name.lower())
    return normalized.startswith("flightsql_dbapi_") and normalized.endswith("_dist_info")


def _selected_distribution() -> tuple[importlib.metadata.Distribution, Path, Path]:
    try:
        discovered = list(importlib.metadata.distributions(name=_DISTRIBUTION))
    except importlib.metadata.PackageNotFoundError as error:
        raise ProvenanceError(f"{_DISTRIBUTION} is not installed") from error
    if not discovered:
        raise ProvenanceError(f"{_DISTRIBUTION} is not installed")

    by_path: Dict[Path, importlib.metadata.Distribution] = {}
    for distribution in discovered:
        by_path[_distribution_path(distribution)] = distribution
    if len(by_path) != 1:
        paths = ", ".join(sorted(str(path) for path in by_path))
        raise ProvenanceError(f"multiple {_DISTRIBUTION} distribution metadata directories were discovered: {paths}")

    dist_info, distribution = next(iter(by_path.items()))
    site_root = Path(str(distribution.locate_file(""))).resolve()
    if dist_info.parent != site_root:
        raise ProvenanceError("selected .dist-info directory is not directly inside its importlib.metadata root")

    decoys = []
    for candidate in site_root.glob("*.dist-info"):
        # Compare the directory entry itself, not its resolved target: a
        # sibling symlink to the selected metadata is still a duplicate/decoy.
        if candidate == dist_info:
            continue
        if _filename_declares_distribution(candidate) or _metadata_declares_distribution(candidate):
            decoys.append(candidate)
    if decoys:
        paths = ", ".join(sorted(str(path) for path in decoys))
        raise ProvenanceError(f"duplicate or decoy {_DISTRIBUTION} .dist-info directories exist: {paths}")
    return distribution, site_root, dist_info


def _record_path(path_text: str) -> PurePosixPath:
    if not path_text or "\\" in path_text or any(ord(character) < 32 for character in path_text):
        raise ProvenanceError(f"installed RECORD contains an invalid path {path_text!r}")
    raw_parts = path_text.split("/")
    if any(part in {"", ".", ".."} for part in raw_parts) or re.match(r"^[A-Za-z]:", path_text):
        raise ProvenanceError(f"installed RECORD contains unsafe path traversal {path_text!r}")
    path = PurePosixPath(path_text)
    if path.is_absolute() or ".." in path.parts:
        raise ProvenanceError(f"installed RECORD contains unsafe path traversal {path_text!r}")
    return path


def _path_has_symlink(root: Path, path: Path) -> bool:
    relative = path.relative_to(root)
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            return True
    return False


def _parse_record_hash(path_text: str, hash_text: str) -> Optional[str]:
    if not hash_text:
        return None
    try:
        algorithm, digest = hash_text.split("=", 1)
    except ValueError as error:
        raise ProvenanceError(f"installed RECORD hash for {path_text!r} is malformed") from error
    if algorithm.lower() != "sha256":
        raise ProvenanceError(
            f"installed RECORD uses unsafe or unsupported hash algorithm {algorithm!r} for {path_text!r}; "
            "SHA-256 is required"
        )
    if not _RECORD_SHA256.fullmatch(digest):
        raise ProvenanceError(f"installed RECORD SHA-256 for {path_text!r} is malformed")
    return digest


def _parse_record_size(path_text: str, size_text: str) -> Optional[int]:
    if not size_text:
        return None
    if not size_text.isdecimal():
        raise ProvenanceError(f"installed RECORD size for {path_text!r} is invalid")
    return int(size_text)


def _read_record(site_root: Path, dist_info: Path) -> Dict[str, _RecordRow]:
    record_path = dist_info / "RECORD"
    if record_path.is_symlink() or not record_path.is_file():
        raise ProvenanceError("selected distribution RECORD is missing or is not a regular file")
    try:
        record_text = record_path.read_text(encoding="utf-8")
        rows = list(csv.reader(io.StringIO(record_text), strict=True))
    except (OSError, UnicodeError, csv.Error) as error:
        raise ProvenanceError("selected distribution RECORD cannot be read as valid UTF-8 CSV") from error
    if not rows:
        raise ProvenanceError("selected distribution RECORD is empty")

    actual_prefix = dist_info.relative_to(site_root).as_posix()
    record_rows: Dict[str, _RecordRow] = {}
    for row in rows:
        if len(row) != 3:
            raise ProvenanceError("installed RECORD contains a row that does not have three fields")
        path_text, hash_text, size_text = row
        relative_path = _record_path(path_text)
        if path_text in record_rows:
            raise ProvenanceError(f"installed RECORD contains duplicate path {path_text!r}")
        if relative_path.parts and relative_path.parts[0].endswith(".dist-info"):
            if relative_path.parts[0] != actual_prefix:
                raise ProvenanceError(f"installed RECORD contains a decoy distribution metadata path {path_text!r}")

        digest = _parse_record_hash(path_text, hash_text)
        size = _parse_record_size(path_text, size_text)
        installed_path = site_root.joinpath(*relative_path.parts)
        if digest is None and not (path_text == f"{actual_prefix}/RECORD" or relative_path.suffix == ".pyc"):
            raise ProvenanceError(f"installed RECORD has no SHA-256 for recorded file {path_text!r}")
        record_rows[path_text] = _RecordRow(relative_path, installed_path, digest, size)

    expected_record = f"{actual_prefix}/RECORD"
    if expected_record not in record_rows:
        raise ProvenanceError("installed RECORD does not list the selected distribution's own RECORD file")
    return record_rows


def _record_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return base64.urlsafe_b64encode(digest.digest()).rstrip(b"=").decode("ascii")


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _verify_scoped_file(site_root: Path, path: Path, record_rows: Dict[str, _RecordRow]) -> None:
    if _path_has_symlink(site_root, path) or not path.is_file():
        raise ProvenanceError(f"scoped installed file {path!s} is missing, non-regular, or reached through a symlink")
    relative = path.relative_to(site_root).as_posix()
    row = record_rows.get(relative)
    if row is None:
        raise ProvenanceError(f"installed RECORD omits scoped package file {relative!r}")
    if row.digest is None:
        raise ProvenanceError(f"installed RECORD has no SHA-256 for scoped package file {relative!r}")
    actual_digest = _record_sha256(path)
    if not hmac.compare_digest(actual_digest, row.digest):
        raise ProvenanceError(f"installed RECORD SHA-256 mismatch for scoped package file {relative!r}")
    if row.size is not None and path.stat().st_size != row.size:
        raise ProvenanceError(f"installed RECORD size mismatch for scoped package file {relative!r}")


def _verify_scoped_inventory(
    site_root: Path,
    dist_info: Path,
    record_rows: Dict[str, _RecordRow],
) -> tuple[Path, Path]:
    package_root = site_root / "flightsql"
    if package_root.is_symlink() or not package_root.is_dir():
        raise ProvenanceError("installed flightsql package directory is missing or is a symbolic link")

    package_files = []
    for path in package_root.rglob("*"):
        if path.is_symlink():
            raise ProvenanceError(f"installed package path {path!s} is a symbolic link")
        if not path.is_file() or path.suffix == ".pyc":
            continue
        package_files.append(path)
        _verify_scoped_file(site_root, path, record_rows)
    package_relatives = {path.relative_to(site_root).as_posix() for path in package_files}
    missing_package_files = _REQUIRED_PACKAGE_FILES.difference(package_relatives)
    if missing_package_files:
        raise ProvenanceError(
            "installed package omits required source files: " + ", ".join(sorted(missing_package_files))
        )

    metadata_names = set()
    for path in dist_info.rglob("*"):
        if path.is_symlink():
            raise ProvenanceError(f"installed distribution metadata path {path!s} is a symbolic link")
        if not path.is_file() or path == dist_info / "RECORD" or path.suffix == ".pyc":
            continue
        metadata_names.add(path.relative_to(dist_info).as_posix())
        _verify_scoped_file(site_root, path, record_rows)
    missing_metadata = _REQUIRED_METADATA_FILES.difference(metadata_names)
    if missing_metadata:
        raise ProvenanceError(
            "selected distribution metadata omits required files: " + ", ".join(sorted(missing_metadata))
        )

    return package_root / "__init__.py", dist_info / "direct_url.json"


def _inspect_installed_distribution() -> _InstalledDistribution:
    distribution, site_root, dist_info = _selected_distribution()
    record_rows = _read_record(site_root, dist_info)
    package_init, direct_url_path = _verify_scoped_inventory(site_root, dist_info, record_rows)
    return _InstalledDistribution(
        distribution=distribution,
        site_root=site_root,
        dist_info=dist_info,
        dist_info_relative=PurePosixPath(dist_info.relative_to(site_root).as_posix()),
        record_rows=record_rows,
        package_init=package_init,
        direct_url_path=direct_url_path,
    )


def _package_version(package_init: Path) -> "Version":
    try:
        module = ast.parse(package_init.read_text(encoding="utf-8"), filename=str(package_init))
    except (OSError, SyntaxError, UnicodeError) as error:
        raise ProvenanceError("installed flightsql/__init__.py cannot be parsed safely") from error
    values = []
    for node in module.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if not any(isinstance(target, ast.Name) and target.id == "__version__" for target in targets):
            continue
        value_node = node.value
        if isinstance(value_node, ast.Constant) and isinstance(value_node.value, str):
            values.append(value_node.value)
    if len(values) != 1:
        raise ProvenanceError("installed flightsql/__init__.py must contain one literal __version__ assignment")
    return _version(values[0], "installed flightsql.__version__")


def _installed_distribution(expected_version: str) -> tuple[_InstalledDistribution, "Version", str]:
    expected = _version(expected_version, "expected version")
    installed_layout = _inspect_installed_distribution()
    distribution = installed_layout.distribution

    try:
        metadata_name = distribution.metadata["Name"]
    except KeyError as error:
        raise ProvenanceError("selected distribution METADATA has no project name") from error
    if not isinstance(metadata_name, str) or _canonical_distribution_name(metadata_name) != _DISTRIBUTION:
        raise ProvenanceError("selected distribution METADATA has the wrong project name")
    installed = _version(distribution.version, f"installed {_DISTRIBUTION} version")
    if installed != expected:
        raise ProvenanceError(
            f"installed {_DISTRIBUTION} version {str(installed)!r} does not equal {str(expected)!r}; "
            "the explicit direct-reference installation assertion failed"
        )
    package_version = _package_version(installed_layout.package_init)
    if package_version != expected:
        raise ProvenanceError(
            f"installed flightsql.__version__ {str(package_version)!r} does not equal {str(expected)!r}"
        )

    installer_path = installed_layout.dist_info / "INSTALLER"
    try:
        installer = installer_path.read_text(encoding="utf-8").strip().lower()
    except (OSError, UnicodeError) as error:
        raise ProvenanceError("installed INSTALLER cannot be read as UTF-8") from error
    if not installer:
        raise ProvenanceError("installed INSTALLER is empty")
    return installed_layout, expected, installer


def _unique_json_object(pairs: list[tuple[str, Any]]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ProvenanceError(f"installed direct_url.json contains duplicate key {key!r}")
        result[key] = value
    return result


def _installed_direct_url(path: Path) -> Dict[str, Any]:
    try:
        direct_url_text = path.read_text(encoding="utf-8")
        direct_url = json.loads(direct_url_text, object_pairs_hook=_unique_json_object)
    except ProvenanceError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ProvenanceError("installed direct_url.json is not valid UTF-8 JSON") from error
    if not isinstance(direct_url, dict):
        raise ProvenanceError("installed direct_url.json must contain a JSON object")
    return direct_url


def _verify_vcs_source(
    source_url: str,
    vcs_info: Dict[str, Any],
    expected_repository: str,
    expected_commit: str,
) -> None:
    if vcs_info.get("vcs") != "git":
        raise ProvenanceError(f"expected Git provenance, found {vcs_info.get('vcs')!r}")
    if _normalized_repository(source_url) != _normalized_repository(expected_repository):
        raise ProvenanceError(f"installed source repository {source_url!r} is not {expected_repository!r}")
    commit_id = vcs_info.get("commit_id")
    requested_revision = vcs_info.get("requested_revision")
    if not isinstance(commit_id, str) or commit_id.lower() != expected_commit:
        raise ProvenanceError(f"installed VCS commit {commit_id!r} is not {expected_commit}")
    if not isinstance(requested_revision, str) or requested_revision.lower() != expected_commit:
        raise ProvenanceError(
            f"requested VCS revision {requested_revision!r} is not the immutable full SHA {expected_commit}"
        )


def _verify_archive_source(
    direct_url: Dict[str, Any],
    source_kind: str,
    expected_hash: Optional[str],
) -> str:
    if expected_hash is None:
        raise ProvenanceError(f"{source_kind} installs require --expected-artifact-sha256")
    installed_hash = _archive_sha256(direct_url)
    if installed_hash != expected_hash:
        raise ProvenanceError(f"recorded {source_kind} SHA-256 {installed_hash!r} is not {expected_hash}")
    return installed_hash


def _result(
    expected: "Version",
    installer: str,
    source_mode: str,
    expected_commit: str,
    commit_verified: bool,
    verified_commit: Optional[str],
    recorded_archive_hash: Optional[str] = None,
    local_artifact_hash: Optional[str] = None,
) -> VerificationResult:
    return VerificationResult(
        ok=True,
        distribution=_DISTRIBUTION,
        installed_version=str(expected),
        version_verified=True,
        source_mode=source_mode,
        package_source_record_sha256_verified=True,
        distribution_metadata_record_sha256_verified=True,
        direct_url_record_sha256_verified=True,
        expected_commit=expected_commit,
        commit_verified=commit_verified,
        verified_commit=verified_commit,
        recorded_archive_sha256_matched=recorded_archive_hash,
        local_artifact_file_sha256_verified=local_artifact_hash,
        installer=installer,
    )


def _verify_local_artifact_file(source_url: str, expected_hash: Optional[str]) -> str:
    if expected_hash is None:
        raise ProvenanceError("local-artifact installs require --expected-artifact-sha256")
    parsed_source = urlparse(source_url)
    if parsed_source.netloc not in ("", "localhost") or parsed_source.query or parsed_source.fragment:
        raise ProvenanceError("local-artifact mode requires a local, uncredentialed file URL")
    artifact_path = Path(url2pathname(parsed_source.path))
    if not artifact_path.is_absolute():
        raise ProvenanceError("local-artifact mode requires an absolute file URL")
    if artifact_path.is_symlink() or not artifact_path.is_file():
        raise ProvenanceError("local-artifact file URL does not reference an available regular non-symlink file")
    artifact_name = artifact_path.name
    if not (artifact_name.endswith(".whl") or artifact_name.endswith(".tar.gz") or artifact_name.endswith(".zip")):
        raise ProvenanceError("local-artifact mode requires a wheel or source-distribution file URL")
    actual_hash = _file_sha256(artifact_path)
    if not hmac.compare_digest(actual_hash, expected_hash):
        raise ProvenanceError(f"local-artifact file SHA-256 {actual_hash!r} is not {expected_hash}")
    return actual_hash


def verify(
    expected_commit: str,
    expected_version: str = _DEFAULT_VERSION,
    expected_repository: str = _DEFAULT_REPOSITORY,
    expected_artifact_sha256: Optional[str] = None,
    allow_local_artifact: bool = False,
) -> VerificationResult:
    """Verify selected package files/metadata and report only that scope."""
    if not _FULL_SHA.fullmatch(expected_commit):
        raise ProvenanceError("expected commit must be a full 40-character hexadecimal Git SHA")
    expected_commit = expected_commit.lower()
    expected_hash = _normalized_hash(expected_artifact_sha256) if expected_artifact_sha256 else None

    # The actual direct_url.json in the importlib.metadata-selected .dist-info
    # directory has already been required in RECORD and SHA-256 checked before
    # any of its Git/archive claims are parsed below.
    installed_layout, expected, installer = _installed_distribution(expected_version)
    direct_url = _installed_direct_url(installed_layout.direct_url_path)

    source_url = direct_url.get("url")
    if not isinstance(source_url, str):
        raise ProvenanceError("installed direct_url.json has no string source URL")
    if "vcs_info" in direct_url:
        vcs_info = direct_url["vcs_info"]
        if not isinstance(vcs_info, dict):
            raise ProvenanceError("installed direct_url.json vcs_info must contain a JSON object")
        if "archive_info" in direct_url or "dir_info" in direct_url:
            raise ProvenanceError("installed direct_url.json mixes VCS provenance with archive/directory metadata")
        _verify_vcs_source(source_url, vcs_info, expected_repository, expected_commit)
        return _result(expected, installer, "git", expected_commit, True, expected_commit)

    accepted_archives = {url.lower() for url in _accepted_archive_urls(expected_repository, expected_commit)}
    if source_url.lower() in accepted_archives:
        if "dir_info" in direct_url:
            raise ProvenanceError("installed direct_url.json mixes archive provenance with directory metadata")
        verified_hash = _verify_archive_source(direct_url, "source-archive", expected_hash)
        return _result(
            expected,
            installer,
            "source-archive",
            expected_commit,
            True,
            expected_commit,
            recorded_archive_hash=verified_hash,
        )

    parsed_source = urlparse(source_url)
    if parsed_source.scheme == "file":
        if not allow_local_artifact:
            raise ProvenanceError(
                "a local artifact does not encode its source commit; use the immutable Git/archive install in "
                "production or pass --allow-local-artifact only for an identity-only artifact test"
            )
        if "dir_info" in direct_url:
            raise ProvenanceError("local-artifact mode does not accept an editable/local directory installation")
        # Local mode hashes the referenced file directly, but reject an unsafe
        # optional installer hash rather than silently blessing weak metadata.
        _archive_sha256(direct_url)
        verified_hash = _verify_local_artifact_file(source_url, expected_hash)
        return _result(
            expected,
            installer,
            "local-artifact",
            expected_commit,
            False,
            None,
            local_artifact_hash=verified_hash,
        )

    raise ProvenanceError(
        f"installed source {source_url!r} is neither the expected Git repository nor its commit archive"
    )


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise ProvenanceError(f"invalid command line: {message}")


def _parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--expected-commit", required=True, help="reviewed immutable 40-character Git SHA")
    parser.add_argument("--expected-version", default=_DEFAULT_VERSION)
    parser.add_argument("--expected-repository", default=_DEFAULT_REPOSITORY)
    parser.add_argument("--expected-artifact-sha256")
    parser.add_argument(
        "--allow-local-artifact",
        action="store_true",
        help="hash the referenced local wheel/sdist without claiming its source commit",
    )
    parser.add_argument("--json", action="store_true", help="emit one machine-readable JSON result")
    return parser


def _success_text(result: VerificationResult) -> str:
    prefix = (
        f"verified recorded SHA-256 for FlightSQL package source and selected distribution metadata "
        f"({result.distribution} {result.installed_version}); source mode {result.source_mode}"
    )
    if result.commit_verified:
        return f"{prefix}; recorded source commit matched: {result.verified_commit}"
    return (
        f"{prefix}; referenced local artifact file SHA-256 verified: "
        f"{result.local_artifact_file_sha256_verified}; "
        f"commit NOT verified (expected commit {result.expected_commit} is contextual only)"
    )


def _failure(json_requested: bool, message: str, error_type: str, returncode: int) -> int:
    if json_requested:
        print(json.dumps({"error": message, "error_type": error_type, "ok": False}, sort_keys=True))
    else:
        print(f"FlightSQL provenance verification failed: {message}", file=sys.stderr)
    return returncode


def main(argv: Optional[Sequence[str]] = None) -> int:
    raw_arguments = list(sys.argv[1:] if argv is None else argv)
    json_requested = any(argument == "--json" or argument.startswith("--json=") for argument in raw_arguments)
    try:
        arguments = _parser().parse_args(raw_arguments)
        result = verify(
            expected_commit=arguments.expected_commit,
            expected_version=arguments.expected_version,
            expected_repository=arguments.expected_repository,
            expected_artifact_sha256=arguments.expected_artifact_sha256,
            allow_local_artifact=arguments.allow_local_artifact,
        )
    except ProvenanceError as error:
        return _failure(json_requested, str(error), "verification_failed", 1)
    except Exception as error:
        # Operational failures must not turn --json into a traceback/non-JSON
        # response.  This does not relabel them as provenance mismatches.
        message = f"{type(error).__name__}: {error}"
        return _failure(json_requested, message, "operational_error", 2)
    if arguments.json:
        print(json.dumps(result.to_dict(), sort_keys=True))
    else:
        print(_success_text(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
