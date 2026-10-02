import hashlib
import importlib.machinery
import importlib.util
import io
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "verify-stored-wheel"
FILENAME = "flightsql_dbapi-1.0-py3-none-any.whl"
VERSION = "1.0"
METADATA = b"Metadata-Version: 2.1\nName: flightsql-dbapi\nVersion: 1.0\n" + b"".join(
    b"Keywords: keyword-%d\n" % i for i in range(200)
)
MEMBER = "flightsql_dbapi-1.0.dist-info/METADATA"


def build_wheel(metadata=METADATA):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as wheel:
        wheel.writestr(MEMBER, metadata)
    return buffer.getvalue()


def run(tmp_path, wheel_bytes):
    stored = tmp_path / "stored.whl"
    stored.write_bytes(wheel_bytes)
    digest = hashlib.sha256(wheel_bytes).hexdigest()
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(stored), FILENAME, VERSION, digest],
        capture_output=True,
        text=True,
    )


def assert_clean_failure(result, message):
    assert result.returncode == 1
    assert "Traceback" not in result.stderr
    lines = result.stderr.strip().splitlines()
    assert len(lines) == 1
    assert lines[0].startswith("verify-stored-wheel: ")
    assert message in lines[0]


def test_good_wheel_verifies(tmp_path):
    result = run(tmp_path, build_wheel())
    assert result.returncode == 0, result.stderr


def test_corrupt_deflate_data_is_reported_without_traceback(tmp_path):
    wheel = bytearray(build_wheel())
    start = 30 + len(MEMBER)  # first byte of the member's compressed data
    for offset in range(start + 5, start + 25):
        wheel[offset] ^= 0xFF
    assert_clean_failure(run(tmp_path, bytes(wheel)), "not a readable wheel")


def test_non_utf8_metadata_is_reported_without_traceback(tmp_path):
    metadata = METADATA + b"Summary: \xff\xfe\n"
    assert_clean_failure(run(tmp_path, build_wheel(metadata)), "not valid UTF-8")


def test_truncated_wheel_is_reported_without_traceback(tmp_path):
    wheel = build_wheel()
    assert_clean_failure(run(tmp_path, wheel[: len(wheel) // 2]), "not a readable wheel")


def test_member_cut_short_is_reported_without_traceback(tmp_path, monkeypatch, capsys):
    loader = importlib.machinery.SourceFileLoader("verify_stored_wheel", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)

    def short_read(self, name, pwd=None):
        raise EOFError("Compressed file ended before the end-of-stream marker was reached")

    monkeypatch.setattr(zipfile.ZipFile, "read", short_read)
    wheel_bytes = build_wheel()
    stored = tmp_path / "stored.whl"
    stored.write_bytes(wheel_bytes)
    digest = hashlib.sha256(wheel_bytes).hexdigest()

    status = module.main(["verify-stored-wheel", str(stored), FILENAME, VERSION, digest])

    stderr = capsys.readouterr().err
    assert status == 1
    assert len(stderr.strip().splitlines()) == 1
    assert "not a readable wheel" in stderr


@pytest.mark.parametrize("version", ["1.0.0", "2.0"])
def test_wrong_version_still_fails(tmp_path, version):
    wheel_bytes = build_wheel()
    stored = tmp_path / "stored.whl"
    stored.write_bytes(wheel_bytes)
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            str(stored),
            FILENAME,
            version,
            hashlib.sha256(wheel_bytes).hexdigest(),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
