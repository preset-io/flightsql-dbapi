"""Regressions for Flight SQL servers built on the DataFusion Flight SQL service.

Each test pins behavior observed live against datafusion-flight-sql-server
0.4.19 (DataFusion 55.1): GetSqlInfo is unimplemented, metadata RPCs only
answer for an explicitly named catalog, prepared statements return a new handle
from DoPut and a typed parameter schema, and string columns arrive as Utf8View.
"""

import datetime
from decimal import Decimal
from types import SimpleNamespace

import pyarrow as pa
import pytest
import sqlalchemy
from pyarrow import flight
from sqlalchemy import bindparam, select
from sqlalchemy.engine import URL, make_url
from sqlalchemy.sql import sqltypes

import flightsql
from flightsql import client as flight_client
from flightsql.dbapi import (
    Connection,
    Cursor,
    TableMetadataResult,
    build_parameter_record,
)
from flightsql.sqlalchemy import (
    DataFusionDialect,
    LiteralBindCompiler,
    PreparedStatementCompiler,
)

SQLALCHEMY_2 = int(sqlalchemy.__version__.split(".")[0]) >= 2


def _endpoint_info(ticket):
    return SimpleNamespace(endpoints=[SimpleNamespace(ticket=ticket)])


class CatalogScopedClient:
    """Answers metadata only for the catalog named in the request."""

    features = {}

    def __init__(self, catalogs=("datafusion",), get_catalogs_implemented=True, default_catalog="datafusion"):
        self.catalogs = list(catalogs)
        self.get_catalogs_implemented = get_catalogs_implemented
        self.calls = []
        self.default_catalog = default_catalog

    def execute(self, query):
        self.calls.append(("execute", {"query": query}))
        return _endpoint_info(("settings", None))

    def get_catalogs(self):
        self.calls.append(("get_catalogs", {}))
        if not self.get_catalogs_implemented:
            raise pa.ArrowNotImplementedError("GetCatalogs unimplemented")
        return _endpoint_info(("catalogs", None))

    def get_db_schemas(self, **kwargs):
        self.calls.append(("get_db_schemas", kwargs))
        return _endpoint_info(("schemas", kwargs.get("catalog")))

    def get_tables(self, **kwargs):
        self.calls.append(("get_tables", kwargs))
        return _endpoint_info(("tables", kwargs.get("catalog")))

    def do_get(self, ticket):
        kind, catalog = ticket
        if kind == "settings":
            table = pa.table({"value": pa.array([self.default_catalog], pa.string())})
        elif kind == "catalogs":
            table = pa.table({"catalog_name": pa.array(self.catalogs, pa.string())})
        elif catalog != "datafusion":
            table = pa.table({"db_schema_name": pa.array([], pa.string())})
            if kind == "tables":
                table = pa.table(
                    {
                        "db_schema_name": pa.array([], pa.string()),
                        "table_name": pa.array([], pa.string()),
                        "table_type": pa.array([], pa.string()),
                    }
                )
        elif kind == "schemas":
            table = pa.table({"db_schema_name": ["information_schema", "public"]})
        else:
            table = pa.table(
                {
                    "db_schema_name": ["public", "public"],
                    "table_name": ["orders", "east_orders"],
                    "table_type": ["BASE TABLE", "VIEW"],
                }
            )
        return SimpleNamespace(read_all=lambda: table)


def test_unscoped_empty_metadata_resolves_the_datafusion_catalog_once():
    client = CatalogScopedClient(catalogs=["other", "datafusion"])
    connection = Connection(client)

    assert connection.flightsql_get_schema_names() == ["information_schema", "public"]
    assert connection.flightsql_get_table_names("public") == ["orders", "east_orders"]
    assert [call for call in client.calls if call[0] == "get_catalogs"] == [("get_catalogs", {})]
    assert client.calls[-1] == ("get_tables", {"db_schema_filter_pattern": "public", "catalog": "datafusion"})


def test_single_catalog_is_used_and_ambiguous_catalogs_stay_unscoped():
    assert Connection(CatalogScopedClient(catalogs=["datafusion"])).flightsql_get_schema_names()
    ambiguous = CatalogScopedClient(catalogs=["a", "b"])
    assert Connection(ambiguous).flightsql_get_schema_names() == []
    assert all("catalog" not in kwargs for name, kwargs in ambiguous.calls if name not in ("get_catalogs", "execute"))


def test_server_without_get_catalogs_keeps_the_unscoped_answer():
    client = CatalogScopedClient(get_catalogs_implemented=False)
    assert Connection(client).flightsql_get_schema_names() == []


def test_explicit_catalog_scopes_every_rpc_without_discovery():
    client = CatalogScopedClient()
    connection = Connection(client, catalog="datafusion")
    assert connection.flightsql_get_schema_names() == ["information_schema", "public"]
    assert not any(name == "get_catalogs" for name, _ in client.calls)
    assert all(kwargs.get("catalog") == "datafusion" for _, kwargs in client.calls)


