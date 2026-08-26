#!/usr/bin/env python3
"""Verify an installed fork against explicit version and source assertions."""

import argparse
import ast
import base64
import csv
import hashlib
import importlib.metadata
import io
import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Optional, Sequence
from urllib.parse import unquote, urlparse

from packaging.version import InvalidVersion, Version

_DISTRIBUTION = "flightsql-dbapi"
_DEFAULT_VERSION = "0.2.3+preset.1"
_DEFAULT_REPOSITORY = "https://github.com/preset-io/flightsql-dbapi.git"
_FULL_SHA = re.compile(r"[0-9a-fA-F]{40}\Z")
_SHA256 = re.compile(r"[0-9a-fA-F]{64}\Z")
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


class ProvenanceError(RuntimeError):
    """The installed distribution does not match the asserted provenance."""


@dataclass(frozen=True)
class VerificationResult:
    """Machine-readable scope of one successful verification."""

    ok: bool
    distribution: str
    installed_version: str
    version_verified: bool
    source_mode: str
    record_integrity_verified: bool
    expected_commit: str
    commit_verified: bool
    verified_commit: Optional[str]
    artifact_sha256_verified: Optional[str]
    installer: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _normalized_hash(value: str, label: str = "expected artifact SHA-256") -> str:
    value = value.removeprefix("sha256=").removeprefix("sha256:")
    if not _SHA256.fullmatch(value):
        raise ProvenanceError(f"{label} must contain exactly 64 hexadecimal characters")
    return value.lower()


def _archive_sha256(direct_url: Dict[str, Any]) -> Optional[str]:
    archive_info = direct_url.get("archive_info") or {}
    if not isinstance(archive_info, dict):
        raise ProvenanceError("installed direct_url.json archive_info must contain a JSON object")
    hashes = archive_info.get("hashes") or {}
    if not isinstance(hashes, dict):
        raise ProvenanceError("installed direct_url.json archive hashes must contain a JSON object")
    value = hashes.get("sha256")
    if value is None:
        legacy_hash = archive_info.get("hash")
        if isinstance(legacy_hash, str) and legacy_hash.startswith("sha256="):
            value = legacy_hash.removeprefix("sha256=")
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
        raise ProvenanceError(f"repository URL is not an uncredentialed immutable HTTPS source: {url!r}")
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


def _version(value: str, label: str) -> Version:
    try:
        return Version(value)
    except InvalidVersion as error:
        raise ProvenanceError(f"{label} {value!r} is not a valid PEP 440 version") from error


def _record_path(path_text: str) -> PurePosixPath:
    if not path_text or "\\" in path_text:
        raise ProvenanceError(f"installed RECORD contains an invalid path {path_text!r}")
    path = PurePosixPath(path_text)
    if path.is_absolute():
        raise ProvenanceError(f"installed RECORD contains an absolute path {path_text!r}")
    return path


def _record_digest(path: Path, algorithm: str) -> str:
    try:
        digest = hashlib.new(algorithm)
    except ValueError as error:
        raise ProvenanceError(f"installed RECORD uses unsupported hash algorithm {algorithm!r}") from error
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return base64.urlsafe_b64encode(digest.digest()).rstrip(b"=").decode("ascii")


def _verify_record_row(distribution: importlib.metadata.Distribution, row: list[str]) -> tuple[str, Path]:
    if len(row) != 3:
        raise ProvenanceError("installed RECORD contains a row that does not have three fields")
    path_text, hash_text, size_text = row
    record_path = _record_path(path_text)
    installed_path = Path(str(distribution.locate_file(record_path)))
    if installed_path.is_symlink():
        raise ProvenanceError(f"installed RECORD path {path_text!r} is a symbolic link")
    if not installed_path.is_file():
        raise ProvenanceError(f"installed RECORD path {path_text!r} is missing or not a regular file")
    resolved_path = installed_path.resolve()

    if hash_text:
        try:
            algorithm, expected_digest = hash_text.split("=", 1)
        except ValueError as error:
            raise ProvenanceError(f"installed RECORD hash for {path_text!r} is malformed") from error
        actual_digest = _record_digest(resolved_path, algorithm)
        if actual_digest != expected_digest:
            raise ProvenanceError(f"installed RECORD hash mismatch for {path_text!r}")
    elif not (path_text.endswith(".dist-info/RECORD") or path_text.endswith(".pyc")):
        raise ProvenanceError(f"installed RECORD has no hash for security-relevant file {path_text!r}")

    if size_text:
        try:
            expected_size = int(size_text)
        except ValueError as error:
            raise ProvenanceError(f"installed RECORD size for {path_text!r} is invalid") from error
        if resolved_path.stat().st_size != expected_size:
            raise ProvenanceError(f"installed RECORD size mismatch for {path_text!r}")
    return path_text, resolved_path


