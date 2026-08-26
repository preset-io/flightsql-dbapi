import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

EXPECTED_COMMIT = "a" * 40
EXPECTED_HASH = "b" * 64
PROVENANCE_SPEC = importlib.util.find_spec("flightsql.provenance")
assert PROVENANCE_SPEC is not None and PROVENANCE_SPEC.origin is not None
SCRIPT = Path(PROVENANCE_SPEC.origin)


def _fake_install(tmp_path, version="0.2.3+preset.1", direct_url=None):
    site_packages = tmp_path / "site-packages"
    package = site_packages / "flightsql"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(f"__version__ = {version!r}\n")

    dist_info = site_packages / f"flightsql_dbapi-{version}.dist-info"
    dist_info.mkdir()
    (dist_info / "METADATA").write_text("Metadata-Version: 2.1\n" "Name: flightsql-dbapi\n" f"Version: {version}\n\n")
    if direct_url is not None:
        (dist_info / "direct_url.json").write_text(json.dumps(direct_url))
    return site_packages


def _run_verifier(tmp_path, site_packages, *arguments):
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(site_packages)
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--expected-commit",
            EXPECTED_COMMIT,
            *arguments,
        ],
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )


def test_provenance_script_accepts_exact_git_repository_and_full_commit(tmp_path):
    site_packages = _fake_install(
        tmp_path,
        direct_url={
            "url": "https://github.com/preset-io/flightsql-dbapi.git",
            "vcs_info": {
                "vcs": "git",
                "commit_id": EXPECTED_COMMIT,
                "requested_revision": EXPECTED_COMMIT,
            },
        },
    )

    completed = _run_verifier(tmp_path, site_packages)

    assert completed.returncode == 0, completed.stderr
    assert "from git" in completed.stdout


def test_provenance_script_accepts_exact_commit_archive_and_hash(tmp_path):
    site_packages = _fake_install(
        tmp_path,
        direct_url={
            "url": f"https://github.com/preset-io/flightsql-dbapi/archive/{EXPECTED_COMMIT}.tar.gz",
            "archive_info": {"hashes": {"sha256": EXPECTED_HASH}},
        },
    )

    completed = _run_verifier(
        tmp_path,
        site_packages,
        "--expected-artifact-sha256",
        EXPECTED_HASH,
    )

    assert completed.returncode == 0, completed.stderr
    assert "from source-archive" in completed.stdout


@pytest.mark.parametrize(
    ("version", "direct_url", "expected_error"),
    [
        (
            "0.2.2",
            None,
            "public-index build or stale fork",
        ),
        (
            "0.2.3+preset.1",
            None,
            "direct_url.json is missing",
        ),
        (
            "0.2.3+preset.1",
            {
                "url": "https://github.com/other/flightsql-dbapi.git",
                "vcs_info": {
                    "vcs": "git",
                    "commit_id": EXPECTED_COMMIT,
                    "requested_revision": EXPECTED_COMMIT,
                },
            },
            "is not 'https://github.com/preset-io/flightsql-dbapi.git'",
        ),
        (
            "0.2.3+preset.1",
            {
                "url": "https://github.com/preset-io/flightsql-dbapi.git",
                "vcs_info": {
                    "vcs": "git",
                    "commit_id": "c" * 40,
                    "requested_revision": EXPECTED_COMMIT,
                },
            },
            "installed VCS commit",
        ),
        (
            "0.2.3+preset.1",
            {
                "url": "https://github.com/preset-io/flightsql-dbapi.git",
                "vcs_info": {
                    "vcs": "git",
                    "commit_id": EXPECTED_COMMIT,
                    "requested_revision": "main",
                },
            },
            "requested VCS revision 'main'",
        ),
    ],
)
def test_provenance_script_rejects_public_downgrade_and_wrong_provenance(tmp_path, version, direct_url, expected_error):
    site_packages = _fake_install(tmp_path, version=version, direct_url=direct_url)

    completed = _run_verifier(tmp_path, site_packages)

    assert completed.returncode == 1
    assert expected_error in completed.stderr


def test_provenance_script_rejects_local_artifact_without_explicit_hash_attestation(tmp_path):
    site_packages = _fake_install(
        tmp_path,
        direct_url={
            "url": "file:///tmp/flightsql_dbapi.whl",
            "archive_info": {"hashes": {"sha256": EXPECTED_HASH}},
        },
    )

    rejected = _run_verifier(tmp_path, site_packages, "--expected-artifact-sha256", EXPECTED_HASH)
    accepted = _run_verifier(
        tmp_path,
        site_packages,
        "--expected-artifact-sha256",
        EXPECTED_HASH,
        "--allow-local-artifact",
    )

    assert rejected.returncode == 1
    assert "does not encode its source commit" in rejected.stderr
    assert accepted.returncode == 0, accepted.stderr
    assert "from local-artifact" in accepted.stdout
