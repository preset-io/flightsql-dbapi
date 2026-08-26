import warnings
from types import SimpleNamespace

import pytest
from sqlalchemy import Integer, String, bindparam, column, create_engine, select, table
from sqlalchemy.dialects import registry
from sqlalchemy.engine import URL
from sqlalchemy.exc import ArgumentError, SADeprecationWarning

import flightsql
from flightsql.sqlalchemy import (
    FEATURE_PRIMARY_KEYS,
    DataFusionDialect,
    FlightSQLDialect,
    LiteralBindCompiler,
    client_from_url,
)

CUSTOM_DBAPI = SimpleNamespace(paramstyle="qmark")
LEGACY_DBAPI = SimpleNamespace(paramstyle="qmark")


class DocumentedExtensionDialect(FlightSQLDialect):
    """Equivalent to the custom-dialect extension documented in the README."""

    name = "reviewextension"
    paramstyle = "qmark"


class CustomLoaderDialect(FlightSQLDialect):
    name = "customloader"

    @classmethod
    def import_dbapi(cls, loader_token=None):
        assert loader_token in (None, "accepted")
        return CUSTOM_DBAPI


class CustomLoaderChildDialect(CustomLoaderDialect):
    name = "customloaderchild"


class LegacyLoaderDialect(FlightSQLDialect):
    name = "legacyloader"

    @classmethod
    def dbapi(cls, legacy_token=None):
        assert legacy_token in (None, "accepted")
        return LEGACY_DBAPI


class LegacyLoaderChildDialect(LegacyLoaderDialect):
    name = "legacyloaderchild"


class StubDBAPIConnection:
    def __init__(self):
        self.features = {}

    def flightsql_get_table_metadata(self, schema):
        return {"records": [{"name": "records", "schema": schema}]}

    def flightsql_get_columns(self, table_name, schema):
        if table_name == "missing":
            return []
        return [{"name": table_name, "schema": schema}]

    def flightsql_get_table_names(self, schema):
        return ["records", "missing"]

    def flightsql_get_schema_names(self):
        return ["public"]

    def flightsql_get_primary_keys(self, table_name, schema):
        assert table_name == "records"
        assert schema == "public"
        return [{"column_name": "id", "key_name": "records_pk"}]


class StubConnection:
    def __init__(self):
        self.connection = StubDBAPIConnection()


def test_dbapi_entrypoints_support_both_sqlalchemy_generations():
    assert FlightSQLDialect.import_dbapi() is flightsql
    assert FlightSQLDialect.dbapi() is flightsql


def test_create_engine_uses_the_current_dbapi_entrypoint_without_deprecation():
    url = URL.create(drivername="datafusion+flightsql", host="localhost", port=443)

    with warnings.catch_warnings():
        warnings.simplefilter("error", SADeprecationWarning)
        engine = create_engine(url)

    assert isinstance(engine.dialect, DataFusionDialect)
    engine.dispose()


def test_documented_dialect_subclass_uses_current_dbapi_entrypoint_without_deprecation():
    registry.register("reviewextension.flightsql", __name__, "DocumentedExtensionDialect")
    url = URL.create(drivername="reviewextension+flightsql", host="localhost", port=443)

    with warnings.catch_warnings():
        warnings.simplefilter("error", SADeprecationWarning)
        engine = create_engine(url)

    assert "import_dbapi" in DocumentedExtensionDialect.__dict__
    assert isinstance(engine.dialect, DocumentedExtensionDialect)
    engine.dispose()


@pytest.mark.parametrize(
    ("dialect_cls", "expected"),
    [
        (CustomLoaderDialect, CUSTOM_DBAPI),
        (CustomLoaderChildDialect, CUSTOM_DBAPI),
        (LegacyLoaderDialect, LEGACY_DBAPI),
        (LegacyLoaderChildDialect, LEGACY_DBAPI),
    ],
)
def test_custom_dbapi_loaders_are_stable_across_inheritance(dialect_cls, expected):
    assert "import_dbapi" in dialect_cls.__dict__
    assert "dbapi" in dialect_cls.__dict__
    if expected is LEGACY_DBAPI:
        assert dialect_cls.import_dbapi(legacy_token="accepted") is expected
        assert dialect_cls.dbapi(legacy_token="accepted") is expected
    else:
        assert dialect_cls.import_dbapi(loader_token="accepted") is expected
        assert dialect_cls.dbapi(loader_token="accepted") is expected


@pytest.mark.parametrize(
    ("registry_name", "drivername", "dialect_cls", "expected", "keyword"),
    [
        (
            "customloaderchild.flightsql",
            "customloaderchild+flightsql",
            CustomLoaderChildDialect,
            CUSTOM_DBAPI,
            {"loader_token": "accepted"},
        ),
        (
            "legacyloaderchild.flightsql",
            "legacyloaderchild+flightsql",
            LegacyLoaderChildDialect,
            LEGACY_DBAPI,
            {"legacy_token": "accepted"},
        ),
    ],
)
def test_create_engine_uses_one_custom_loader_on_both_sqlalchemy_generations(
    registry_name, drivername, dialect_cls, expected, keyword
):
    registry.register(registry_name, __name__, dialect_cls.__name__)

    with warnings.catch_warnings():
        warnings.simplefilter("error", SADeprecationWarning)
        engine = create_engine(URL.create(drivername=drivername, host="localhost", port=443), **keyword)

    assert engine.dialect.dbapi is expected
    engine.dispose()