def test_url_database_names_the_catalog():
    dialect = DataFusionDialect()
    _, kwargs = dialect.create_connect_args(make_url("datafusion://localhost:50051/analytics?insecure=true"))
    assert kwargs == {"catalog": "analytics"}
    # "/?" rather than "?": SQLAlchemy 1.4.6 cannot parse a query string with no path.
    _, kwargs = dialect.create_connect_args(make_url("datafusion://localhost:50051/?insecure=true"))
    assert kwargs == {}


def test_table_metadata_reports_table_types():
    result = Connection(CatalogScopedClient()).flightsql_get_table_metadata("public")
    assert isinstance(result, TableMetadataResult)
    assert result.table_types == {"orders": "BASE TABLE", "east_orders": "VIEW"}


class _SqlInfoUnimplemented:
    features = {}

    def flightsql_get_sql_info(self, _info):
        raise flightsql.NotSupportedError("Flight returned unimplemented error: Implement CommandGetSqlInfo")


def test_initialize_falls_back_when_get_sql_info_is_unimplemented(monkeypatch):
    dialect = DataFusionDialect()
    monkeypatch.setattr(sqlalchemy.engine.default.DefaultDialect, "initialize", lambda self, connection: None)
    dialect.initialize(SimpleNamespace(connection=_SqlInfoUnimplemented()))
    assert dialect.sql_info == {}
    assert dialect.identifier_preparer.initial_quote == '"'
    assert dialect.identifier_preparer.final_quote == '"'
    assert dialect.statement_compiler is LiteralBindCompiler


def test_views_are_listed_separately_and_has_table_includes_them():
    dialect = DataFusionDialect()
    connection = SimpleNamespace(connection=Connection(CatalogScopedClient()))
    assert dialect.get_table_names(connection, schema="public") == ["orders"]
    assert dialect.get_view_names(connection, schema="public") == ["east_orders"]
    assert dialect.has_table(connection, "east_orders", schema="public") is True
    assert dialect.has_table(connection, "missing", schema="public") is False


def test_no_authorization_header_without_credentials():
    _, headers = flight_client.create_flight_client(host="localhost", port=1, insecure=True)
    assert all(name != b"authorization" for name, _ in headers)
    _, headers = flight_client.create_flight_client(host="localhost", port=1, insecure=True, token="t")
    assert (b"authorization", b"Bearer t") in headers


class _FailingClient:
    def __init__(self, error):
        self.error = error

    def execute(self, _query):
        raise self.error


@pytest.mark.parametrize(
    "error, expected",
    [
        (flight.FlightUnavailableError("Flight returned unavailable error"), flightsql.OperationalError),
        (flight.FlightUnauthenticatedError("Flight returned unauthenticated error"), flightsql.OperationalError),
        (pa.ArrowNotImplementedError("unimplemented"), flightsql.NotSupportedError),
        (flight.FlightInternalError("Plan error"), flightsql.InternalError),
        (pa.ArrowInvalid("bad SQL"), flightsql.ProgrammingError),
    ],
)
def test_transport_errors_are_dbapi_errors(error, expected):
    with pytest.raises(expected) as raised:
        Cursor(_FailingClient(error)).execute("SELECT 1")
    assert raised.value.__cause__ is error
    assert str(error) in str(raised.value)


def test_unavailable_is_a_disconnect():
    dialect = DataFusionDialect()
    with pytest.raises(flightsql.OperationalError) as raised:
        Cursor(_FailingClient(flight.FlightUnavailableError("Flight returned unavailable error"))).execute("SELECT 1")
    assert dialect.is_disconnect(raised.value, None, None)
    assert not dialect.is_disconnect(flightsql.ProgrammingError("bad SQL"), None, None)


def test_parameter_record_uses_server_schema_and_allows_null():
    schema = pa.schema([pa.field("$1", pa.string(), nullable=False), pa.field("$2", pa.int64(), nullable=False)])
    record = build_parameter_record(("east", None), schema)
    assert record.schema.names == ["$1", "$2"]
    assert record.schema.types == [pa.string(), pa.int64()]
    assert all(field.nullable for field in record.schema)
    assert record.to_pylist() == [{"$1": "east", "$2": None}]
    with pytest.raises(flightsql.DataError, match="cannot bind"):
        build_parameter_record(("east", "not a number"), schema)


def test_parameter_record_without_server_schema_keeps_union_binding():
    record = build_parameter_record(("east", 1), None)
    assert record.schema.names == ["param_0", "param_1"]
    assert all(pa.types.is_union(t) for t in record.schema.types)


def test_do_put_prepared_result_handle_is_decoded_raw_or_any_wrapped():
    handle = b"\x0a\x03abc"
    raw = b"\x0a" + bytes([len(handle)]) + handle
    url = flight_client._DO_PUT_PREPARED_RESULT_URL.encode()
    wrapped = b"\x0a" + bytes([len(url)]) + url + b"\x12" + bytes([len(raw)]) + raw
    assert flight_client._updated_prepared_handle(raw) == handle
    assert flight_client._updated_prepared_handle(wrapped) == handle
    assert flight_client._updated_prepared_handle(b"") is None
    assert flight_client._updated_prepared_handle(b"\xff") is None