def _verify_record_inventory(root: Path, recorded_paths: Dict[str, Path]) -> Path:
    missing_package_files = _REQUIRED_PACKAGE_FILES.difference(recorded_paths)
    if missing_package_files:
        raise ProvenanceError(
            "installed RECORD omits required package files: " + ", ".join(sorted(missing_package_files))
        )

    metadata_paths = [path for path in recorded_paths if path.endswith(".dist-info/METADATA")]
    if len(metadata_paths) != 1:
        raise ProvenanceError("installed RECORD does not identify exactly one distribution metadata directory")
    dist_info_prefix = metadata_paths[0].removesuffix("/METADATA")
    for metadata_name in ("INSTALLER", "METADATA"):
        if f"{dist_info_prefix}/{metadata_name}" not in recorded_paths:
            raise ProvenanceError(f"installed RECORD omits {metadata_name}")

    package_root = root / "flightsql"
    if not package_root.is_dir() or package_root.is_symlink():
        raise ProvenanceError("installed flightsql package directory is missing or is a symbolic link")
    recorded_files = set(recorded_paths.values())
    for package_path in package_root.rglob("*"):
        if package_path.is_symlink():
            raise ProvenanceError(f"installed package path {package_path} is a symbolic link")
        ignored = not package_path.is_file() or package_path.suffix == ".pyc" or "__pycache__" in package_path.parts
        if not ignored and package_path.resolve() not in recorded_files:
            raise ProvenanceError(f"installed package contains unrecorded file {package_path.relative_to(root)!s}")
    return recorded_paths["flightsql/__init__.py"]


def _verify_record_integrity(distribution: importlib.metadata.Distribution) -> Path:
    """Check installer-recorded files without importing the target package.

    RECORD is not signed. This detects inconsistent, missing, injected, or
    partially modified files; it does not resist an actor who can rewrite both
    installed files and RECORD.
    """

    record_text = distribution.read_text("RECORD")
    if record_text is None:
        raise ProvenanceError("installed RECORD is missing; file integrity cannot be checked")

    root = Path(str(distribution.locate_file(""))).resolve()
    recorded_paths: Dict[str, Path] = {}
    record_rows = list(csv.reader(io.StringIO(record_text)))
    if not record_rows:
        raise ProvenanceError("installed RECORD is empty")

    for row in record_rows:
        path_text, resolved_path = _verify_record_row(distribution, row)
        recorded_paths[path_text] = resolved_path
    return _verify_record_inventory(root, recorded_paths)


def _package_version(package_init: Path) -> Version:
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


def _installed_distribution(expected_version: str) -> tuple[importlib.metadata.Distribution, Version, str]:
    expected = _version(expected_version, "expected version")
    try:
        distribution = importlib.metadata.distribution(_DISTRIBUTION)
    except importlib.metadata.PackageNotFoundError as error:
        raise ProvenanceError(f"{_DISTRIBUTION} is not installed") from error

    package_init = _verify_record_integrity(distribution)
    installed = _version(distribution.version, f"installed {_DISTRIBUTION} version")
    if installed != expected:
        raise ProvenanceError(
            f"installed {_DISTRIBUTION} version {str(installed)!r} does not equal {str(expected)!r}; "
            "the explicit direct-reference installation assertion failed"
        )
    package_version = _package_version(package_init)
    if package_version != expected:
        raise ProvenanceError(
            f"installed flightsql.__version__ {str(package_version)!r} does not equal {str(expected)!r}"
        )

    installer = (distribution.read_text("INSTALLER") or "").strip().lower()
    if not installer:
        raise ProvenanceError("installed INSTALLER is empty")
    return distribution, expected, installer


def _installed_direct_url(distribution: importlib.metadata.Distribution) -> Dict[str, Any]:
    direct_url_text = distribution.read_text("direct_url.json")
    if direct_url_text is None:
        raise ProvenanceError(
            "PEP 610 direct_url.json is missing; index installations do not carry the asserted fork source"
        )
    try:
        direct_url = json.loads(direct_url_text)
    except json.JSONDecodeError as error:
        raise ProvenanceError("installed direct_url.json is invalid JSON") from error
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
        raise ProvenanceError(f"installed {source_kind} SHA-256 {installed_hash!r} is not {expected_hash}")
    return installed_hash


