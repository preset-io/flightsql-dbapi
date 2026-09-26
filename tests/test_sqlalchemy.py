import warnings
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import (
    Integer,
    String,
    bindparam,
    column,
    create_engine,
    select,
    table,
    tuple_,
)
from sqlalchemy.dialects import registry
from sqlalchemy.engine import URL
from sqlalchemy.exc import (
    ArgumentError,
    NoSuchTableError,
    SADeprecationWarning,
    SAWarning,
    UnreflectableTableError,
)
from sqlalchemy.sql.sqltypes import NullType
from sqlalchemy.types import TypeDecorator

import flightsql
from flightsql.dbapi import TableMetadataResult
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
        return TableMetadataResult(
            ["records"],
            {"records": [{"name": "records", "schema": schema}]},
            True,
        )

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
            columns = {
                "alpha": [{"name": "id", "table": "alpha"}],
                "beta": [{"name": "id", "table": "beta"}],
            }
            return TableMetadataResult(["alpha", "beta"], columns, True)

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


def test_partial_reflection_live_probes_only_corrupt_cache_miss_and_recovers_columns():
    class PartialDBAPIConnection:
        features = {}

        def __init__(self):
            self.calls = []

        def flightsql_get_table_metadata(self, schema):
            self.calls.append(("metadata", schema))
            return TableMetadataResult(
                ["good", "corrupt"],
                {"good": [{"name": "bulk_id"}]},
                False,
            )

        def flightsql_get_table_metadata_for_table(self, table_name, schema):
            self.calls.append(("probe", table_name, schema))
            assert table_name == "corrupt"
            return TableMetadataResult(
                ["corrupt"],
                {"corrupt": [{"name": "live_id"}]},
                True,
            )

    connection = SimpleNamespace(connection=PartialDBAPIConnection())
    dialect = FlightSQLDialect()
    info_cache = {}

    with pytest.warns(SAWarning, match=r"issued 1 filtered probe\(s\): 1 recovered") as captured:
        assert dialect.get_table_names(connection, schema="public", info_cache=info_cache) == ["good", "corrupt"]
    assert Path(captured[0].filename) == Path(__file__)
    assert dialect.get_columns(connection, "good", schema="public", info_cache=info_cache) == [{"name": "bulk_id"}]
    assert dialect.get_columns(connection, "corrupt", schema="public", info_cache=info_cache) == [{"name": "live_id"}]
    assert dialect.has_table(connection, "corrupt", schema="public", info_cache=info_cache)
    assert connection.connection.calls == [
        ("metadata", "public"),
        ("probe", "corrupt", "public"),
    ]


def test_permission_scoped_reflection_preserves_name_but_fails_columns_explicitly():
    class PermissionScopedDBAPIConnection:
        features = {}

        def __init__(self):
            self.calls = []

        def flightsql_get_table_metadata(self, schema):
            self.calls.append(("metadata", schema))
            return TableMetadataResult(["visible_without_columns"], None, False)

        def flightsql_get_table_metadata_for_table(self, table_name, schema):
            self.calls.append(("probe", table_name, schema))
            return TableMetadataResult([table_name], None, False)

    connection = SimpleNamespace(connection=PermissionScopedDBAPIConnection())
    dialect = FlightSQLDialect()
    info_cache = {}

    with pytest.warns(SAWarning, match="1 still unreflectable"):
        assert dialect.get_table_names(connection, schema="restricted", info_cache=info_cache) == [
            "visible_without_columns"
        ]
    assert dialect.has_table(
        connection,
        "visible_without_columns",
        schema="restricted",
        info_cache=info_cache,
    )
    with pytest.raises(UnreflectableTableError, match="metadata permissions"):
        dialect.get_columns(
            connection,
            "visible_without_columns",
            schema="restricted",
            info_cache=info_cache,
        )
    assert connection.connection.calls == [
        ("metadata", "restricted"),
        ("probe", "visible_without_columns", "restricted"),
    ]


def test_partial_reflection_excludes_confirmed_stale_row_and_preserves_no_such_table_semantics():
    class StaleDBAPIConnection:
        features = {}

        def __init__(self):
            self.calls = []

        def flightsql_get_table_metadata(self, schema):
            self.calls.append(("metadata", schema))
            return TableMetadataResult(
                ["good", "stale"],
                {"good": [{"name": "id"}]},
                False,
            )

        def flightsql_get_table_metadata_for_table(self, table_name, schema):
            self.calls.append(("probe", table_name, schema))
            return TableMetadataResult([], {}, True)

    connection = SimpleNamespace(connection=StaleDBAPIConnection())
    dialect = FlightSQLDialect()
    info_cache = {}

    with pytest.warns(SAWarning, match=r"1 stale row\(s\) excluded"):
        assert dialect.get_table_names(connection, schema="public", info_cache=info_cache) == ["good"]
    assert not dialect.has_table(connection, "stale", schema="public", info_cache=info_cache)
    with pytest.raises(NoSuchTableError, match=r"public\.stale"):
        dialect.get_columns(connection, "stale", schema="public", info_cache=info_cache)
    assert connection.connection.calls == [
        ("metadata", "public"),
        ("probe", "stale", "public"),
    ]


