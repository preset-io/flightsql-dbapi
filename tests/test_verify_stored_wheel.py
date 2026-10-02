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


def _verify(tmp_path, data):
    stored = tmp_path / "stored.whl"
    stored.write_bytes(bytes(data))
    digest = hashlib.sha256(bytes(data)).hexdigest()
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(stored), FILENAME, "0.2.2.5", digest], capture_output=True, text=True
    )


def test_good_wheel_is_verified(tmp_path):
    result = _verify(tmp_path, _wheel())
    assert result.returncode == 0, result.stderr
    assert "verified" in result.stdout


@pytest.mark.parametrize(
    "data",
    [
        pytest.param(_corrupt_metadata_deflate(_wheel()), id="corrupt-deflate"),
        pytest.param(_wheel(METADATA + b"Summary: \xff\xfe\n"), id="non-utf8-metadata"),
    ],
)
def test_unreadable_wheel_fails_cleanly_without_a_traceback(tmp_path, data):
    result = _verify(tmp_path, data)
    assert result.returncode == 1
    assert "stored object is not a readable wheel" in result.stderr
    assert "Traceback" not in result.stderr


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
    argv = ["verify-stored-wheel", str(stored), FILENAME, "0.2.2.5", hashlib.sha256(data).hexdigest()]
    assert module.main(argv) == 1
    assert "not a readable wheel: member stream ended early" in capsys.readouterr().err