def verify(
    expected_commit: str,
    expected_version: str = _DEFAULT_VERSION,
    expected_repository: str = _DEFAULT_REPOSITORY,
    expected_artifact_sha256: Optional[str] = None,
    allow_local_artifact: bool = False,
) -> VerificationResult:
    """Verify one distribution and explicitly report the verified scope."""
    if not _FULL_SHA.fullmatch(expected_commit):
        raise ProvenanceError("expected commit must be a full 40-character hexadecimal Git SHA")
    expected_commit = expected_commit.lower()
    expected_hash = _normalized_hash(expected_artifact_sha256) if expected_artifact_sha256 else None
    distribution, expected, installer = _installed_distribution(expected_version)
    direct_url = _installed_direct_url(distribution)

    source_url = direct_url.get("url")
    if not isinstance(source_url, str):
        raise ProvenanceError("installed direct_url.json has no source URL")
    vcs_info = direct_url.get("vcs_info")
    if isinstance(vcs_info, dict):
        _verify_vcs_source(source_url, vcs_info, expected_repository, expected_commit)
        return VerificationResult(
            True,
            _DISTRIBUTION,
            str(expected),
            True,
            "git",
            True,
            expected_commit,
            True,
            expected_commit,
            None,
            installer,
        )

    accepted_archives = {url.lower() for url in _accepted_archive_urls(expected_repository, expected_commit)}
    if source_url.lower() in accepted_archives:
        verified_hash = _verify_archive_source(direct_url, "source-archive", expected_hash)
        return VerificationResult(
            True,
            _DISTRIBUTION,
            str(expected),
            True,
            "source-archive",
            True,
            expected_commit,
            True,
            expected_commit,
            verified_hash,
            installer,
        )

    parsed_source = urlparse(source_url)
    if parsed_source.scheme == "file":
        if not allow_local_artifact:
            raise ProvenanceError(
                "a local artifact does not encode its source commit; use the immutable Git/archive install in "
                "production or pass --allow-local-artifact only for an identity-only artifact test"
            )
        if installer != "pip":
            raise ProvenanceError(
                f"local-artifact verification requires pip, but INSTALLER records {installer!r}; uv local "
                "direct_url.json shapes may omit the artifact digest. Reinstall the exact wheel/sdist with "
                "python -m pip before using --allow-local-artifact"
            )
        if "dir_info" in direct_url or not isinstance(direct_url.get("archive_info"), dict):
            raise ProvenanceError("local-artifact mode requires pip archive_info for an installed wheel or sdist")
        artifact_name = Path(unquote(parsed_source.path)).name
        if not (artifact_name.endswith(".whl") or artifact_name.endswith(".tar.gz") or artifact_name.endswith(".zip")):
            raise ProvenanceError("local-artifact mode requires a wheel or source-distribution file URL")
        verified_hash = _verify_archive_source(direct_url, "local-artifact", expected_hash)
        return VerificationResult(
            True,
            _DISTRIBUTION,
            str(expected),
            True,
            "local-artifact",
            True,
            expected_commit,
            False,
            None,
            verified_hash,
            installer,
        )

    raise ProvenanceError(
        f"installed source {source_url!r} is neither the expected Git repository nor its commit archive"
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-commit", required=True, help="reviewed immutable 40-character Git SHA")
    parser.add_argument("--expected-version", default=_DEFAULT_VERSION)
    parser.add_argument("--expected-repository", default=_DEFAULT_REPOSITORY)
    parser.add_argument("--expected-artifact-sha256")
    parser.add_argument(
        "--allow-local-artifact",
        action="store_true",
        help="with pip only, accept an exact local wheel/sdist hash without claiming its source commit",
    )
    parser.add_argument("--json", action="store_true", help="emit one machine-readable JSON result")
    return parser


def _success_text(result: VerificationResult) -> str:
    prefix = (
        f"verified {result.distribution} {result.installed_version} RECORD integrity; "
        f"source mode {result.source_mode}"
    )
    if result.commit_verified:
        return f"{prefix}; commit verified: {result.verified_commit}"
    return (
        f"{prefix}; local artifact SHA-256 verified: {result.artifact_sha256_verified}; "
        f"commit NOT verified (expected commit {result.expected_commit} is contextual only)"
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        result = verify(
            expected_commit=arguments.expected_commit,
            expected_version=arguments.expected_version,
            expected_repository=arguments.expected_repository,
            expected_artifact_sha256=arguments.expected_artifact_sha256,
            allow_local_artifact=arguments.allow_local_artifact,
        )
    except ProvenanceError as error:
        if arguments.json:
            print(json.dumps({"ok": False, "error": str(error)}, sort_keys=True))
        else:
            print(f"FlightSQL provenance verification failed: {error}", file=sys.stderr)
        return 1
    if arguments.json:
        print(json.dumps(result.to_dict(), sort_keys=True))
    else:
        print(_success_text(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
