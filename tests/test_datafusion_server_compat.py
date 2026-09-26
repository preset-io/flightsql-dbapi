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
    _, kwargs = dialect.create_connect_args(make_url("datafusion://localhost:50051?insecure=true"))
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
    assert dialect.is_disconnect(flightsql.OperationalError("Flight returned unavailable error"), None, None)
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
