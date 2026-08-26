import base64
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

EXPECTED_COMMIT = "a" * 40
EXPECTED_HASH = "b" * 64
ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "verify-installed-provenance"
PACKAGE_FILES = {
    "client.py",
    "dbapi.py",
    "exceptions.py",
    "flightsql_pb2.py",
    "flightsql_pb2.pyi",
    "provenance.py",
    "sqlalchemy.py",
    "util.py",
}


def _record_hash(path):
    digest = hashlib.sha256(path.read_bytes()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _rewrite_record(site_packages):
    dist_info = next(site_packages.glob("flightsql_dbapi-*.dist-info"))
    record = dist_info / "RECORD"
    rows = []
    for path in sorted(path for path in site_packages.rglob("*") if path.is_file() and path != record):
        relative = path.relative_to(site_packages).as_posix()
        rows.append(f"{relative},sha256={_record_hash(path)},{path.stat().st_size}\n")
    rows.append(f"{record.relative_to(site_packages).as_posix()},,\n")
    record.write_text("".join(rows))


def _fake_install(tmp_path, version="0.2.3+preset.1", direct_url=None, installer="pip"):
    site_packages = tmp_path / "site-packages"
    package = site_packages / "flightsql"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(f"__version__ = {version!r}\n")
    for filename in PACKAGE_FILES:
        (package / filename).write_text(f"# modeled installed {filename}\n")

    dist_info = site_packages / f"flightsql_dbapi-{version}.dist-info"
    dist_info.mkdir()
    (dist_info / "METADATA").write_text("Metadata-Version: 2.1\n" "Name: flightsql-dbapi\n" f"Version: {version}\n\n")
    (dist_info / "INSTALLER").write_text(f"{installer}\n")
    if direct_url is not None:
        (dist_info / "direct_url.json").write_text(json.dumps(direct_url))
    _rewrite_record(site_packages)
    return site_packages


def _run_verifier(tmp_path, site_packages, *arguments, script=SCRIPT):
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(site_packages)
    return subprocess.run(
        [
            sys.executable,
            str(script),
            "--expected-commit",
            EXPECTED_COMMIT,
            *arguments,
        ],
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )


def _git_direct_url(commit=EXPECTED_COMMIT):
    return {
        "url": "https://github.com/preset-io/flightsql-dbapi.git",
        "vcs_info": {
            "vcs": "git",
            "commit_id": commit,
            "requested_revision": commit,
        },
    }


def test_trusted_standalone_accepts_exact_git_repository_and_full_commit(tmp_path):
    site_packages = _fake_install(tmp_path, direct_url=_git_direct_url())

    completed = _run_verifier(tmp_path, site_packages)

    assert completed.returncode == 0, completed.stderr
    assert "source mode git" in completed.stdout
    assert f"commit verified: {EXPECTED_COMMIT}" in completed.stdout


def test_trusted_standalone_is_a_copyable_single_file(tmp_path):
    site_packages = _fake_install(tmp_path, direct_url=_git_direct_url())
    copied_script = tmp_path / "trusted-provenance-verifier"
    copied_script.write_bytes(SCRIPT.read_bytes())

    completed = _run_verifier(tmp_path, site_packages, script=copied_script)

    assert completed.returncode == 0, completed.stderr


def test_provenance_json_normalizes_version_and_mixed_case_sha(tmp_path):
    mixed_case_commit = "aA" * 20
    site_packages = _fake_install(tmp_path, direct_url=_git_direct_url(mixed_case_commit))

    completed = _run_verifier(
        tmp_path,
        site_packages,
        "--expected-commit",
        mixed_case_commit.upper(),
        "--expected-version",
        "0.2.3+PRESET.1",
        "--json",
    )

    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result["installed_version"] == "0.2.3+preset.1"
    assert result["expected_commit"] == mixed_case_commit.lower()
    assert result["verified_commit"] == mixed_case_commit.lower()
    assert result["commit_verified"] is True
    assert result["record_integrity_verified"] is True


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
    assert "source mode source-archive" in completed.stdout
    assert f"commit verified: {EXPECTED_COMMIT}" in completed.stdout


@pytest.mark.parametrize(
    ("version", "direct_url", "expected_error"),
    [
        (
            "0.2.2",
            None,
            "explicit direct-reference installation assertion failed",
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


def test_local_artifact_reports_artifact_identity_but_never_claims_fake_commit(tmp_path):
    fake_commit = "f" * 40
    site_packages = _fake_install(
        tmp_path,
        direct_url={
            "url": "file:///tmp/flightsql_dbapi-0.2.3%2Bpreset.1-py3-none-any.whl",
            "archive_info": {"hashes": {"sha256": EXPECTED_HASH}},
        },
    )

    completed = _run_verifier(
        tmp_path,
        site_packages,
        "--expected-commit",
        fake_commit,
        "--expected-artifact-sha256",
        EXPECTED_HASH,
        "--allow-local-artifact",
        "--json",
    )

    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result["source_mode"] == "local-artifact"
    assert result["expected_commit"] == fake_commit
    assert result["commit_verified"] is False
    assert result["verified_commit"] is None
    assert result["artifact_sha256_verified"] == EXPECTED_HASH

    human = _run_verifier(
        tmp_path,
        site_packages,
        "--expected-commit",
        fake_commit,
        "--expected-artifact-sha256",
        EXPECTED_HASH,
        "--allow-local-artifact",
    )
    assert human.returncode == 0, human.stderr
    assert "commit NOT verified" in human.stdout
    assert f"commit verified: {fake_commit}" not in human.stdout


def test_provenance_script_rejects_local_artifact_without_explicit_mode(tmp_path):
    site_packages = _fake_install(
        tmp_path,
        direct_url={
            "url": "file:///tmp/flightsql_dbapi-0.2.3%2Bpreset.1.tar.gz",
            "archive_info": {"hashes": {"sha256": EXPECTED_HASH}},
        },
    )

    rejected = _run_verifier(tmp_path, site_packages, "--expected-artifact-sha256", EXPECTED_HASH)

    assert rejected.returncode == 1
    assert "does not encode its source commit" in rejected.stderr


@pytest.mark.parametrize(
    "uv_direct_url",
    [
        {
            "url": "file:///tmp/flightsql_dbapi-0.2.3%2Bpreset.1-py3-none-any.whl",
            "archive_info": {},
        },
        {
            "url": "file:///tmp/flightsql_dbapi-0.2.3%2Bpreset.1-py3-none-any.whl",
            "archive_info": {"hashes": {"sha256": EXPECTED_HASH}},
        },
    ],
)
def test_uv_local_artifact_direct_url_shapes_fail_with_causal_pip_preflight(tmp_path, uv_direct_url):
    site_packages = _fake_install(tmp_path, direct_url=uv_direct_url, installer="uv")

    completed = _run_verifier(
        tmp_path,
        site_packages,
        "--expected-artifact-sha256",
        EXPECTED_HASH,
        "--allow-local-artifact",
    )

    assert completed.returncode == 1
    assert "local-artifact verification requires pip" in completed.stderr
    assert "uv local direct_url.json shapes may omit the artifact digest" in completed.stderr


def test_record_integrity_rejects_modified_and_unrecorded_package_files(tmp_path):
    modified = _fake_install(tmp_path / "modified", direct_url=_git_direct_url())
    (modified / "flightsql" / "dbapi.py").write_text("# modified after installation\n")

    modified_result = _run_verifier(tmp_path, modified)

    assert modified_result.returncode == 1
    assert "RECORD hash mismatch for 'flightsql/dbapi.py'" in modified_result.stderr

    injected = _fake_install(tmp_path / "injected", direct_url=_git_direct_url())
    (injected / "flightsql" / "injected.py").write_text("# not installer-recorded\n")

    injected_result = _run_verifier(tmp_path, injected)

    assert injected_result.returncode == 1
    assert "unrecorded file flightsql/injected.py" in injected_result.stderr


def test_record_is_scoped_as_integrity_consistency_not_replacement_detection(tmp_path):
    site_packages = _fake_install(tmp_path, direct_url=_git_direct_url())
    completed = _run_verifier(tmp_path, site_packages, "--json")

    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result["record_integrity_verified"] is True
    assert "replacement" not in completed.stdout.lower()