def _literal(value, type_):
    dialect = DataFusionDialect()
    dialect.statement_compiler = LiteralBindCompiler
    compiled = select(bindparam("v", type_=type_)).compile(dialect=dialect)
    return compiled._process_parameters_for_postcompile({"v": value}).statement


@pytest.mark.parametrize(
    "value, type_, expected",
    [
        (
            datetime.datetime(2024, 2, 29, 12, 34, 56, 123456),
            sqltypes.DateTime(),
            "TIMESTAMP '2024-02-29 12:34:56.123456'",
        ),
        (datetime.date(2024, 2, 29), sqltypes.Date(), "DATE '2024-02-29'"),
        (datetime.time(23, 59, 59, 5), sqltypes.Time(), "TIME '23:59:59.000005'"),
        (b"\x00\xff", sqltypes.LargeBinary(), "X'00ff'"),
        (datetime.date(2024, 2, 29), sqltypes.NullType(), "DATE '2024-02-29'"),
        (Decimal("12345678901234567890.0123456789"), sqltypes.NullType(), "12345678901234567890.0123456789"),
        ("O'Brien %(k)s", sqltypes.NullType(), "'O''Brien %(k)s'"),
    ],
)
def test_typed_literals(value, type_, expected):
    assert _literal(value, type_) == f"SELECT {expected} AS anon_1"


@pytest.mark.skipif(not SQLALCHEMY_2, reason="numeric_dollar requires SQLAlchemy 2")
def test_only_the_prepared_statement_feature_switches_to_numbered_placeholders():
    # create_engine builds connect args eagerly; no connection is opened.
    plain = sqlalchemy.create_engine("datafusion://localhost:1?insecure=true").dialect
    assert plain.paramstyle == "qmark"
    prepared = sqlalchemy.create_engine(
        "datafusion://localhost:1?insecure=true&feature-sqlalchemy-prepared-statements=on"
    ).dialect
    assert prepared.paramstyle == "numeric_dollar"
    compiled = select(bindparam("a", type_=sqltypes.Integer()), bindparam("b", type_=sqltypes.String())).compile(
        dialect=prepared
    )
    assert "$1" in str(compiled) and "$2" in str(compiled)


class CompliantCatalogClient(CatalogScopedClient):
    """Spec-compliant: an unset catalog answers across every catalog."""

    TABLES = {"datafusion": ("public", "t1"), "sales": ("public", "orders")}

    def do_get(self, ticket):
        kind, catalog = ticket
        rows = [value for name, value in self.TABLES.items() if catalog in (None, name)]
        if kind == "settings":
            table = pa.table({"value": pa.array([self.default_catalog], pa.string())})
        elif kind == "catalogs":
            return SimpleNamespace(read_all=lambda: pa.table({"catalog_name": list(self.TABLES)}))
        if kind == "schemas":
            return SimpleNamespace(
                read_all=lambda: pa.table({"db_schema_name": pa.array([s for s, _ in rows], pa.string())})
            )
        schema = self.calls[-1][1].get("db_schema_filter_pattern")
        rows = [(s, t) for s, t in rows if schema in (None, s)]
        table = pa.table(
            {
                "db_schema_name": pa.array([s for s, _ in rows], pa.string()),
                "table_name": pa.array([t for _, t in rows], pa.string()),
                "table_type": pa.array(["BASE TABLE"] * len(rows), pa.string()),
            }
        )
        return SimpleNamespace(read_all=lambda: table)


def test_legitimately_empty_answers_do_not_scope_a_compliant_server():
    client = CompliantCatalogClient()
    connection = Connection(client)
    assert connection.flightsql_get_table_names("staging") == []
    assert connection.flightsql_get_table_names(None) == ["t1", "orders"]
    assert not any(name == "get_catalogs" for name, _ in client.calls)
    assert all("catalog" not in kwargs for _, kwargs in client.calls)


def test_parameter_record_with_union_parameter_schema_uses_union_binding():
    # The Arrow C++ SQLite example server declares every parameter this way.
    du = pa.dense_union(
        [
            pa.field("string_value", pa.string()),
            pa.field("bigint_value", pa.int64()),
            pa.field("double_value", pa.float64()),
            pa.field("bytes_value", pa.binary()),
        ]
    )
    record = build_parameter_record((1, "one"), pa.schema([("parameter_1", du), ("parameter_2", du)]))
    assert record.schema.names == ["param_0", "param_1"]
    assert all(pa.types.is_union(t) for t in record.schema.types)
    assert record.to_pylist() == [{"param_0": 1, "param_1": "one"}]


def test_uint64_and_decimal_results_are_not_rounded_through_float():
    dialect = DataFusionDialect()
    uint64 = flightsql.dbapi.resolve_sql_type(pa.uint64()).dialect_impl(dialect)
    processor = uint64.result_processor(dialect, None)
    value = 18446744073709551615
    assert (processor(value) if processor else value) == value
    decimal = flightsql.dbapi.resolve_sql_type(pa.decimal128(38, 10)).dialect_impl(dialect)
    processor = decimal.result_processor(dialect, None)
    exact = Decimal("1234567890123456789012345678.0123456789")
    assert (processor(exact) if processor else exact) == exact


