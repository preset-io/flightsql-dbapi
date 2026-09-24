import subprocess
import sys
from importlib.metadata import distribution, requires, version

from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.version import Version


def test_sqlalchemy_dependency_supports_1_4_and_2_x():
    dependencies = (Requirement(value) for value in requires("flightsql-dbapi") or [])
    sqlalchemy = next(dependency for dependency in dependencies if dependency.name == "sqlalchemy")

    assert Version("1.4.6") in sqlalchemy.specifier
    assert Version("2.0") in sqlalchemy.specifier
    assert Version("2.99") in sqlalchemy.specifier
    assert Version("1.4.5") not in sqlalchemy.specifier
    assert Version("1.3.24") not in sqlalchemy.specifier
    assert Version("3.0") not in sqlalchemy.specifier


def test_provenance_version_normalizer_is_a_runtime_dependency():
    dependencies = (Requirement(value) for value in requires("flightsql-dbapi") or [])
    packaging = next(dependency for dependency in dependencies if dependency.name == "packaging")

    assert Version("21.3") in packaging.specifier


def test_sqlalchemy_entrypoint_registers_documented_driver_name():
    entrypoints = {entrypoint.name: entrypoint.value for entrypoint in distribution("flightsql-dbapi").entry_points}

    assert entrypoints["datafusion.flightsql"] == "flightsql.sqlalchemy:DataFusionDialect"


def test_untrusted_installed_provenance_console_entrypoint_is_absent():
    entrypoints = {entrypoint.name: entrypoint.value for entrypoint in distribution("flightsql-dbapi").entry_points}

    assert "flightsql-verify-provenance" not in entrypoints


def test_fork_has_non_pypi_release_identity():
    installed_version = Version(version("flightsql-dbapi"))

    assert installed_version == Version("0.2.2.1")
    assert installed_version.local is None


def test_fork_version_is_distinct_from_every_public_release():
    # The fork version is a four-component public version, not a PEP 440 local
    # version.  That is the whole point: a local version such as 0.2.3+preset.2
    # is *matched* by the specifier ==0.2.3, so a resolver asked for the public
    # release can silently select the Preset build and vice versa.  A fourth
    # component is a distinct release that no public specifier admits.
    fork = Version("0.2.2.1")
    upstream = Version("0.2.2")

    assert fork not in SpecifierSet(f"=={upstream}")
    assert upstream not in SpecifierSet(f"=={fork}")
    assert fork.local is None

    # It sorts above the last public release and below the next one upstream
    # could ever publish, so it never shadows a future public version.
    assert fork > upstream
    assert fork < Version("0.2.3")


def test_packaging_is_honest_about_partial_typing_support():
    installed_files = {str(path) for path in distribution("flightsql-dbapi").files or []}

    assert "flightsql/flightsql_pb2.pyi" in installed_files
    assert "flightsql/py.typed" not in installed_files


def test_documented_entrypoint_loads_in_a_cold_interpreter(tmp_path):
    script = """
import sys
import warnings

assert "flightsql.sqlalchemy" not in sys.modules

from sqlalchemy import create_engine
from sqlalchemy.exc import SADeprecationWarning

warnings.simplefilter("error", SADeprecationWarning)
engine = create_engine("datafusion+flightsql://localhost:443")
assert type(engine.dialect).__name__ == "DataFusionDialect"
engine.dispose()
"""

    completed = subprocess.run(
        [sys.executable, "-I", "-c", script],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
