import base64
import csv
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import packaging
import pytest

EXPECTED_COMMIT = "a" * 40
EXPECTED_VERSION = "0.2.3+preset.2"
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


def _dist_info(site_packages, version=EXPECTED_VERSION):
    return site_packages / f"flightsql_dbapi-{version}.dist-info"


def _rewrite_record(site_packages, version=EXPECTED_VERSION):
    dist_info = _dist_info(site_packages, version)
    record = dist_info / "RECORD"
    rows = []
    for path in sorted(path for path in site_packages.rglob("*") if path.is_file() and path != record):
        relative = path.relative_to(site_packages).as_posix()
        rows.append([relative, f"sha256={_record_hash(path)}", str(path.stat().st_size)])
    rows.append([record.relative_to(site_packages).as_posix(), "", ""])
    stream = io.StringIO()
    csv.writer(stream, lineterminator="\n").writerows(rows)
    record.write_text(stream.getvalue())


def _fake_install(tmp_path, version=EXPECTED_VERSION, direct_url=None, installer="pip"):
    site_packages = tmp_path / "site-packages"
    package = site_packages / "flightsql"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(f"__version__ = {version!r}\n")
    for filename in PACKAGE_FILES:
        (package / filename).write_text(f"# modeled installed {filename}\n")

    dist_info = _dist_info(site_packages, version)
    dist_info.mkdir()
    (dist_info / "METADATA").write_text("Metadata-Version: 2.1\n" "Name: flightsql-dbapi\n" f"Version: {version}\n\n")
    (dist_info / "INSTALLER").write_text(f"{installer}\n")
    if direct_url is not None:
        (dist_info / "direct_url.json").write_text(json.dumps(direct_url))
    _rewrite_record(site_packages, version)
    return site_packages