def test_nested_values_pass_through_the_json_type():
    dialect = DataFusionDialect()
    nested = flightsql.dbapi.resolve_sql_type(pa.struct([("a", pa.int64())]))
    assert isinstance(nested, sqltypes.JSON)
    # No result processor: Arrow's dicts and lists are returned unchanged.
    assert nested.result_processor(dialect, None) is None


def test_any_wrapping_another_message_is_not_a_prepared_handle():
    url = b"type.googleapis.com/arrow.flight.protocol.sql.DoPutUpdateResult"
    wrapped = b"\x0a" + bytes([len(url)]) + url + b"\x12\x02\x08\x01"
    assert flight_client._updated_prepared_handle(wrapped) is None


@pytest.mark.parametrize(
    "cause, disconnect",
    [
        (flight.FlightUnavailableError("failed to connect to all addresses"), True),
        (flight.FlightUnauthorizedError("user may not open a connection"), False),
        (flight.FlightCancelledError("connection pool exhausted"), False),
        (flight.FlightTimedOutError("deadline exceeded"), False),
    ],
)
def test_only_an_unavailable_server_is_a_disconnect(cause, disconnect):
    error = flightsql.OperationalError(str(cause))
    error.__cause__ = cause
    assert DataFusionDialect().is_disconnect(error, None, None) is disconnect


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_float_literals_fail_to_compile(value):
    with pytest.raises(sqlalchemy.exc.CompileError, match="non-finite"):
        _literal(value, sqltypes.NullType())


@pytest.mark.skipif(not SQLALCHEMY_2, reason="numeric_dollar requires SQLAlchemy 2")
def test_explicit_paramstyle_is_honored_with_prepared_statements():
    dialect = sqlalchemy.create_engine(
        "datafusion://localhost:1?insecure=true&feature-sqlalchemy-prepared-statements=on", paramstyle="qmark"
    ).dialect
    assert dialect.paramstyle == "qmark"


def _result(type_, value):
    dialect = DataFusionDialect()
    processor = type_.dialect_impl(dialect).result_processor(dialect, None)
    return processor(value) if processor else value


def test_numeric_over_a_double_column_still_returns_decimal():
    assert _result(sqltypes.Numeric(10, 2), 1.1) == Decimal("1.10")
    assert isinstance(_result(sqltypes.Numeric(10, 2), 1.1), Decimal)
    exact = Decimal("12345678901234567890.0123456789")
    assert _result(sqltypes.Numeric(38, 10), exact) is exact
    assert _result(sqltypes.Numeric(20, 0), 18446744073709551615) == Decimal(18446744073709551615)
    assert _result(sqltypes.Numeric(asdecimal=False), Decimal("1.5")) == 1.5
    assert isinstance(_result(sqltypes.Float(), 1.5), float)
    assert _result(sqltypes.Numeric(10, 2), None) is None


@pytest.mark.parametrize("value", [1.5, Decimal("1.9")])
def test_typed_integer_parameter_rejects_fractional_values(value):
    with pytest.raises(flightsql.DataError, match="cannot bind.*int64") as raised:
        build_parameter_record((value,), pa.schema([("$1", pa.int64())]))
    assert isinstance(raised.value.__cause__, pa.ArrowInvalid)


@pytest.mark.parametrize("value", [1, 1.0, Decimal("1.0"), None])
def test_typed_integer_parameter_accepts_lossless_values(value):
    record = build_parameter_record((value,), pa.schema([("$1", pa.int64())]))
    assert record.to_pylist() == [{"$1": None if value is None else 1}]


@pytest.mark.parametrize("type_", [sqltypes.Numeric(10, 2), sqltypes.Float()])
@pytest.mark.parametrize("schema_kind", ["absent", "union", "double"])
def test_decimal_sqlalchemy_bind_reaches_prepared_parameter_record(type_, schema_kind):
    engine = sqlalchemy.create_engine(
        URL.create(
            "datafusion",
            host="localhost",
            port=1,
            query={"insecure": "true", "feature-sqlalchemy-prepared-statements": "on"},
        ),
        paramstyle="qmark",
    )
    compiled = select(bindparam("value", type_=type_)).compile(dialect=engine.dialect)
    value = Decimal("2.0")
    processor = compiled._bind_processors.get("value")
    bound = processor(value) if processor else value
    if schema_kind == "absent":
        schema = None
    elif schema_kind == "union":
        schema = pa.schema([("$1", pa.dense_union([pa.field("double", pa.float64())]))])
    else:
        schema = pa.schema([("$1", pa.float64())])
    record = build_parameter_record((bound,), schema)
    assert list(record.to_pylist()[0].values()) == [2.0]
    assert isinstance(bound, float)
    engine.dispose()


@pytest.mark.parametrize(
    "value, target",
    [(18446744073709551615, pa.uint64()), (Decimal("12345678901234567890.0123456789"), pa.decimal128(38, 10))],
)
def test_typed_parameter_preserves_exact_numeric_values(value, target):
    record = build_parameter_record((value,), pa.schema([("$1", target)]))
    assert record.to_pylist() == [{"$1": value}]


