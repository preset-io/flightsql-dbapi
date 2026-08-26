import warnings
from typing import Any, Callable, Dict, MutableMapping, Sequence, Tuple

from sqlalchemy import exc, pool
from sqlalchemy.dialects import registry
from sqlalchemy.engine import URL, default, reflection
from sqlalchemy.sql import compiler, elements

import flightsql.flightsql_pb2 as flightsql
from flightsql.client import FlightSQLClient
from flightsql.dbapi import TableMetadataResult

feature_prefix = "feature-"

FEATURE_PREPARED_STATEMENTS = "sqlalchemy-prepared-statements"
FEATURE_PRIMARY_KEYS = "sqlalchemy-primary-keys"

_TABLE_METADATA_CACHE_NAMESPACE = "flightsql-table-metadata"

_TRUE_QUERY_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_QUERY_VALUES = frozenset({"", "0", "false", "no", "off"})


def _import_flightsql_dbapi(_dialect_cls):
    import flightsql as dbapi

    return dbapi


def _dbapi_loader_function(name: str, value: Any) -> Callable[..., Any]:
    """Return a DBAPI classmethod's function or reject an ambiguous override."""
    if not isinstance(value, classmethod):
        raise TypeError(f"{name} must be declared with @classmethod")
    return value.__func__


def _parse_boolean_query_value(name: str, value: str = "") -> bool:
    normalized = value.strip().lower()
    if normalized in _TRUE_QUERY_VALUES:
        return True
    if normalized in _FALSE_QUERY_VALUES:
        return False
    choices = ", ".join(
        "<empty>" if choice == "" else choice for choice in sorted(_TRUE_QUERY_VALUES | _FALSE_QUERY_VALUES)
    )
    raise exc.ArgumentError(f"invalid boolean value for {name!r}: {value!r}; expected one of: {choices}")


def client_from_url(url: URL) -> FlightSQLClient:
    fields = url.translate_connect_args(username="user")

    # SQLAlchemy represents repeated query parameters as tuples. Flight RPC
    # metadata accepts a single value per key, so preserve the last value in
    # the same way FlightSQLClient resolves duplicate call-option headers.
    metadata: Dict[str, str] = {
        key.lower(): value if isinstance(value, str) else value[-1] for key, value in url.query.items()
    }
    insecure = _parse_boolean_query_value("insecure", metadata.pop("insecure", ""))
    disable_server_verification = _parse_boolean_query_value(
        "disable_server_verification", metadata.pop("disable_server_verification", "")
    )
    if insecure and disable_server_verification:
        raise exc.ArgumentError("insecure and disable_server_verification cannot both be true")
    token = metadata.pop("token", None)

    features = {}
    for k in list(metadata.keys()):
        if k.startswith(feature_prefix):
            features[k[len(feature_prefix) :]] = metadata.pop(k)

    return FlightSQLClient(
        host=fields["host"],
        port=fields["port"],
        user=fields.pop("user", None),
        password=fields.pop("password", None),
        token=token,
        insecure=insecure,
        disable_server_verification=disable_server_verification,
        metadata=metadata,
        features=features,
    )