def test_partial_reflection_probe_transport_failure_propagates_after_partial_success():
    class PartiallyFailingDBAPIConnection:
        features = {}

        def __init__(self):
            self.calls = []

        def flightsql_get_table_metadata(self, schema):
            self.calls.append(("metadata", schema))
            return TableMetadataResult(["first", "second"], None, False)

        def flightsql_get_table_metadata_for_table(self, table_name, schema):
            self.calls.append(("probe", table_name, schema))
            if table_name == "second":
                raise RuntimeError("filtered transport unavailable")
            return TableMetadataResult([table_name], {table_name: [{"name": "id"}]}, True)

    connection = SimpleNamespace(connection=PartiallyFailingDBAPIConnection())

    with pytest.raises(RuntimeError, match="filtered transport unavailable"):
        FlightSQLDialect().get_table_names(connection, schema="public", info_cache={})
    assert connection.connection.calls == [
        ("metadata", "public"),
        ("probe", "first", "public"),
        ("probe", "second", "public"),
    ]


def test_reflection_cache_keys_columns_by_schema_for_same_table_name():
    class MultiSchemaDBAPIConnection:
        features = {}

        def __init__(self):
            self.calls = []

        def flightsql_get_table_metadata(self, schema):
            self.calls.append(("metadata", schema))
            return TableMetadataResult(
                ["records"],
                {"records": [{"name": f"{schema}_id"}]},
                True,
            )

    connection = SimpleNamespace(connection=MultiSchemaDBAPIConnection())
    dialect = FlightSQLDialect()
    info_cache = {}

    assert dialect.get_table_names(connection, schema="public", info_cache=info_cache) == ["records"]
    assert dialect.get_table_names(connection, schema="private", info_cache=info_cache) == ["records"]
    assert dialect.get_columns(connection, "records", schema="public", info_cache=info_cache) == [{"name": "public_id"}]
    assert dialect.get_columns(connection, "records", schema="private", info_cache=info_cache) == [
        {"name": "private_id"}
    ]
    assert connection.connection.calls == [("metadata", "public"), ("metadata", "private")]


def test_degraded_reflection_warning_is_latched_per_schema():
    class StaleDBAPIConnection:
        features = {}

        def flightsql_get_table_metadata(self, schema):
            return TableMetadataResult(["stale"], None, False)

        def flightsql_get_table_metadata_for_table(self, table_name, schema):
            return TableMetadataResult([], {}, True)

    connection = SimpleNamespace(connection=StaleDBAPIConnection())
    dialect = FlightSQLDialect()

    with pytest.warns(SAWarning) as captured:
        dialect.get_table_names(connection, schema="one", info_cache={})
        dialect.get_table_names(connection, schema="one", info_cache={})
        dialect.get_table_names(connection, schema="two", info_cache={})

    assert len(captured) == 2
    assert all(Path(item.filename) == Path(__file__) for item in captured)
    assert "schema 'one'" in str(captured[0].message)
    assert "schema 'two'" in str(captured[1].message)


def test_reflection_empty_catalog_is_not_treated_as_unsupported_schema_metadata():
    class EmptyDBAPIConnection:
        features = {}

        def __init__(self):
            self.calls = []

        def flightsql_get_table_metadata(self, schema):
            self.calls.append(("metadata", schema))
            return TableMetadataResult([], {}, True)

    connection = SimpleNamespace(connection=EmptyDBAPIConnection())
    dialect = FlightSQLDialect()

    with warnings.catch_warnings():
        warnings.simplefilter("error", SAWarning)
        assert dialect.get_table_names(connection, schema="public", info_cache={}) == []
        assert not dialect.has_table(connection, "missing", schema="public", info_cache={})

    assert connection.connection.calls == [("metadata", "public"), ("metadata", "public")]


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