@pytest.mark.parametrize("value", [2**63, -(2**63) - 1])
def test_typed_integer_parameter_rejects_overflow(value):
    with pytest.raises(flightsql.DataError, match="cannot bind.*int64"):
        build_parameter_record((value,), pa.schema([("$1", pa.int64())]))


def test_numbered_parameter_schema_binds_by_index_not_lexical_order():
    values = (20, 2, 3, 4, 5, 6, 7, 8, 9, "ten", Decimal("11.25"))
    fields = [pa.field(f"${i}", pa.int64()) for i in range(1, 10)]
    fields += [pa.field("$10", pa.string()), pa.field("$11", pa.decimal128(10, 2))]
    schema = pa.schema(sorted(fields, key=lambda field: field.name), metadata={b"source": b"server"})
    record = build_parameter_record(values, schema)
    assert record.to_pylist() == [{f"${i}": value for i, value in enumerate(values, 1)}]
    assert record.schema == schema
    assert record.schema.metadata == schema.metadata


@pytest.mark.parametrize("names", [("$2", "$1"), ("b", "a"), ("$2", "$2"), ("$0", "$1"), ("$2", "other")])
def test_only_complete_numbered_schemas_change_positional_binding(names):
    record = build_parameter_record((10, 20), pa.schema([(name, pa.int64()) for name in names]))
    expected = [20, 10] if names == ("$2", "$1") else [10, 20]
    assert [column[0].as_py() for column in record.columns] == expected


@pytest.mark.parametrize("value", [100, -100, 0, 2**64 - 1])
def test_integer_binds_to_decimal_using_value_precision(value):
    target = pa.decimal128(22, 2) if value > 100 else pa.decimal128(10, 2)
    record = build_parameter_record((value,), pa.schema([("$1", target)]))
    assert record.column(0)[0].as_py() == Decimal(value)


@pytest.mark.parametrize("value", [1000, -1000])
def test_integer_decimal_bind_still_rejects_insufficient_precision(value):
    with pytest.raises(flightsql.DataError):
        build_parameter_record((value,), pa.schema([("$1", pa.decimal128(4, 2))]))


@pytest.mark.parametrize("type_,value", [(pa.string_view(), "héllo"), (pa.binary_view(), b"\x00\xff")])
def test_view_parameter_binds_on_arrow_floor(type_, value):
    for item in (value, None):
        record = build_parameter_record((item,), pa.schema([("$1", type_)]))
        assert record.schema.types == [type_]
        assert record.to_pylist() == [{"$1": item}]


@pytest.mark.parametrize("catalog", ["cat2", "datafusion"])
def test_url_catalog_is_checked_against_execution_default(monkeypatch, catalog):
    monkeypatch.setattr(sqlalchemy.engine.default.DefaultDialect, "initialize", lambda self, connection: None)
    dbapi = _SqlInfoUnimplemented()
    dbapi.catalog = catalog
    queries = []

    def execute(query):
        queries.append(query)
        return SimpleNamespace(scalar=lambda: "datafusion")

    connection = SimpleNamespace(connection=dbapi, exec_driver_sql=execute)
    if catalog == "cat2":
        with pytest.raises(sqlalchemy.exc.InvalidRequestError, match="must match.*server's default catalog"):
            DataFusionDialect().initialize(connection)
    else:
        DataFusionDialect().initialize(connection)
    assert queries == [
        "SELECT value FROM information_schema.df_settings WHERE name = 'datafusion.catalog.default_catalog'"
    ]


def test_url_catalog_fails_closed_when_default_cannot_be_verified(monkeypatch):
    monkeypatch.setattr(sqlalchemy.engine.default.DefaultDialect, "initialize", lambda self, connection: None)
    dbapi = _SqlInfoUnimplemented()
    dbapi.catalog = "unknown"

    def execute(query):
        raise sqlalchemy.exc.NotSupportedError(
            query, {}, flightsql.NotSupportedError("default catalog discovery unsupported")
        )

    with pytest.raises(sqlalchemy.exc.InvalidRequestError, match="cannot verify.*catalog"):
        DataFusionDialect().initialize(SimpleNamespace(connection=dbapi, exec_driver_sql=execute))


@pytest.mark.skipif(not SQLALCHEMY_2, reason="numeric_dollar requires SQLAlchemy 2")
@pytest.mark.parametrize("fragment", ["text", "literal_column"])
@pytest.mark.parametrize("name", ["x", "actual"])
def test_prepared_raw_pyformat_fragments_fail_clearly_without_rewriting(fragment, name):
    engine = sqlalchemy.create_engine(
        URL.create(
            "datafusion",
            host="localhost",
            port=1,
            query={"insecure": "true", "feature-sqlalchemy-prepared-statements": "on"},
        )
    )
    expr = (sqlalchemy.text if fragment == "text" else sqlalchemy.literal_column)(f"'%({name})s'")
    stmt = select(expr, bindparam("actual", "value"))
    with pytest.raises(sqlalchemy.exc.CompileError, match="raw.*%.*bind parameter"):
        stmt.compile(dialect=engine.dialect)
    engine.dispose()