class FlightSQLDialect(default.DefaultDialect):
    """
    Establishes baseline behavior of a FlightSQL Dialect. All other
    Dialects extend from this base class.
    """

    driver = "flightsql"
    sql_info: Dict[int, Any] = {}

    sql_info_values = [
        flightsql.FLIGHT_SQL_SERVER_NAME,
        flightsql.FLIGHT_SQL_SERVER_ARROW_VERSION,
        flightsql.FLIGHT_SQL_SERVER_READ_ONLY,
        flightsql.SQL_IDENTIFIER_QUOTE_CHAR,
    ]

    # SQLAlchemy 2 only recognizes import_dbapi when it is present directly on
    # the concrete dialect class. Install the hook on every subclass so the
    # documented FlightSQLDialect extension point remains warning-free.
    import_dbapi = classmethod(_import_flightsql_dbapi)
    dbapi = classmethod(_import_flightsql_dbapi)  # type: ignore[assignment]
    _degraded_reflection_warning_emitted = False

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        direct_import = cls.__dict__.get("import_dbapi")
        direct_legacy = cls.__dict__.get("dbapi")

        if direct_import is not None and direct_legacy is not None:
            import_loader = _dbapi_loader_function("import_dbapi", direct_import)
            legacy_loader = _dbapi_loader_function("dbapi", direct_legacy)
            if import_loader is not legacy_loader:
                raise TypeError("define only import_dbapi or dbapi; SQLAlchemy 1.4 and 2.x must use one loader")
            loader = import_loader
        elif direct_import is not None:
            loader = _dbapi_loader_function("import_dbapi", direct_import)
        elif direct_legacy is not None:
            loader = _dbapi_loader_function("dbapi", direct_legacy)
        else:
            inherited_import = getattr(cls, "import_dbapi")
            loader = inherited_import.__func__

        # SQLAlchemy 2 requires import_dbapi directly on the concrete class,
        # while SQLAlchemy 1.4 calls dbapi. Rebind one canonical loader under
        # both names so inherited custom loaders and their keyword signatures
        # remain identical across every generation of a dialect hierarchy.
        cls.import_dbapi = classmethod(loader)
        cls.dbapi = classmethod(loader)

    def connect(self, *args, **kwargs):
        return self.dbapi.connect(*args, **kwargs)

    def initialize(self, connection):
        super().initialize(connection)

        self.sql_info = connection.connection.flightsql_get_sql_info(self.sql_info_values)

        # Set the quote character for identifiers.
        self.identifier_preparer.initial_quote = self.sql_info[flightsql.SQL_IDENTIFIER_QUOTE_CHAR]
        self.identifier_preparer.final_quote = self.identifier_preparer.initial_quote

        read_only = self.sql_info[flightsql.FLIGHT_SQL_SERVER_READ_ONLY]
        self.supports_delete = not read_only
        self.supports_alter = not read_only

    def create_connect_args(self, url: URL) -> Tuple[Sequence[Any], MutableMapping[str, Any]]:
        client = client_from_url(url)
        return [client], {}

    @reflection.cache
    def get_columns(self, connection, table_name, schema=None, **kwargs):
        info_cache = kwargs.get("info_cache")
        metadata_key = (_TABLE_METADATA_CACHE_NAMESPACE, schema)
        if info_cache is not None and metadata_key in info_cache:
            return info_cache[metadata_key].get(table_name, [])
        return connection.connection.flightsql_get_columns(table_name, schema)

    @reflection.cache
    def get_table_names(self, connection, schema=None, **kwargs):
        info_cache = kwargs.get("info_cache")
        result = connection.connection.flightsql_get_table_metadata(schema)
        if not isinstance(result, TableMetadataResult):
            # Keep the documented dialect extension point tolerant of DB API
            # adapters written against the earlier internal dictionary shape.
            if result is None:
                table_names = connection.connection.flightsql_get_table_names(schema)
                result = TableMetadataResult(table_names, None, False)
            else:
                result = TableMetadataResult(list(result), result, True)
        if result.columns_by_name is not None and info_cache is not None:
            info_cache[(_TABLE_METADATA_CACHE_NAMESPACE, schema)] = result.columns_by_name
        if result.table_names and not result.included_schema_supported:
            self._warn_degraded_reflection(schema)
            reflectable_names = [
                table_name
                for table_name in result.table_names
                if self.get_columns(connection, table_name, schema=schema, info_cache=info_cache)
            ]
            # Per-table included schemas can filter server-internal/stale rows
            # when available. If every probe is empty, included schemas are
            # unsupported globally: preserve the names rather than blacking
            # reflection out.
            if reflectable_names:
                return reflectable_names
        return result.table_names

    def _warn_degraded_reflection(self, schema):
        if self._degraded_reflection_warning_emitted:
            return
        self._degraded_reflection_warning_emitted = True
        scope = "all schemas" if schema is None else f"schema {schema!r}"
        warnings.warn(
            "Flight SQL GetTables(include_schema=True) returned names but no usable Arrow table_schema metadata "
            f"for {scope}. Table names and has_table() remain available, but reflected columns may be empty; "
            "configure or upgrade the server to honor include_schema and return serialized Arrow schemas.",
            exc.SAWarning,
            stacklevel=3,
        )

    @reflection.cache
    def get_schema_names(self, connection, **kwargs):
        return connection.connection.flightsql_get_schema_names()

    @reflection.cache
    def has_table(self, connection, table_name, schema=None, **kwargs):
        return table_name in self.get_table_names(
            connection,
            schema=schema,
            info_cache=kwargs.get("info_cache"),
        )

    def get_indexes(self, connection, table_name, schema=None, **kwargs):
        return []

    def get_pk_constraint(self, connection, table_name, schema=None, **kwargs):
        conn = connection.connection
        primary_keys_enabled = conn.features.get(FEATURE_PRIMARY_KEYS)
        if primary_keys_enabled != "on":
            return {"constrained_columns": [], "name": None}

        columns = conn.flightsql_get_primary_keys(table_name, schema=schema)
        if len(columns) == 0:
            return {"constrained_columns": [], "name": None}
        names = [v["column_name"] for v in columns]
        return {"constrained_columns": names, "name": columns[0]["key_name"]}

    def get_foreign_keys(self, connection, table_name, schema=None, **kwargs):
        return []

    def get_view_names(self, connection, schema=None, **kwargs):
        return []