def test_conflicting_modern_and_legacy_dbapi_loaders_are_rejected():
    with pytest.raises(TypeError, match="define only import_dbapi or dbapi"):

        class ConflictingLoaderDialect(FlightSQLDialect):
            @classmethod
            def import_dbapi(cls):
                return CUSTOM_DBAPI

            @classmethod
            def dbapi(cls):
                return LEGACY_DBAPI


def test_create_connect_args_follows_dialect_contract(monkeypatch):
    client = object()
    monkeypatch.setattr("flightsql.sqlalchemy.client_from_url", lambda url: client)

    args, kwargs = FlightSQLDialect().create_connect_args(URL.create("datafusion+flightsql"))

    assert args == [client]
    assert kwargs == {}


def test_client_from_url_normalizes_repeated_query_values(monkeypatch):
    captured = {}
    monkeypatch.setattr("flightsql.sqlalchemy.FlightSQLClient", lambda **kwargs: captured.update(kwargs))
    url = URL.create(
        drivername="datafusion+flightsql",
        host="localhost",
        port=443,
        query={"header": ("first", "last")},
    )

    client_from_url(url)

    assert captured["metadata"] == {"header": "last"}


@pytest.mark.parametrize("parameter", ["insecure", "disable_server_verification"])
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("true", True),
        ("TRUE", True),
        ("1", True),
        ("yes", True),
        ("on", True),
        ("false", False),
        ("FALSE", False),
        ("0", False),
        ("no", False),
        ("off", False),
        ("", False),
    ],
)
def test_client_from_url_parses_security_booleans(monkeypatch, parameter, value, expected):
    captured = {}
    monkeypatch.setattr("flightsql.sqlalchemy.FlightSQLClient", lambda **kwargs: captured.update(kwargs))
    url = URL.create(
        drivername="datafusion+flightsql",
        host="localhost",
        port=443,
        query={parameter: value},
    )

    client_from_url(url)

    assert captured[parameter] is expected


@pytest.mark.parametrize("parameter", ["insecure", "disable_server_verification"])
def test_client_from_url_rejects_ambiguous_security_booleans(monkeypatch, parameter):
    monkeypatch.setattr("flightsql.sqlalchemy.FlightSQLClient", lambda **kwargs: None)
    url = URL.create(
        drivername="datafusion+flightsql",
        host="localhost",
        port=443,
        query={parameter: "definitely"},
    )

    with pytest.raises(ArgumentError, match=f"invalid boolean value for {parameter!r}") as error:
        client_from_url(url)

    assert "<empty>, 0, 1, false, no, off, on, true, yes" in str(error.value)


def test_client_from_url_rejects_conflicting_tls_modes(monkeypatch):
    monkeypatch.setattr("flightsql.sqlalchemy.FlightSQLClient", lambda **kwargs: None)
    url = URL.create(
        drivername="datafusion+flightsql",
        host="localhost",
        port=443,
        query={"insecure": "true", "disable_server_verification": "true"},
    )

    with pytest.raises(ArgumentError, match="cannot both be true"):
        client_from_url(url)


def test_client_from_url_extracts_basic_auth(monkeypatch):
    captured = {}
    monkeypatch.setattr("flightsql.sqlalchemy.FlightSQLClient", lambda **kwargs: captured.update(kwargs))
    url = URL.create(
        drivername="datafusion+flightsql",
        username="user@example.com",
        password="password with spaces",
        host="localhost",
        port=443,
    )

    client_from_url(url)

    assert captured["user"] == "user@example.com"
    assert captured["password"] == "password with spaces"
    assert captured["token"] is None


def test_client_from_url_extracts_token_features_and_metadata(monkeypatch):
    captured = {}
    monkeypatch.setattr("flightsql.sqlalchemy.FlightSQLClient", lambda **kwargs: captured.update(kwargs))
    url = URL.create(
        drivername="datafusion+flightsql",
        host="localhost",
        port=443,
        query={
            "token": "secret-token",
            "feature-sqlalchemy-prepared-statements": "on",
            "bucket-name": "analytics",
        },
    )

    client_from_url(url)

    assert captured["token"] == "secret-token"
    assert captured["features"] == {"sqlalchemy-prepared-statements": "on"}
    assert captured["metadata"] == {"bucket-name": "analytics"}
    assert captured["insecure"] is False
    assert captured["disable_server_verification"] is False