class BrokenProbeClient(CatalogScopedClient):
    """Fails one advisory discovery RPC (or its stream) while ``fail`` is set."""

    fail = True

    def __init__(self, rpc, error_type, **kwargs):
        super().__init__(**kwargs)
        self.rpc = rpc
        self.error_type = error_type

    def get_db_schemas(self, **kwargs):
        if self.fail and self.rpc == "get_db_schemas" and "catalog" not in kwargs:
            self.calls.append(("get_db_schemas", kwargs))
            raise self.error_type("probe failed")
        return super().get_db_schemas(**kwargs)

    def get_catalogs(self):
        if self.fail and self.rpc == "get_catalogs":
            self.calls.append(("get_catalogs", {}))
            raise self.error_type("probe failed")
        return super().get_catalogs()

    def do_get(self, ticket):
        if self.fail and ticket[0] == "catalogs" and self.rpc == "catalog_do_get":
            raise self.error_type("probe stream failed")
        return super().do_get(ticket)


UNSCOPED_METADATA_CALLS = [("get_tables", {"db_schema_filter_pattern": "public"}), ("get_db_schemas", {})]


def _assert_discovery_is_not_repeated(conn, client):
    """Later empty answers stand with only their own RPC, even once probes would work."""
    client.fail = False
    del client.calls[:]
    for _ in range(3):
        assert conn.flightsql_get_table_names("public") == []
        assert conn.flightsql_get_schema_names() == []
    assert client.calls == UNSCOPED_METADATA_CALLS * 3


TRANSIENT_PROBE_ERRORS = [flight.FlightUnavailableError, flight.FlightTimedOutError, flight.FlightCancelledError]
# INTERNAL and UNKNOWN (FlightServerError) from GetDbSchemas/GetCatalogs are server faults, so they are retried.
TRANSIENT_METADATA_PROBE_ERRORS = TRANSIENT_PROBE_ERRORS + [flight.FlightInternalError, flight.FlightServerError]
PERMANENT_METADATA_PROBE_ERRORS = [flight.FlightUnauthorizedError, flight.FlightUnauthenticatedError, pa.ArrowInvalid]


@pytest.mark.parametrize("rpc", ["get_db_schemas", "get_catalogs", "catalog_do_get"])
@pytest.mark.parametrize("error_type", TRANSIENT_METADATA_PROBE_ERRORS)
def test_transient_fallback_probe_failures_preserve_empty_metadata_and_retry(rpc, error_type):
    client = BrokenProbeClient(rpc, error_type)
    conn = Connection(client)
    assert conn.flightsql_get_table_names("missing") == []
    assert conn._metadata_catalog() is None
    assert conn._resolved_catalog is None
    # Once the transient failure clears, the same connection retries discovery.
    client.fail = False
    assert conn.flightsql_get_table_names("public") == ["orders", "east_orders"]
    assert conn.flightsql_get_schema_names() == ["information_schema", "public"]


@pytest.mark.parametrize("rpc", ["get_db_schemas", "get_catalogs", "catalog_do_get"])
@pytest.mark.parametrize("error_type", PERMANENT_METADATA_PROBE_ERRORS)
def test_permanent_fallback_probe_failures_are_cached_not_retried(rpc, error_type):
    client = BrokenProbeClient(rpc, error_type)
    conn = Connection(client)
    assert conn.flightsql_get_table_names("public") == []
    assert conn._resolved_catalog == (None,)
    _assert_discovery_is_not_repeated(conn, client)


@pytest.mark.parametrize("error_type", [flight.FlightInternalError, flight.FlightServerError])
def test_one_internal_get_catalogs_failure_does_not_hide_reflection_for_the_connection(error_type):
    """The information schema is on, so the default resolves once GetCatalogs answers."""

    class FlakyCatalogsClient(CatalogScopedClient):
        failures = 1

        def get_catalogs(self):
            if self.failures:
                self.failures -= 1
                self.calls.append(("get_catalogs", {}))
                raise error_type("catalog listing failed")
            return super().get_catalogs()

    client = FlakyCatalogsClient(catalogs=["datafusion", "cat2"])
    conn = Connection(client)
    assert conn.flightsql_get_table_names("public") == []
    assert conn._resolved_catalog is None
    assert conn.flightsql_get_table_names("public") == ["orders", "east_orders"]
    assert conn._metadata_catalog() == "datafusion"


def test_server_without_get_catalogs_is_probed_once():
    client = CatalogScopedClient(get_catalogs_implemented=False)
    conn = Connection(client)
    assert conn.flightsql_get_schema_names() == []
    assert [name for name, _ in client.calls] == ["get_db_schemas", "get_catalogs"]
    assert conn._resolved_catalog == (None,)
    _assert_discovery_is_not_repeated(conn, client)


def test_primary_metadata_error_is_not_swallowed_as_a_fallback_failure():
    class BrokenTablesClient(CatalogScopedClient):
        def get_tables(self, **kwargs):
            raise flight.FlightInternalError("primary GetTables failed")

    with pytest.raises(flightsql.InternalError, match="primary GetTables failed"):
        Connection(BrokenTablesClient()).flightsql_get_table_names("missing")


