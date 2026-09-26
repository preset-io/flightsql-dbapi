import datetime
import inspect
import warnings
from dataclasses import dataclass
from typing import Any, Callable, Dict, MutableMapping, Sequence, Tuple

import sqlalchemy
from sqlalchemy import exc, pool
from sqlalchemy.dialects import registry
from sqlalchemy.engine import URL, default, reflection
from sqlalchemy.sql import compiler, elements, sqltypes

import flightsql.flightsql_pb2 as flightsql
from flightsql.client import FlightSQLClient
from flightsql.dbapi import TableMetadataResult
from flightsql.exceptions import NotSupportedError, OperationalError

feature_prefix = "feature-"

_SQLALCHEMY_2 = int(sqlalchemy.__version__.split(".", 1)[0]) >= 2

FEATURE_PREPARED_STATEMENTS = "sqlalchemy-prepared-statements"
FEATURE_PRIMARY_KEYS = "sqlalchemy-primary-keys"

_TABLE_METADATA_CACHE_NAMESPACE = "flightsql-table-metadata"


@dataclass(frozen=True)
class _TableColumnCacheEntry:
    status: str
    columns: Tuple[Dict[str, Any], ...] = ()


_MISSING_TABLE = _TableColumnCacheEntry("missing")
_UNREFLECTABLE_TABLE = _TableColumnCacheEntry("unreflectable")

_TRUE_QUERY_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_QUERY_VALUES = frozenset({"", "0", "false", "no", "off"})


def _reflection_warning_stacklevel() -> int:
    """Point warnings through SQLAlchemy wrappers to the consumer callsite."""
    stack = inspect.stack(context=0)
    try:
        for stacklevel, frame_info in enumerate(stack[1:], start=1):
            module_name = frame_info.frame.f_globals.get("__name__", "")
            if module_name == __name__ or module_name.startswith("sqlalchemy."):
                continue
            if frame_info.filename == "<string>":
                continue
            return stacklevel
    finally:
        # Frame objects retain locals; release them after this rare warning.
        del stack
    return 2


