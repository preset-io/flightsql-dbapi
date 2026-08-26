"""Verify the installed fork's version and PEP 610 source provenance."""

import argparse
import importlib
import importlib.metadata
import json
import re
from pathlib import Path
from typing import Any, Dict, Optional, Sequence
from urllib.parse import urlparse

_DISTRIBUTION = "flightsql-dbapi"
_DEFAULT_VERSION = "0.2.3+preset.1"
_DEFAULT_REPOSITORY = "https://github.com/preset-io/flightsql-dbapi.git"
_FULL_SHA = re.compile(r"[0-9a-fA-F]{40}\Z")
_SHA256 = re.compile(r"[0-9a-fA-F]{64}\Z")


class ProvenanceError(RuntimeError):
    """The installed distribution does not match the asserted provenance."""


def _normalized_hash(value: str) -> str:
    value = value.removeprefix("sha256=").removeprefix("sha256:")
    if not _SHA256.fullmatch(value):
        raise ProvenanceError("expected artifact SHA-256 must contain exactly 64 hexadecimal characters")
    return value.lower()


def _archive_sha256(direct_url: Dict[str, Any]) -> Optional[str]:
    archive_info = direct_url.get("archive_info") or {}
    hashes = archive_info.get("hashes") or {}
    value = hashes.get("sha256")
    if value is None:
        legacy_hash = archive_info.get("hash")
        if isinstance(legacy_hash, str) and legacy_hash.startswith("sha256="):
            value = legacy_hash.removeprefix("sha256=")
    return value.lower() if isinstance(value, str) else None


def _normalized_repository(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.username or parsed.password or parsed.query or parsed.fragment:
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


def _installed_distribution(expected_version: str) -> importlib.metadata.Distribution:
    try:
        distribution = importlib.metadata.distribution(_DISTRIBUTION)
    except importlib.metadata.PackageNotFoundError as error:
        raise ProvenanceError(f"{_DISTRIBUTION} is not installed") from error

    if distribution.version != expected_version:
        raise ProvenanceError(
            f"installed {_DISTRIBUTION} version {distribution.version!r} does not equal {expected_version!r}; "
            "a public-index build or stale fork may have replaced the pin"
        )

    package = importlib.import_module("flightsql")
    if getattr(package, "__version__", None) != expected_version:
        raise ProvenanceError(
            f"imported flightsql.__version__ {getattr(package, '__version__', None)!r} does not equal "
            f"{expected_version!r}"
        )
    package_file = Path(package.__file__ or "").resolve()
    distribution_root = Path(str(distribution.locate_file(""))).resolve()
    if not package_file.is_relative_to(distribution_root):
        raise ProvenanceError(
            f"imported flightsql at {package_file} is outside installed distribution root {distribution_root}"
        )
    return distribution


def _installed_direct_url(distribution: importlib.metadata.Distribution) -> Dict[str, Any]:
    direct_url_text = distribution.read_text("direct_url.json")
    if direct_url_text is None:
        raise ProvenanceError(
            "PEP 610 direct_url.json is missing; index installations do not prove the reviewed fork source"
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
) -> str:
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
    return "git"


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
    return source_kind


def verify(
    expected_commit: str,
    expected_version: str = _DEFAULT_VERSION,
    expected_repository: str = _DEFAULT_REPOSITORY,
    expected_artifact_sha256: Optional[str] = None,
    allow_local_artifact: bool = False,
) -> str:
    """Verify one installed distribution and return its validated source mode."""
    if not _FULL_SHA.fullmatch(expected_commit):
        raise ProvenanceError("expected commit must be a full 40-character hexadecimal Git SHA")
    expected_commit = expected_commit.lower()
    expected_hash = _normalized_hash(expected_artifact_sha256) if expected_artifact_sha256 else None
    distribution = _installed_distribution(expected_version)
    direct_url = _installed_direct_url(distribution)

    source_url = direct_url.get("url")
    if not isinstance(source_url, str):
        raise ProvenanceError("installed direct_url.json has no source URL")
    vcs_info = direct_url.get("vcs_info")
    if isinstance(vcs_info, dict):
        return _verify_vcs_source(source_url, vcs_info, expected_repository, expected_commit)

    parsed_source = urlparse(source_url)
    if source_url in _accepted_archive_urls(expected_repository, expected_commit):
        return _verify_archive_source(direct_url, "source-archive", expected_hash)

    if parsed_source.scheme == "file":
        if not allow_local_artifact:
            raise ProvenanceError(
                "a local artifact does not encode its source commit; use the immutable Git/archive install in "
                "production or pass --allow-local-artifact only with an externally attested artifact hash"
            )
        return _verify_archive_source(direct_url, "local-artifact", expected_hash)

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
        help="accept an exact local wheel/sdist hash; this does not independently encode the source commit",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        mode = verify(
            expected_commit=arguments.expected_commit,
            expected_version=arguments.expected_version,
            expected_repository=arguments.expected_repository,
            expected_artifact_sha256=arguments.expected_artifact_sha256,
            allow_local_artifact=arguments.allow_local_artifact,
        )
    except ProvenanceError as error:
        print(f"FlightSQL provenance verification failed: {error}", file=__import__("sys").stderr)
        return 1
    print(
        f"verified {_DISTRIBUTION} {arguments.expected_version} from {mode}; "
        f"expected commit {arguments.expected_commit.lower()}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
