import hashlib
import io
import struct
import subprocess
import sys
import zipfile
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "verify-stored-wheel"
FILENAME = "flightsql_dbapi-0.2.2.5-py3-none-any.whl"
VERSION = "0.2.2.5"
METADATA_NAME = "flightsql_dbapi-0.2.2.5.dist-info/METADATA"
METADATA = b"Metadata-Version: 2.1\nName: flightsql-dbapi\nVersion: 0.2.2.5\n"


def _wheel(metadata=METADATA):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as wheel:
        wheel.writestr("flightsql/__init__.py", b"")
        wheel.writestr(METADATA_NAME, metadata)
    return bytearray(buffer.getvalue())


def _corrupt_metadata_deflate(data):
    """Give METADATA's deflate stream an invalid block type; sizes stay consistent."""
    header = data.index(b"PK\x03\x04", data.index(b"PK\x03\x04") + 4)
    name_length, extra_length = struct.unpack("<HH", data[header + 26 : header + 30])
    data[header + 30 + name_length + extra_length] = 0xFF
    return data


def _verify(tmp_path, data, version=VERSION):
    stored = tmp_path / "stored.whl"
    stored.write_bytes(bytes(data))
    digest = hashlib.sha256(bytes(data)).hexdigest()
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(stored), FILENAME, version, digest], capture_output=True, text=True
    )


def _assert_clean_failure(result, message):
    """The verifier must fail with exit 1 and a single explanatory line, never a traceback."""
    assert result.returncode == 1
    assert "Traceback" not in result.stderr
    lines = result.stderr.strip().splitlines()
    assert len(lines) == 1, result.stderr
    assert lines[0].startswith("verify-stored-wheel: ")
    assert message in lines[0]


def test_good_wheel_is_verified(tmp_path):
    result = _verify(tmp_path, _wheel())
    assert result.returncode == 0, result.stderr
    assert "verified" in result.stdout


def test_corrupt_deflate_fails_cleanly_without_a_traceback(tmp_path):
    _assert_clean_failure(_verify(tmp_path, _corrupt_metadata_deflate(_wheel())), "not a readable wheel")


def test_non_utf8_metadata_fails_cleanly_without_a_traceback(tmp_path):
    data = _wheel(METADATA + b"Summary: \xff\xfe\n")
    _assert_clean_failure(_verify(tmp_path, data), "wheel METADATA is not valid UTF-8")


def test_truncated_wheel_fails_cleanly_without_a_traceback(tmp_path):
    data = _wheel()
    _assert_clean_failure(_verify(tmp_path, data[: len(data) // 2]), "not a readable wheel")


def test_truncated_member_stream_fails_cleanly(tmp_path, monkeypatch, capsys):
    loader = SourceFileLoader("verify_stored_wheel", str(SCRIPT))
    module = module_from_spec(spec_from_loader(loader.name, loader))
    loader.exec_module(module)
    data = bytes(_wheel())
    stored = tmp_path / "stored.whl"
    stored.write_bytes(data)

    def truncated(self, name, pwd=None):
        raise EOFError("member stream ended early")

    monkeypatch.setattr(zipfile.ZipFile, "read", truncated)
    argv = ["verify-stored-wheel", str(stored), FILENAME, VERSION, hashlib.sha256(data).hexdigest()]
    assert module.main(argv) == 1
    assert "not a readable wheel: member stream ended early" in capsys.readouterr().err


@pytest.mark.parametrize("version", ["0.2.2.5.1", "0.2.3"])
def test_version_disagreement_still_fails(tmp_path, version):
    result = _verify(tmp_path, _wheel(), version=version)
    assert result.returncode == 1
    assert "Traceback" not in result.stderr