def _is_view_type(table_type) -> bool:
    return isinstance(table_type, str) and table_type.strip().upper() == "VIEW"


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
    # Used when the server does not report SQL_IDENTIFIER_QUOTE_CHAR.
    default_identifier_quote = '"'

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

        try:
            self.sql_info = connection.connection.flightsql_get_sql_info(self.sql_info_values)
        except NotSupportedError:
            # GetSqlInfo is optional in practice: the DataFusion Flight SQL
            # service answers UNIMPLEMENTED. Fall back to the dialect defaults
            # instead of failing every connection.
            self.sql_info = {}

        # Set the quote character for identifiers.
        quote = self.sql_info.get(flightsql.SQL_IDENTIFIER_QUOTE_CHAR) or self.default_identifier_quote
        self.identifier_preparer.initial_quote = quote
        self.identifier_preparer.final_quote = quote

        read_only = self.sql_info.get(flightsql.FLIGHT_SQL_SERVER_READ_ONLY)
        if read_only is not None:
            self.supports_delete = not read_only
            self.supports_alter = not read_only

    def create_connect_args(self, url: URL) -> Tuple[Sequence[Any], MutableMapping[str, Any]]:
        client = client_from_url(url)
        # The URL database component names the Flight SQL catalog that scopes
        # reflection, e.g. datafusion://host:port/datafusion.
        return [client], ({"catalog": url.database} if url.database else {})

    def is_disconnect(self, e, connection, cursor):
        if isinstance(e, OperationalError):
            text = str(e).lower()
            return "unavailable" in text or "connection" in text or "socket closed" in text
        return False

    @reflection.cache
    def get_columns(self, connection, table_name, schema=None, **kwargs):
        info_cache = kwargs.get("info_cache")
        cache_key = self._column_cache_key(schema, table_name)
        entry = info_cache.get(cache_key) if info_cache is not None else None
        if not isinstance(entry, _TableColumnCacheEntry):
            entry = self._probe_table_columns(connection, table_name, schema)
            if info_cache is not None:
                info_cache[cache_key] = entry
        return self._columns_from_cache_entry(entry, table_name, schema)

    @reflection.cache
    def get_table_names(self, connection, schema=None, **kwargs):
        names, table_types = self._relation_names(connection, schema, kwargs.get("info_cache"))
        return [name for name in names if not _is_view_type(table_types.get(name))]

    @reflection.cache
    def get_view_names(self, connection, schema=None, **kwargs):
        names, table_types = self._relation_names(connection, schema, kwargs.get("info_cache"))
        return [name for name in names if _is_view_type(table_types.get(name))]

    def _relation_names(self, connection, schema, info_cache):
        """Return every GetTables name (tables and views) and its table_type."""
        key = (_TABLE_METADATA_CACHE_NAMESPACE, "relations", schema)
        if info_cache is not None and key in info_cache:
            return info_cache[key]
        result = self._relation_names_uncached(connection, schema, info_cache)
        if info_cache is not None:
            info_cache[key] = result
        return result

    def _relation_names_uncached(self, connection, schema, info_cache):
        result = connection.connection.flightsql_get_table_metadata(schema)
        if not isinstance(result, TableMetadataResult):
            # Keep the documented dialect extension point tolerant of DB API
            # adapters written against the earlier internal dictionary shape.
            if result is None:
                table_names = connection.connection.flightsql_get_table_names(schema)
                result = TableMetadataResult(table_names, None, False)
            else:
                result = TableMetadataResult(list(result), result, True)

        # A cache entry is qualified by both schema and table.  This prevents
        # identically named tables in different schemas from sharing columns,
        # while retaining the one-bulk-RPC fast path for complete metadata.
        columns_by_name = result.columns_by_name or {}
        for table_name, columns in columns_by_name.items():
            self._cache_table_columns(info_cache, schema, table_name, columns)

        table_types = result.table_types or {}
        unresolved = [name for name in dict.fromkeys(result.table_names) if name not in columns_by_name]
        if not unresolved and result.included_schema_supported:
            return list(dict.fromkeys(result.table_names)), table_types

        names = [name for name in dict.fromkeys(result.table_names) if name in columns_by_name]
        recovered = 0
        unreflectable = 0
        stale = 0
        for table_name in unresolved:
            entry = self._probe_table_columns(connection, table_name, schema)
            if info_cache is not None:
                info_cache[self._column_cache_key(schema, table_name)] = entry
            if entry.status == "missing":
                stale += 1
                continue
            names.append(table_name)
            if entry.status == "columns":
                recovered += 1
            else:
                unreflectable += 1

        if result.table_names and (unresolved or not result.included_schema_supported):
            self._warn_degraded_reflection(
                schema,
                probes=len(unresolved),
                recovered=recovered,
                unreflectable=unreflectable,
                stale=stale,
            )
        return names, table_types

    @staticmethod
    def _column_cache_key(schema, table_name):
        return (_TABLE_METADATA_CACHE_NAMESPACE, "columns", schema, table_name)

    def _cache_table_columns(self, info_cache, schema, table_name, columns):
        if info_cache is not None:
            info_cache[self._column_cache_key(schema, table_name)] = _TableColumnCacheEntry("columns", tuple(columns))

    def _probe_table_columns(self, connection, table_name, schema):
        dbapi_connection = connection.connection
        probe = getattr(dbapi_connection, "flightsql_get_table_metadata_for_table", None)
        if callable(probe):
            result = probe(table_name, schema)
            if not isinstance(result, TableMetadataResult):
                raise exc.InvalidRequestError(
                    "flightsql_get_table_metadata_for_table() must return TableMetadataResult"
                )
            columns_by_name = result.columns_by_name or {}
            if table_name in columns_by_name:
                return _TableColumnCacheEntry("columns", tuple(columns_by_name[table_name]))
            if table_name in result.table_names:
                return _UNREFLECTABLE_TABLE
            return _MISSING_TABLE

        # Compatibility for custom DB API adapters predating the richer probe:
        # an empty list historically meant that the filtered request did not
        # find a reflectable table, so treat it as missing rather than creating
        # a false columnless Table object.
        columns = dbapi_connection.flightsql_get_columns(table_name, schema)
        if columns:
            return _TableColumnCacheEntry("columns", tuple(columns))
        return _MISSING_TABLE

    @staticmethod
    def _columns_from_cache_entry(entry, table_name, schema):
        qualified_name = table_name if schema is None else f"{schema}.{table_name}"
        if entry.status == "missing":
            raise exc.NoSuchTableError(qualified_name)
        if entry.status == "unreflectable":
            raise exc.UnreflectableTableError(
                f"Flight SQL reports table {qualified_name!r}, but filtered GetTables(include_schema=True) "
                "did not return a parseable Arrow table_schema; check metadata permissions and server support"
            )
        return [dict(column) for column in entry.columns]

    def _warn_degraded_reflection(self, schema, *, probes, recovered, unreflectable, stale):
        warned_schemas = getattr(self, "_degraded_reflection_warned_schemas", None)
        if warned_schemas is None:
            warned_schemas = set()
            self._degraded_reflection_warned_schemas = warned_schemas
        if schema in warned_schemas:
            return
        warned_schemas.add(schema)
        scope = "all schemas" if schema is None else f"schema {schema!r}"
        warnings.warn(
            "Flight SQL GetTables(include_schema=True) returned incomplete Arrow table_schema metadata for "
            f"{scope}; issued {probes} filtered probe(s): {recovered} recovered, {unreflectable} still "
            f"unreflectable, {stale} stale row(s) excluded. Existing unreflectable names remain visible to "
            "has_table() but get_columns() fails explicitly. Grant schema-metadata permission or configure/upgrade "
            "the server to return a serialized Arrow schema for every reported table.",
            exc.SAWarning,
            stacklevel=_reflection_warning_stacklevel(),
        )

    @reflection.cache
    def get_schema_names(self, connection, **kwargs):
        return connection.connection.flightsql_get_schema_names()

    @reflection.cache
    def has_table(self, connection, table_name, schema=None, **kwargs):
        # SQLAlchemy 2 defines has_table() as true for views as well.
        names, _table_types = self._relation_names(connection, schema, kwargs.get("info_cache"))
        return table_name in names

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

    def _literal_execute_expanding_parameter_literal_binds(
        self,
        parameter,
        values,
        bind_expression_template=None,
    ):
        # SQLAlchemy 1.4.6 asserts that an empty literal-execute expanding
        # parameter is scalar, even when IN coercion assigned it TupleType.
        # Render the correctly shaped empty subquery before reaching that old
        # assertion.  Nonempty values and newer SQLAlchemy behavior continue
        # through the upstream compiler implementation.
        if not values and parameter.type._is_tuple_type:
            return (), self.visit_empty_set_expr(parameter.type.types)
        if bind_expression_template is None:
            return super()._literal_execute_expanding_parameter_literal_binds(parameter, values)
        return super()._literal_execute_expanding_parameter_literal_binds(
            parameter,
            values,
            bind_expression_template=bind_expression_template,
        )

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
        if value is None and not type_.should_evaluate_none:
            return self.process(elements.Null._instance())
        if type_._isnull:
            # Untyped binds (e.g. text(":v")) are typed from the Python value
            # with SQLAlchemy's own literal resolver. Values it cannot map stay
            # NullType and still fail closed below.
            type_ = sqltypes._resolve_value_to_type(value)
        # Typed temporal/binary literals: a quoted string would be compared or
        # returned as text rather than as the SQL type.
        if isinstance(value, datetime.datetime) and type_._type_affinity is sqltypes.DateTime:
            return f"TIMESTAMP '{value.isoformat(sep=' ')}'"
        if isinstance(value, datetime.date) and not isinstance(value, datetime.datetime):
            if type_._type_affinity is sqltypes.Date:
                return f"DATE '{value.isoformat()}'"
        if isinstance(value, datetime.time) and type_._type_affinity is sqltypes.Time:
            if value.tzinfo is not None:
                raise exc.CompileError("time zone aware time literals are not supported")
            return f"TIME '{value.isoformat()}'"
        if isinstance(value, (bytes, bytearray, memoryview)) and type_._type_affinity is sqltypes._Binary:
            return f"X'{bytes(value).hex()}'"
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

    def create_connect_args(self, url: URL) -> Tuple[Sequence[Any], MutableMapping[str, Any]]:
        args, kwargs = super().create_connect_args(url)
        # DataFusion binds numbered placeholders ($1, $2, ...); every bare "?"
        # is one and the same placeholder to it, so a prepared statement with
        # two differently typed "?" binds fails to prepare. Only the opt-in
        # prepared-statement compiler emits placeholders, so switch only that
        # path. The default literal-bind path keeps qmark: under numeric
        # paramstyles SQLAlchemy re-scans the post-compiled statement for
        # %(name)s, which would corrupt literal values containing that text.
        if args[0].features.get(FEATURE_PREPARED_STATEMENTS) == "on" and _SQLALCHEMY_2:
            self.paramstyle = "numeric_dollar"
            self.positional = True
        return args, kwargs

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