class LiteralBindCompiler(compiler.SQLCompiler):
    # Render bind parameters into the SQL immediately before execution. IOx
    # does not support prepared statements, but SQLAlchemy's post-compile
    # literal tokens keep cached statements independent of prior values.
    # TODO: Remove this when we're able to support prepared statements.
    def visit_empty_set_expr(self, element_types, **kwargs):
        # SQLAlchemy 1.4's backend-neutral compiler requires third-party
        # dialects to supply this expression. Keep it valid as the contents of
        # both scalar and tuple IN clauses; SQLAlchemy 2 can use it as well.
        columns = ", ".join("1" for _ in element_types)
        return f"SELECT {columns} WHERE 1 != 1"

    def visit_bindparam(self, bindparam, within_columns_clause=False, literal_binds=False, **kwargs):
        if literal_binds:
            kwargs["literal_execute"] = False
            return super().visit_bindparam(
                bindparam,
                within_columns_clause=within_columns_clause,
                literal_binds=True,
                **kwargs,
            )

        kwargs["literal_execute"] = True
        return super().visit_bindparam(
            bindparam,
            within_columns_clause=within_columns_clause,
            literal_binds=False,
            **kwargs,
        )

    def render_literal_value(self, value, type_):
        # SQLAlchemy 1.4.6 passes execution-time None values directly to the
        # type's literal processor. String/Integer/NullType processors either
        # fail or can produce a non-SQL value. Compile the SQL NULL expression
        # through SQLAlchemy's visitor contract; never return user text such as
        # the string "NULL" from a type processor.
        if value is None:
            return self.process(elements.Null._instance())
        return super().render_literal_value(value, type_)


class DataFusionDialect(FlightSQLDialect):
    """
    DataFusionDialect is a SQLAlchemy Dialect that uses Flight SQL as its
    transport layer and for metadata lookups. It is specifically tuned for the
    baseline configuration of a DataFusion execution engine.

    Metadata reflection uses the Flight SQL GetTables/GetDbSchemas and key RPCs.
    Servers that ignore GetTables(include_schema=True) can still provide table
    names and has_table results, but cannot provide complete column reflection.
    """

    name = "datafusion"

    paramstyle = "qmark"
    poolclass = pool.SingletonThreadPool
    returns_unicode_strings = True
    supports_default_values = False
    supports_empty_insert = False
    supports_native_boolean = True
    supports_pk_autoincrement = False
    supports_statement_cache = True
    supports_unicode_binds = True
    supports_unicode_statements = True
    supports_sane_rowcount = False
    supports_sane_multi_rowcount = False

    def initialize(self, connection):
        super().initialize(connection)

        # Use the literal binding SQL compiler if we haven't turned on the
        # prepared statements feature. This ensures that the client won't
        # attempt to create a prepared statement if the upstream server isn't
        # expected to support it.
        prepared_statements_enabled = connection.connection.features.get(FEATURE_PREPARED_STATEMENTS)
        if prepared_statements_enabled != "on":
            self.statement_compiler = LiteralBindCompiler
        else:
            self.statement_compiler = compiler.SQLCompiler


registry.register("datafusion.flightsql", "flightsql.sqlalchemy", "DataFusionDialect")