def _run_verifier(tmp_path, site_packages, *arguments, script=SCRIPT):
    dependency_root = tmp_path / "standalone-dependencies"
    packaged_dependency = dependency_root / "packaging"
    if not packaged_dependency.exists():
        dependency_root.mkdir(exist_ok=True)
        shutil.copytree(Path(packaging.__file__).parent, packaged_dependency)
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join((str(site_packages), str(dependency_root)))
    return subprocess.run(
        [
            sys.executable,
            "-S",
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
    assert f"recorded source commit matched: {EXPECTED_COMMIT}" in completed.stdout


def test_trusted_standalone_is_a_copyable_single_file(tmp_path):
    site_packages = _fake_install(tmp_path, direct_url=_git_direct_url())
    copied_script = tmp_path / "trusted-provenance-verifier"
    copied_script.write_bytes(SCRIPT.read_bytes())

    completed = _run_verifier(tmp_path, site_packages, script=copied_script)

    assert completed.returncode == 0, completed.stderr


def test_provenance_json_names_the_narrow_recorded_hash_scope(tmp_path):
    mixed_case_commit = "aA" * 20
    site_packages = _fake_install(tmp_path, direct_url=_git_direct_url(mixed_case_commit))

    completed = _run_verifier(
        tmp_path,
        site_packages,
        "--expected-commit",
        mixed_case_commit.upper(),
        "--expected-version",
        "0.2.3+PRESET.2",
        "--json",
    )

    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result["installed_version"] == EXPECTED_VERSION
    assert result["expected_commit"] == mixed_case_commit.lower()
    assert result["verified_commit"] == mixed_case_commit.lower()
    assert result["commit_verified"] is True
    assert result["package_source_record_sha256_verified"] is True
    assert result["distribution_metadata_record_sha256_verified"] is True
    assert result["direct_url_record_sha256_verified"] is True
    assert "record_integrity_verified" not in result
    assert "environment" not in " ".join(result).lower()


def test_provenance_script_accepts_exact_commit_archive_and_recorded_hash(tmp_path):
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
        "--json",
    )

    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result["source_mode"] == "source-archive"
    assert result["commit_verified"] is True
    assert result["recorded_archive_sha256_matched"] == EXPECTED_HASH
    assert result["local_artifact_file_sha256_verified"] is None


@pytest.mark.parametrize(
    ("version", "direct_url", "expected_error"),
    [
        (
            "0.2.2",
            _git_direct_url(),
            "explicit direct-reference installation assertion failed",
        ),
        (
            EXPECTED_VERSION,
            None,
            "selected distribution metadata omits required files: direct_url.json",
        ),
        (
            EXPECTED_VERSION,
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
            EXPECTED_VERSION,
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
            EXPECTED_VERSION,
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


def test_forged_expected_sha_in_actual_direct_url_fails_causally_before_source_claim(tmp_path):
    originally_recorded = _git_direct_url("c" * 40)
    site_packages = _fake_install(tmp_path, direct_url=originally_recorded)
    direct_url_path = _dist_info(site_packages) / "direct_url.json"
    # This models the review's attack: forge only the PEP 610 claim to the
    # expected SHA.  RECORD still hashes the causal pre-forgery metadata.
    direct_url_path.write_text(json.dumps(_git_direct_url(EXPECTED_COMMIT)))

    completed = _run_verifier(tmp_path, site_packages)

    assert completed.returncode == 1
    assert "RECORD SHA-256 mismatch" in completed.stderr
    assert "direct_url.json" in completed.stderr
    assert "recorded source commit matched" not in completed.stdout


def test_actual_direct_url_must_be_present_and_hash_covered_in_actual_record(tmp_path):
    site_packages = _fake_install(tmp_path, direct_url=_git_direct_url())
    record = _dist_info(site_packages) / "RECORD"
    rows = list(csv.reader(io.StringIO(record.read_text())))
    rows = [row for row in rows if not row[0].endswith("/direct_url.json")]
    stream = io.StringIO()
    csv.writer(stream, lineterminator="\n").writerows(rows)
    record.write_text(stream.getvalue())

    completed = _run_verifier(tmp_path, site_packages)

    assert completed.returncode == 1
    assert "RECORD omits scoped package file" in completed.stderr
    assert "direct_url.json" in completed.stderr


def test_record_cannot_select_a_decoy_dist_info_for_source_evidence(tmp_path):
    site_packages = _fake_install(tmp_path, direct_url=_git_direct_url())
    actual = _dist_info(site_packages)
    decoy = site_packages / "attacker.dist-info"
    decoy.mkdir()
    (decoy / "METADATA").write_text("Metadata-Version: 2.1\nName: attacker\nVersion: 1\n\n")
    for name in ("INSTALLER", "direct_url.json"):
        (decoy / name).write_bytes((actual / name).read_bytes())
    _rewrite_record(site_packages)

    completed = _run_verifier(tmp_path, site_packages)

    assert completed.returncode == 1
    assert "decoy distribution metadata path" in completed.stderr


def test_duplicate_distribution_dist_info_is_rejected_before_source_claim(tmp_path):
    site_packages = _fake_install(tmp_path, direct_url=_git_direct_url())
    duplicate = site_packages / "flightsql_dbapi-9.9.dist-info"
    duplicate.mkdir()
    (duplicate / "METADATA").write_text("Metadata-Version: 2.1\nName: flightsql-dbapi\nVersion: 9.9\n\n")

    completed = _run_verifier(tmp_path, site_packages)

    assert completed.returncode == 1
    assert "multiple flightsql-dbapi distribution metadata directories" in completed.stderr or (
        "duplicate or decoy flightsql-dbapi" in completed.stderr
    )


def test_symlinked_dist_info_alias_is_rejected_as_a_decoy(tmp_path):
    site_packages = _fake_install(tmp_path, direct_url=_git_direct_url())
    actual = _dist_info(site_packages)
    (site_packages / "flightsql_dbapi-decoy.dist-info").symlink_to(actual, target_is_directory=True)

    completed = _run_verifier(tmp_path, site_packages)

    assert completed.returncode == 1
    assert "duplicate or decoy flightsql-dbapi" in completed.stderr or "not one regular .dist-info" in completed.stderr


def test_record_rejects_path_traversal_and_unsafe_hash_algorithm(tmp_path):
    traversal = _fake_install(tmp_path / "traversal", direct_url=_git_direct_url())
    traversal_record = _dist_info(traversal) / "RECORD"
    traversal_record.write_text("../outside.py,sha256=" + "a" * 43 + ",1\n" + traversal_record.read_text())

    traversal_result = _run_verifier(tmp_path, traversal)

    assert traversal_result.returncode == 1
    assert "unsafe path traversal" in traversal_result.stderr

    weak = _fake_install(tmp_path / "weak", direct_url=_git_direct_url())
    weak_record = _dist_info(weak) / "RECORD"
    weak_record.write_text(weak_record.read_text().replace("flightsql/dbapi.py,sha256=", "flightsql/dbapi.py,md5=", 1))

    weak_result = _run_verifier(tmp_path, weak)

    assert weak_result.returncode == 1
    assert "unsafe or unsupported hash algorithm 'md5'" in weak_result.stderr


def test_direct_url_rejects_unsafe_artifact_hash_and_duplicate_json_keys(tmp_path):
    weak = _fake_install(
        tmp_path / "weak",
        direct_url={
            "url": f"https://github.com/preset-io/flightsql-dbapi/archive/{EXPECTED_COMMIT}.tar.gz",
            "archive_info": {"hashes": {"md5": "0" * 32, "sha256": EXPECTED_HASH}},
        },
    )

    weak_result = _run_verifier(tmp_path, weak, "--expected-artifact-sha256", EXPECTED_HASH)

    assert weak_result.returncode == 1
    assert "unsafe artifact hash algorithm 'md5'" in weak_result.stderr

    duplicate = _fake_install(tmp_path / "duplicate", direct_url=_git_direct_url())
    direct_url_path = _dist_info(duplicate) / "direct_url.json"
    direct_url_path.write_text(
        '{"url":"https://github.com/preset-io/flightsql-dbapi.git",'
        '"url":"https://github.com/preset-io/flightsql-dbapi.git",'
        f'"vcs_info":{json.dumps(_git_direct_url()["vcs_info"])}}}'
    )
    _rewrite_record(duplicate)

    duplicate_result = _run_verifier(tmp_path, duplicate)

    assert duplicate_result.returncode == 1
    assert "duplicate key 'url'" in duplicate_result.stderr


def test_json_mode_returns_structured_failures_for_json_and_argument_errors(tmp_path):
    site_packages = _fake_install(tmp_path, direct_url=_git_direct_url())
    direct_url_path = _dist_info(site_packages) / "direct_url.json"
    direct_url_path.write_text("{not valid JSON")
    _rewrite_record(site_packages)

    malformed = _run_verifier(tmp_path, site_packages, "--json")
    bad_argument = _run_verifier(tmp_path, site_packages, "--not-an-option", "--json")
    malformed_json_option = _run_verifier(tmp_path, site_packages, "--json=unexpected")

    for completed in (malformed, bad_argument, malformed_json_option):
        assert completed.returncode == 1
        assert completed.stderr == ""
        result = json.loads(completed.stdout)
        assert result["ok"] is False
        assert result["error_type"] == "verification_failed"


def test_json_mode_structures_unexpected_operational_errors(monkeypatch, capsys):
    import flightsql.provenance as provenance

    def unavailable(**kwargs):
        raise OSError("metadata filesystem unavailable")

    monkeypatch.setattr(provenance, "verify", unavailable)

    returncode = provenance.main(["--expected-commit", EXPECTED_COMMIT, "--json"])

    result = json.loads(capsys.readouterr().out)
    assert returncode == 2
    assert result == {
        "error": "OSError: metadata filesystem unavailable",
        "error_type": "operational_error",
        "ok": False,
    }


def test_standalone_json_mode_structures_missing_runtime_dependency(tmp_path):
    site_packages = _fake_install(tmp_path, direct_url=_git_direct_url())
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(site_packages)

    completed = subprocess.run(
        [
            sys.executable,
            "-S",
            str(SCRIPT),
            "--expected-commit",
            EXPECTED_COMMIT,
            "--json",
        ],
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )

    result = json.loads(completed.stdout)
    assert completed.returncode == 2
    assert completed.stderr == ""
    assert result["ok"] is False
    assert result["error_type"] == "operational_error"
    assert "packaging dependency is unavailable" in result["error"]


def test_local_artifact_hashes_referenced_file_but_never_claims_fake_commit(tmp_path):
    fake_commit = "f" * 40
    # The literal percent proves the file URL is decoded exactly once.
    artifact = tmp_path / f"flightsql_dbapi-{EXPECTED_VERSION}-%20-py3-none-any.whl"
    artifact.write_bytes(b"reviewed local wheel bytes")
    artifact_hash = hashlib.sha256(artifact.read_bytes()).hexdigest()
    site_packages = _fake_install(
        tmp_path / "install",
        direct_url={
            "url": artifact.as_uri(),
            "archive_info": {},
        },
    )

    completed = _run_verifier(
        tmp_path,
        site_packages,
        "--expected-commit",
        fake_commit,
        "--expected-artifact-sha256",
        artifact_hash,
        "--allow-local-artifact",
        "--json",
    )

    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result["source_mode"] == "local-artifact"
    assert result["expected_commit"] == fake_commit
    assert result["commit_verified"] is False
    assert result["verified_commit"] is None
    assert result["recorded_archive_sha256_matched"] is None
    assert result["local_artifact_file_sha256_verified"] == artifact_hash

    human = _run_verifier(
        tmp_path,
        site_packages,
        "--expected-commit",
        fake_commit,
        "--expected-artifact-sha256",
        artifact_hash,
        "--allow-local-artifact",
    )
    assert human.returncode == 0, human.stderr
    assert "commit NOT verified" in human.stdout
    assert "recorded source commit matched" not in human.stdout


def test_provenance_script_rejects_local_artifact_without_explicit_mode(tmp_path):
    artifact = tmp_path / f"flightsql_dbapi-{EXPECTED_VERSION}.tar.gz"
    artifact.write_bytes(b"local sdist")
    artifact_hash = hashlib.sha256(artifact.read_bytes()).hexdigest()
    site_packages = _fake_install(
        tmp_path / "install",
        direct_url={
            "url": artifact.as_uri(),
            "archive_info": {},
        },
    )

    rejected = _run_verifier(tmp_path, site_packages, "--expected-artifact-sha256", artifact_hash)

    assert rejected.returncode == 1
    assert "does not encode its source commit" in rejected.stderr


@pytest.mark.parametrize(
    "uv_direct_url",
    [
        {
            "url": f"file:///tmp/flightsql_dbapi-{EXPECTED_VERSION}-py3-none-any.whl",
            "archive_info": {},
        },
        {
            "url": f"file:///tmp/flightsql_dbapi-{EXPECTED_VERSION}-py3-none-any.whl",
            "archive_info": {"hashes": {"sha256": EXPECTED_HASH}},
        },
    ],
)
def test_local_artifact_hashes_referenced_file_without_trusting_installer_archive_metadata(tmp_path, uv_direct_url):
    artifact = tmp_path / f"flightsql_dbapi-{EXPECTED_VERSION}-py3-none-any.whl"
    artifact.write_bytes(b"local artifact bytes")
    artifact_hash = hashlib.sha256(artifact.read_bytes()).hexdigest()
    uv_direct_url["url"] = artifact.as_uri()
    site_packages = _fake_install(tmp_path / "install", direct_url=uv_direct_url, installer="uv")

    completed = _run_verifier(
        tmp_path,
        site_packages,
        "--expected-artifact-sha256",
        artifact_hash,
        "--allow-local-artifact",
        "--json",
    )

    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result["local_artifact_file_sha256_verified"] == artifact_hash


def test_local_artifact_rejects_missing_or_mismatched_referenced_file(tmp_path):
    artifact = tmp_path / f"flightsql_dbapi-{EXPECTED_VERSION}.tar.gz"
    artifact.write_bytes(b"original bytes")
    site_packages = _fake_install(
        tmp_path / "install",
        direct_url={"url": artifact.as_uri(), "archive_info": {}},
    )
    wrong_hash = hashlib.sha256(b"different bytes").hexdigest()

    mismatch = _run_verifier(
        tmp_path,
        site_packages,
        "--expected-artifact-sha256",
        wrong_hash,
        "--allow-local-artifact",
    )
    artifact.unlink()
    missing = _run_verifier(
        tmp_path,
        site_packages,
        "--expected-artifact-sha256",
        wrong_hash,
        "--allow-local-artifact",
    )

    assert mismatch.returncode == 1
    assert "local-artifact file SHA-256" in mismatch.stderr
    assert missing.returncode == 1
    assert "does not reference an available regular" in missing.stderr


def test_recorded_package_scope_rejects_modified_and_unrecorded_source_files(tmp_path):
    modified = _fake_install(tmp_path / "modified", direct_url=_git_direct_url())
    (modified / "flightsql" / "dbapi.py").write_text("# modified after installation\n")

    modified_result = _run_verifier(tmp_path, modified)

    assert modified_result.returncode == 1
    assert "RECORD SHA-256 mismatch" in modified_result.stderr
    assert "flightsql/dbapi.py" in modified_result.stderr

    injected = _fake_install(tmp_path / "injected", direct_url=_git_direct_url())
    (injected / "flightsql" / "injected.py").write_text("# not installer-recorded\n")

    injected_result = _run_verifier(tmp_path, injected)

    assert injected_result.returncode == 1
    assert "RECORD omits scoped package file 'flightsql/injected.py'" in injected_result.stderr


def test_unrecorded_bytecode_is_explicitly_outside_source_hash_scope(tmp_path):
    site_packages = _fake_install(tmp_path, direct_url=_git_direct_url())
    pycache = site_packages / "flightsql" / "__pycache__"
    pycache.mkdir()
    (pycache / "injected.cpython-310.pyc").write_bytes(b"unchecked bytecode")

    completed = _run_verifier(tmp_path, site_packages, "--json")

    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result["package_source_record_sha256_verified"] is True
    assert all("bytecode" not in key for key in result)

    # The exception is by file type, not by directory name: non-bytecode code
    # under __pycache__ remains a package-owned regular file in scope.
    (pycache / "injected.py").write_text("# unrecorded source\n")
    source_result = _run_verifier(tmp_path, site_packages)
    assert source_result.returncode == 1
    assert "RECORD omits scoped package file" in source_result.stderr
    assert "__pycache__/injected.py" in source_result.stderr