def test_empty_scalar_and_tuple_expanding_binds_render_at_1_4_6_floor_and_reuse_compiled_state():
    records = table("records", column("id", Integer), column("label", String))
    ids = bindparam("ids", expanding=True)
    pairs = bindparam("pairs", expanding=True)
    scalar_statement = select(records.c.id).where(records.c.id.in_(ids))
    tuple_statement = select(records.c.id).where(tuple_(records.c.id, records.c.label).in_(pairs))

    scalar_compiled = scalar_statement.compile(dialect=_literal_dialect())
    tuple_compiled = tuple_statement.compile(dialect=_literal_dialect())

    scalar_empty = scalar_compiled._process_parameters_for_postcompile({"ids": []}).statement
    tuple_empty_list = tuple_compiled._process_parameters_for_postcompile({"pairs": []}).statement
    tuple_populated = tuple_compiled._process_parameters_for_postcompile({"pairs": [(7, "seven")]}).statement
    tuple_empty_tuple = tuple_compiled._process_parameters_for_postcompile({"pairs": ()}).statement

    assert "1 != 1" in scalar_empty
    assert "IN (SELECT 1, 1 WHERE 1 != 1)" in tuple_empty_list
    assert "IN ((7, 'seven'))" in tuple_populated
    assert "IN (SELECT 1, 1 WHERE 1 != 1)" in tuple_empty_tuple
    assert "POSTCOMPILE" in str(tuple_compiled)
    assert tuple_statement._generate_cache_key() is not None


def test_empty_tuple_not_in_preserves_empty_set_truth_semantics():
    records = table("records", column("id", Integer), column("label", String))
    pairs = bindparam("pairs", expanding=True)
    statement = select(records.c.id).where(tuple_(records.c.id, records.c.label).not_in(pairs))
    compiled = statement.compile(dialect=_literal_dialect())

    expanded = compiled._process_parameters_for_postcompile({"pairs": []}).statement

    assert "NOT IN (SELECT 1, 1 WHERE 1 != 1)" in expanded


@pytest.mark.parametrize("bind_type", [String(), Integer(), NullType()], ids=["string", "integer", "nulltype"])
@pytest.mark.parametrize("operator", ["equality", "is-not-distinct", "is-distinct"])
def test_none_postcompile_and_literal_binds_render_sql_null_for_all_declared_sqlalchemy_floors(bind_type, operator):
    value_column = column("value", bind_type)
    candidate = bindparam("candidate", type_=bind_type)
    if operator == "equality":
        predicate = value_column == candidate
    elif operator == "is-not-distinct":
        predicate = value_column.isnot_distinct_from(candidate)
    else:
        predicate = value_column.is_distinct_from(candidate)
    statement = select(value_column).where(predicate)

    compiled = statement.compile(dialect=_literal_dialect())
    expanded = compiled._process_parameters_for_postcompile({"candidate": None})
    literal = statement.params(candidate=None).compile(
        dialect=_literal_dialect(),
        compile_kwargs={"literal_binds": True},
    )

    assert "POSTCOMPILE" in str(compiled)
    assert "NULL" in expanded.statement
    assert "'NULL'" not in expanded.statement
    assert "POSTCOMPILE" not in str(literal)
    assert "NULL" in str(literal)
    assert "'NULL'" not in str(literal)


def test_none_is_not_confused_with_the_string_null_and_unmappable_untyped_values_fail_closed():
    value_column = column("value", String())
    string_statement = select(value_column).where(value_column == bindparam("candidate", type_=String()))
    compiled = string_statement.compile(dialect=_literal_dialect())

    none_sql = compiled._process_parameters_for_postcompile({"candidate": None}).statement
    string_sql = compiled._process_parameters_for_postcompile({"candidate": "NULL"}).statement

    assert "value = NULL" in none_sql
    assert "value = 'NULL'" in string_sql

    # Untyped binds (text(":v")) are typed from the Python value with
    # SQLAlchemy's own resolver; values it cannot map still fail closed.
    untyped_statement = select(bindparam("candidate", type_=NullType()))
    untyped = untyped_statement.compile(dialect=_literal_dialect())
    rendered = untyped._process_parameters_for_postcompile({"candidate": "it's typed"}).statement
    assert "'it''s typed'" in rendered
    with pytest.raises(Exception, match=r"literal|render|quote"):
        untyped._process_parameters_for_postcompile({"candidate": object()})


def test_literal_none_preserves_type_should_evaluate_none_opt_in():
    class EvaluateNoneLiteral(TypeDecorator):
        impl = String
        cache_ok = True

        def process_literal_param(self, value, dialect):
            return "TYPE_HANDLED_NONE" if value is None else value

    value_type = EvaluateNoneLiteral().evaluates_none()
    statement = select(bindparam("candidate", type_=value_type))
    compiled = statement.compile(dialect=_literal_dialect())

    expanded = compiled._process_parameters_for_postcompile({"candidate": None}).statement

    assert "'TYPE_HANDLED_NONE'" in expanded
    assert "SELECT NULL" not in expanded