@pytest.mark.skipif(not SQLALCHEMY_2, reason="numeric_dollar requires SQLAlchemy 2")
def test_prepared_pyformat_string_is_safe_as_a_bound_value():
    engine = sqlalchemy.create_engine(
        URL.create(
            "datafusion",
            host="localhost",
            port=1,
            query={"insecure": "true", "feature-sqlalchemy-prepared-statements": "on"},
        )
    )
    compiled = select(bindparam("actual", "%(actual)s")).compile(dialect=engine.dialect)
    assert compiled.params == {"actual": "%(actual)s"}
    assert "$1" in str(compiled)
    engine.dispose()


@pytest.mark.skipif(not SQLALCHEMY_2, reason="SQLAlchemy 1.4 does not rewrite raw qmark text")
def test_raw_pyformat_fragment_also_rejects_qmark_rewriting():
    engine = sqlalchemy.create_engine(
        URL.create(
            "datafusion",
            host="localhost",
            port=1,
            query={"insecure": "true", "feature-sqlalchemy-prepared-statements": "on"},
        ),
        paramstyle="qmark",
    )
    with pytest.raises(sqlalchemy.exc.CompileError, match="raw.*bind parameter"):
        select(sqlalchemy.literal_column("'%(x)s'"), bindparam("x", 1)).compile(dialect=engine.dialect)
    engine.dispose()


@pytest.mark.parametrize("metadata", ["schemas", "tables"])
def test_multiple_catalogs_use_execution_default_not_datafusion(metadata):
    class OtherDefaultClient(CatalogScopedClient):
        def do_get(self, ticket):
            kind, catalog = ticket
            if catalog == "cat2":
                if kind == "schemas":
                    table = pa.table({"db_schema_name": ["public"]})
                else:
                    table = pa.table({"table_name": ["t"], "table_type": ["BASE TABLE"]})
                return SimpleNamespace(read_all=lambda: table)
            return super().do_get(ticket)

    client = OtherDefaultClient(catalogs=["datafusion", "cat2"], default_catalog="cat2")
    conn = Connection(client)
    if metadata == "schemas":
        assert conn.flightsql_get_schema_names() == ["public"]
    else:
        assert conn.flightsql_get_table_names("public") == ["t"]
    assert conn._metadata_catalog() == "cat2"
    assert conn.flightsql_get_table_names("public") == ["t"]
    assert len([call for call in client.calls if call[0] == "execute"]) == 1


@pytest.mark.parametrize("default", [None, "missing", ""])
def test_multiple_catalogs_without_verified_default_never_guess_datafusion(default):
    conn = Connection(CatalogScopedClient(catalogs=["datafusion", "cat2"], default_catalog=default))
    assert conn.flightsql_get_schema_names() == []
    assert conn._metadata_catalog() is None


class BrokenSettingsClient(CatalogScopedClient):
    """Fails the DataFusion settings query (or its stream) while ``fail`` is set."""

    fail = True

    def __init__(self, rpc, error_type, **kwargs):
        super().__init__(**kwargs)
        self.rpc = rpc
        self.error_type = error_type

    def execute(self, query):
        if self.fail and self.rpc == "execute":
            self.calls.append(("execute", {"query": query}))
            raise self.error_type("settings unavailable")
        return super().execute(query)

    def do_get(self, ticket):
        if self.fail and ticket[0] == "settings":
            if self.rpc == "settings_do_get":
                raise self.error_type("settings stream unavailable")
            if self.rpc == "settings_read":

                def read_all():
                    raise self.error_type("settings read failed")

                return SimpleNamespace(read_all=read_all)
        return super().do_get(ticket)


@pytest.mark.parametrize("rpc", ["execute", "settings_do_get", "settings_read"])
@pytest.mark.parametrize("error_type", TRANSIENT_PROBE_ERRORS)
def test_transient_default_catalog_discovery_failure_stays_unscoped_and_retries(rpc, error_type):
    client = BrokenSettingsClient(rpc, error_type, catalogs=["datafusion", "cat2"])
    conn = Connection(client)
    assert conn.flightsql_get_table_names("public") == []
    assert conn._resolved_catalog is None
    client.fail = False
    assert conn.flightsql_get_table_names("public") == ["orders", "east_orders"]


@pytest.mark.parametrize("rpc", ["execute", "settings_do_get", "settings_read"])
@pytest.mark.parametrize(
    "error_type",
    [
        pa.ArrowNotImplementedError,
        flight.FlightInternalError,  # how DataFusion reports a missing information_schema
        flight.FlightServerError,
        flight.FlightUnauthorizedError,
        pa.ArrowInvalid,
    ],
)
def test_denied_or_missing_default_catalog_settings_are_cached(rpc, error_type):
    client = BrokenSettingsClient(rpc, error_type, catalogs=["datafusion", "cat2"])
    conn = Connection(client)
    assert conn.flightsql_get_table_names("public") == []
    assert conn._resolved_catalog == (None,)
    assert [name for name, _ in client.calls].count("execute") == 1
    _assert_discovery_is_not_repeated(conn, client)