def test_reflection_methods_accept_sqlalchemy_keyword_contracts():
    dialect = FlightSQLDialect()
    connection = StubConnection()

    assert dialect.get_columns(connection, table_name="records", schema="public", info_cache={}) == [
        {"name": "records", "schema": "public"}
    ]
    assert dialect.get_table_names(connection, schema="public", info_cache={}) == ["records"]
    assert dialect.has_table(connection, table_name="records", schema="public", info_cache={})
    assert dialect.get_indexes(connection, table_name="records") == []
    assert dialect.get_pk_constraint(connection, table_name="records", schema="public") == {
        "constrained_columns": [],
        "name": None,
    }

    connection.connection.features[FEATURE_PRIMARY_KEYS] = "on"
    assert dialect.get_pk_constraint(connection, table_name="records", schema="public") == {
        "constrained_columns": ["id"],
        "name": "records_pk",
    }


def test_reflection_uses_bulk_included_schemas_and_shared_info_cache_without_n_plus_one_calls():
    class BulkDBAPIConnection:
        features = {}

        def __init__(self):
            self.calls = []

        def flightsql_get_table_metadata(self, schema):
            self.calls.append(("metadata", schema))
            return {
                "alpha": [{"name": "id", "table": "alpha"}],
                "beta": [{"name": "id", "table": "beta"}],
            }

        def flightsql_get_table_names(self, schema):
            raise AssertionError("the bulk metadata response already contains names")

        def flightsql_get_columns(self, table_name, schema):
            raise AssertionError("the bulk metadata response already contains columns")

    connection = SimpleNamespace(connection=BulkDBAPIConnection())
    dialect = FlightSQLDialect()
    info_cache = {}

    assert dialect.get_table_names(connection, schema="public", info_cache=info_cache) == ["alpha", "beta"]
    assert dialect.get_columns(connection, "alpha", schema="public", info_cache=info_cache) == [
        {"name": "id", "table": "alpha"}
    ]
    assert dialect.get_columns(connection, "beta", schema="public", info_cache=info_cache) == [
        {"name": "id", "table": "beta"}
    ]
    assert dialect.has_table(connection, "alpha", schema="public", info_cache=info_cache)
    assert connection.connection.calls == [("metadata", "public")]


def test_reflection_compatibility_fallback_filters_stale_tables_and_caches_each_columns_call():
    class FallbackDBAPIConnection:
        features = {}

        def __init__(self):
            self.calls = []

        def flightsql_get_table_metadata(self, schema):
            self.calls.append(("metadata", schema))
            return None

        def flightsql_get_table_names(self, schema):
            self.calls.append(("names", schema))
            return ["alpha", "stale"]

        def flightsql_get_columns(self, table_name, schema):
            self.calls.append(("columns", table_name, schema))
            if table_name == "stale":
                return []
            return [{"name": "id", "table": table_name}]

    connection = SimpleNamespace(connection=FallbackDBAPIConnection())
    dialect = FlightSQLDialect()
    info_cache = {}

    assert dialect.get_table_names(connection, schema="public", info_cache=info_cache) == ["alpha"]
    assert dialect.get_columns(connection, "alpha", schema="public", info_cache=info_cache) == [
        {"name": "id", "table": "alpha"}
    ]
    assert connection.connection.calls == [
        ("metadata", "public"),
        ("names", "public"),
        ("columns", "alpha", "public"),
        ("columns", "stale", "public"),
    ]


def test_reflection_does_not_mask_bulk_metadata_transport_errors():
    class FailingDBAPIConnection:
        features = {}

        def flightsql_get_table_metadata(self, schema):
            raise RuntimeError("transport unavailable")

    connection = SimpleNamespace(connection=FailingDBAPIConnection())

    with pytest.raises(RuntimeError, match="transport unavailable"):
        FlightSQLDialect().get_table_names(connection, schema="public", info_cache={})


def _literal_dialect():
    dialect = DataFusionDialect()
    dialect.statement_compiler = LiteralBindCompiler
    return dialect


def test_literal_binds_compilation_stays_fully_literal():
    records = table("records", column("id", Integer), column("label", String))
    repeated = bindparam("candidate", type_=Integer, literal_execute=True)
    statement = select(records.c.id).where((records.c.id == repeated) | (records.c.id == repeated))

    compiled = statement.params(candidate=7).compile(dialect=_literal_dialect(), compile_kwargs={"literal_binds": True})

    assert "POSTCOMPILE" not in str(compiled)
    assert str(compiled).count("records.id = 7") == 2
    assert compiled.params == {}


def test_normal_compilation_uses_postcompile_tokens_and_stable_cache_keys():
    records = table("records", column("id", Integer))
    first = select(records.c.id).where(records.c.id == 7)
    second = select(records.c.id).where(records.c.id == 9)

    first_compiled = first.compile(dialect=_literal_dialect())
    second_compiled = second.compile(dialect=_literal_dialect())

    assert "POSTCOMPILE" in str(first_compiled)
    assert "7" not in str(first_compiled)
    assert "9" not in str(second_compiled)
    assert first._generate_cache_key()[0] == second._generate_cache_key()[0]
    assert DataFusionDialect.supports_statement_cache is True
