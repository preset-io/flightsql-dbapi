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
from sqlalchemy.engine import make_url
from sqlalchemy.sql import sqltypes

import flightsql
from flightsql import client as flight_client
from flightsql.dbapi import (
    Connection,
    Cursor,
    TableMetadataResult,
    build_parameter_record,
)
from flightsql.sqlalchemy import DataFusionDialect, LiteralBindCompiler

SQLALCHEMY_2 = int(sqlalchemy.__version__.split(".")[0]) >= 2


def _endpoint_info(ticket):
    return SimpleNamespace(endpoints=[SimpleNamespace(ticket=ticket)])


class CatalogScopedClient:
    """Answers metadata only for the catalog named in the request."""

    features = {}

    def __init__(self, catalogs=("datafusion",), get_catalogs_implemented=True):
        self.catalogs = list(catalogs)
        self.get_catalogs_implemented = get_catalogs_implemented
        self.calls = []

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
        if kind == "catalogs":
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
    assert all("catalog" not in kwargs for name, kwargs in ambiguous.calls if name != "get_catalogs")


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
        if kind == "catalogs":
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
        "datafusion://localhost:1?insecure=true&feature-sqlalchemy-prepared-statements=on",
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