def test_multi_catalog_server_without_information_schema_caches_unresolved_discovery():
    """DataFusion's default config has no information_schema, so df_settings is missing.

    Reflection stays unscoped (empty, since the server only answers a named
    catalog) rather than guessing among catalogs, and that outcome is decided
    once: later metadata calls send only their own RPC.
    """

    class NoInformationSchemaClient(CatalogScopedClient):
        def execute(self, query):
            self.calls.append(("execute", {"query": query}))
            if "information_schema" in query:
                # Raised at GetFlightInfo by the planner, as observed live.
                raise flight.FlightInternalError("table 'information_schema.df_settings' not found")
            return super().execute(query)

    client = NoInformationSchemaClient(catalogs=["datafusion", "cat2"])
    conn = Connection(client)
    assert conn.flightsql_get_schema_names() == []
    assert [name for name, _ in client.calls] == ["get_db_schemas", "get_catalogs", "execute"]
    assert conn._resolved_catalog == (None,)
    _assert_discovery_is_not_repeated(conn, client)
    # An explicit catalog still scopes metadata on such a server.
    scoped = Connection(NoInformationSchemaClient(catalogs=["datafusion", "cat2"]), catalog="datafusion")
    assert scoped.flightsql_get_schema_names() == ["information_schema", "public"]


@pytest.mark.skipif(not SQLALCHEMY_2, reason="SQLAlchemy 1.4 does not rewrite raw positional text")
@pytest.mark.parametrize("fragment", ["text", "literal_column"])
@pytest.mark.parametrize("literal_binds", [False, True])
@pytest.mark.parametrize("paramstyle", ["qmark", "numeric"])
def test_literal_raw_pyformat_fragments_fail_before_positional_rewrite(fragment, literal_binds, paramstyle):
    dialect = DataFusionDialect(paramstyle=paramstyle)
    dialect.statement_compiler = LiteralBindCompiler
    expression = (sqlalchemy.text if fragment == "text" else sqlalchemy.literal_column)("SELECT '%(x)s'")
    with pytest.raises(sqlalchemy.exc.CompileError, match="raw.*bind parameter"):
        expression.compile(dialect=dialect, compile_kwargs={"literal_binds": literal_binds})


@pytest.mark.parametrize("literal_binds", [False, True])
def test_literal_pyformat_string_is_safe_as_a_bound_value(literal_binds):
    dialect = DataFusionDialect()
    dialect.statement_compiler = LiteralBindCompiler
    compiled = select(bindparam("x", "%(x)s")).compile(dialect=dialect, compile_kwargs={"literal_binds": literal_binds})
    if literal_binds:
        assert "'%(x)s'" in str(compiled)
    else:
        expanded = compiled._process_parameters_for_postcompile(compiled.construct_params())
        assert "'%(x)s'" in expanded.statement


RAW_PERCENT_FRAGMENT = "'%(x)s %a%'"


def _compile_raw_fragment(fragment, compiler_name, paramstyle):
    dialect = DataFusionDialect(paramstyle=paramstyle)
    dialect.statement_compiler = LiteralBindCompiler if compiler_name == "literal" else PreparedStatementCompiler
    expression = (sqlalchemy.text if fragment == "text" else sqlalchemy.literal_column)(RAW_PERCENT_FRAGMENT)
    compiled = select(expression, bindparam("x", 5)).compile(dialect=dialect)
    if compiler_name == "literal":
        statement = compiled._process_parameters_for_postcompile(compiled.construct_params()).statement
        assert statement.endswith(", 5 AS anon_1")
        return statement
    return str(compiled)


@pytest.mark.skipif(SQLALCHEMY_2, reason="SQLAlchemy 2 rewrites raw positional text, so the guard applies")
@pytest.mark.parametrize("fragment", ["text", "literal_column"])
@pytest.mark.parametrize("compiler_name", ["literal", "prepared"])
@pytest.mark.parametrize("paramstyle", ["qmark", "numeric"])
def test_sqlalchemy_14_keeps_raw_pyformat_fragments_unchanged(fragment, compiler_name, paramstyle):
    if compiler_name == "literal" and paramstyle == "numeric":
        pytest.skip("SQLAlchemy 1.4 has no post-compile literal support for the numeric paramstyle")
    assert _compile_raw_fragment(fragment, compiler_name, paramstyle).startswith(f"SELECT {RAW_PERCENT_FRAGMENT}, ")


@pytest.mark.skipif(SQLALCHEMY_2, reason="covers the SQLAlchemy 1.4 compiler")
@pytest.mark.parametrize("fragment", ["text", "literal_column"])
@pytest.mark.parametrize("compiler_name", ["literal", "prepared"])
@pytest.mark.parametrize("paramstyle", ["format", "pyformat"])
def test_sqlalchemy_14_format_paramstyles_double_every_percent_in_raw_text(fragment, compiler_name, paramstyle):
    # SQLAlchemy escapes "%" for drivers that %-interpolate; this driver does
    # not, so the doubled text reaches the server. Use qmark (the default).
    doubled = RAW_PERCENT_FRAGMENT.replace("%", "%%")
    assert _compile_raw_fragment(fragment, compiler_name, paramstyle).startswith(f"SELECT {doubled}, ")
