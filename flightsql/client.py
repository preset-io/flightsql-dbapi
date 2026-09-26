from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

import pyarrow as pa
import pyarrow.ipc  # noqa: F401 - pa.ipc.read_schema
from google.protobuf import any_pb2
from pyarrow import flight
from pyarrow.ipc import IpcReadOptions, IpcWriteOptions

import flightsql.flightsql_pb2 as flightsql
from flightsql.util import check_closed


@dataclass
class TableRef:
    """Reference information for a specific table."""

    catalog: Optional[str] = None
    """
    Specifies the catalog the table belongs to. `None` means any catalog and
    an empty string means a table without a catalog.
    """

    db_schema: Optional[str] = None
    """
    Specifies the DB schema the table belongs to. `None` means any DB schema and
    an empty string means a table without a DB schema.
    """

    table: str = ""
    """
    Name of the table.
    """


@dataclass
class FlightSQLCallOptions:
    """
    Collection of options to send with the RPC. These are converted to
    `flight.CallOptions` when the RPC is invoked.
    """

    timeout: Optional[float] = None
    headers: Optional[List[Tuple[bytes, bytes]]] = None
    write_options: Optional[IpcWriteOptions] = None
    read_options: Optional[IpcReadOptions] = None


class PreparedStatement:
    """
    Scoped prepared statement execution.

    Closing the `PreparedStatement` will invoke a close action with server.
    """

    def __init__(self, client: flight.FlightClient, options: flight.FlightCallOptions, handle: bytes):
        self.client = client
        self.options = options
        self.handle = handle
        self.dataset_schema: Optional[pa.Schema] = None
        self.parameter_schema: Optional[pa.Schema] = None
        self.closed = False

    def __enter__(self) -> "PreparedStatement":
        return self

    def __exit__(self, *args) -> None:
        self.close()

    @check_closed
    def execute(self, binding: pa.RecordBatch) -> flight.FlightInfo:
        """
        Apply parameter bindings to the prepared statement and execute the query.
        """
        cmd = flightsql.CommandPreparedStatementQuery(prepared_statement_handle=self.handle)
        desc = flight_descriptor(cmd)

        if binding is not None and binding.num_rows > 0:
            writer, reader = self.client.do_put(desc, binding.schema, self.options)
            writer.write(binding)
            writer.done_writing()
            metadata = reader.read()
            writer.close()
            # Flight SQL servers may return DoPutPreparedStatementResult with a
            # new handle that carries the bound parameters (the DataFusion
            # service is stateless and does exactly this). Executing the
            # original handle would run the statement without its parameters.
            updated = _updated_prepared_handle(metadata.to_pybytes() if metadata is not None else b"")
            if updated:
                self.handle = updated
                cmd = flightsql.CommandPreparedStatementQuery(prepared_statement_handle=updated)
                desc = flight_descriptor(cmd)

        return self.client.get_flight_info(desc, self.options)

    @check_closed
    def close(self) -> None:
        """
        Close the prepared statement.
        """
        request = flight_action(
            "ClosePreparedStatement",
            flightsql.ActionClosePreparedStatementRequest(prepared_statement_handle=self.handle),
        )
        self.client.do_action(request, self.options)
        self.closed = True


class FlightSQLClient:
    """
    Wrapper around a `flight.FlightClient` that implements the Flight SQL
    protocol. This client should be retired when/if pyarrow receives an official
    Flight SQL client.
    """

    def __init__(self, *args, features: Optional[Dict[str, str]] = None, **kwargs):
        """Initializes the client with the following parameters (see create_flight_client()):

        host, port, user, password, token, insecure, disable_server_verification, metadata, features

        Additional keyword arguments are passed on as keyword arguments to flight.FlightClient
        """
        client, headers = create_flight_client(*args, **kwargs)
        self.client = client
        self.headers = headers
        self.features = features or {}
        self.closed = False

    def close(self):
        """Close the client."""
        self.closed = True
        self.client.close()

    def execute(self, query: str, call_options: Optional[FlightSQLCallOptions] = None):
        """
        Execute a query. Returns a `flight.FlightInfo` object to specify the
        retrieval location for Arrow data.
        """
        return self._get_flight_info(flightsql.CommandStatementQuery(query=query), call_options)

    def execute_update(self, query: str, call_options: Optional[FlightSQLCallOptions] = None):
        """Execute an update query and return the number of updated rows."""
        cmd = flightsql.CommandStatementUpdate(query=query)
        desc = flight_descriptor(cmd)
        writer, reader = self._do_put(desc, call_options)
        result = reader.read()
        writer.close()

        if result is None:
            return 0
        update_result = flightsql.DoPutUpdateResult()
        update_result.ParseFromString(result.to_pybytes())
        return update_result.record_count

    def get_tables(
        self,
        include_schema=False,
        catalog: Optional[str] = None,
        table_types: Optional[Iterable[str]] = None,
        table_name_filter_pattern: Optional[str] = None,
        db_schema_filter_pattern: Optional[str] = None,
        call_options: Optional[FlightSQLCallOptions] = None,
    ):
        """
        Requests a list of tables and returns a `flight.FlightInfo`
        object to specify the retrieval location for Arrow data.
        """
        cmd = flightsql.CommandGetTables(
            catalog=catalog,
            include_schema=include_schema,
            table_name_filter_pattern=table_name_filter_pattern,
            table_types=table_types,
            db_schema_filter_pattern=db_schema_filter_pattern,
        )
        return self._get_flight_info(cmd, call_options)

    def get_db_schemas(
        self,
        catalog: Optional[str] = None,
        db_schema_filter_pattern: Optional[str] = None,
        call_options: Optional[FlightSQLCallOptions] = None,
    ):
        """
        Requests a list of schemas and returns a `flight.FlightInfo`
        object to specify the retrieval location for Arrow data.
        """
        cmd = flightsql.CommandGetDbSchemas(catalog=catalog, db_schema_filter_pattern=db_schema_filter_pattern)
        return self._get_flight_info(cmd, call_options)

    def get_catalogs(self, call_options: Optional[FlightSQLCallOptions] = None):
        """
        Requests a list of catalogs and returns a `flight.FlightInfo`
        object to specify the retrieval location for Arrow data.
        """
        return self._get_flight_info(flightsql.CommandGetCatalogs(), call_options)

    def get_primary_keys(self, table: TableRef, call_options: Optional[FlightSQLCallOptions] = None):
        """
        Requests a list of primary keys and returns a `flight.FlightInfo`
        object to specify the retrieval location for Arrow data.
        """
        cmd = flightsql.CommandGetPrimaryKeys(catalog=table.catalog, db_schema=table.db_schema, table=table.table)
        return self._get_flight_info(cmd, call_options)

    def get_exported_keys(self, table: TableRef, call_options: Optional[FlightSQLCallOptions] = None):
        """
        Requests a description of foreign keys that reference primary keys in
        the given table. Returns a `flight.FlightInfo` object to specify
        the retrieval location for Arrow data.
        """
        cmd = flightsql.CommandGetExportedKeys(catalog=table.catalog, db_schema=table.db_schema, table=table.table)
        return self._get_flight_info(cmd, call_options)

    def get_imported_keys(self, table: TableRef, call_options: Optional[FlightSQLCallOptions] = None):
        """
        Requests a description of foreign keys for the given table. Returns a
        `flight.FlightInfo` object to specify the retrieval location for Arrow data.
        """
        cmd = flightsql.CommandGetImportedKeys(catalog=table.catalog, db_schema=table.db_schema, table=table.table)
        return self._get_flight_info(cmd, call_options)

    def get_cross_reference(
        self, pk_table: TableRef, fk_table: TableRef, call_options: Optional[FlightSQLCallOptions] = None
    ):
        """
        Requests a description of foreign keys in the `fk_table` that reference
        the primary keys in the `pk_table`. Returns a `flight.FlightInfo`
        object to specify the retrieval location for Arrow data.
        """
        cmd = flightsql.CommandGetCrossReference(
            fk_catalog=fk_table.catalog,
            fk_db_schema=fk_table.db_schema,
            fk_table=fk_table.table,
            pk_catalog=pk_table.catalog,
            pk_db_schema=pk_table.db_schema,
            pk_table=pk_table.table,
        )
        return self._get_flight_info(cmd, call_options)

    def get_xdbc_type_info(self, data_type: Optional[int], call_options: Optional[FlightSQLCallOptions] = None):
        """
        Requests information about all data types supported or a specific data
        type. Returns a `flight.FlightInfo` object to specify the
        retrieval location for Arrow data.
        """
        return self._get_flight_info(flightsql.CommandGetXdbcTypeInfo(data_type=data_type), call_options)

    def prepare(self, query: str, call_options: Optional[FlightSQLCallOptions] = None) -> PreparedStatement:
        """
        Create a `PreparedStatement` for a given query.

        The resulting `PreparedStatement` should be closed when it is no longer
        needed. This can be done via an explicity `close()` or by using the
        `with` statement.
        """
        request = flight_action("CreatePreparedStatement", flightsql.ActionCreatePreparedStatementRequest(query=query))
        options = self._flight_call_options(call_options)
        stream = self.client.do_action(request, options)

        flight_results = [r for r in stream]

        result_wrap = any_pb2.Any()
        result_wrap.ParseFromString(flight_results[0].body.to_pybytes())
        result = flightsql.ActionCreatePreparedStatementResult()
        result_wrap.Unpack(result)

        stmt = PreparedStatement(self.client, options, result.prepared_statement_handle)
        stmt.dataset_schema = _read_ipc_schema(result.dataset_schema)
        stmt.parameter_schema = _read_ipc_schema(result.parameter_schema)
        return stmt

    def get_table_types(self, call_options: Optional[FlightSQLCallOptions] = None):
        """
        Requests a list of table types and returns a `flight.FlightInfo`
        object to specify the retrieval location for Arrow data.
        """
        return self._get_flight_info(flightsql.CommandGetTableTypes(), call_options)

    def get_sql_info(self, info: List[int], call_options: Optional[FlightSQLCallOptions] = None):
        """
        Requests a list of information about the upstream server's capabilities
        and returns a `flight.FlightInfo` object to specify the retrieval
        location for Arrow data.
        """
        return self._get_flight_info(flightsql.CommandGetSqlInfo(info=info), call_options)

    @check_closed
    def do_get(self, ticket, call_options: Optional[FlightSQLCallOptions] = None):
        """
        Uses a Flight ticket to request a stream of Arrow data. A
        `flight.FlightStreamReader` is returned to stream the results.
        """
        options = self._flight_call_options(call_options)
        return self.client.do_get(ticket, options)

    @check_closed
    def _do_put(self, desc, call_options: Optional[FlightSQLCallOptions] = None):
        options = self._flight_call_options(call_options)
        return self.client.do_put(desc, pa.Table.from_arrays([]).schema, options)

    @check_closed
    def _get_flight_info(self, command, call_options: Optional[FlightSQLCallOptions] = None):
        options = self._flight_call_options(call_options)
        return self.client.get_flight_info(flight_descriptor(command), options)

    def _flight_call_options(self, call_options: Optional[FlightSQLCallOptions] = None):
        headers = self.headers
        timeout = None
        read_options = None
        write_options = None

        if call_options is not None:
            timeout = call_options.timeout
            read_options = call_options.read_options
            write_options = call_options.write_options

            if call_options.headers is not None:
                headers = headers + call_options.headers

            # Deduplicate entries (tuples) based on their first field. Given two
            # duplicate entries, the last one will win.
            headers = list(OrderedDict(headers).items())

        return flight.FlightCallOptions(
            timeout=timeout, headers=headers, read_options=read_options, write_options=write_options
        )


def create_flight_client(
    host: str = "localhost",
    port: int = 443,
    user: Optional[str] = None,
    password: Optional[str] = None,
    token: Optional[str] = None,
    insecure: Optional[bool] = None,
    disable_server_verification: Optional[bool] = None,
    metadata: Optional[Dict[str, str]] = None,
    **flight_client_kwargs: Any,
) -> Tuple[flight.FlightClient, List[Tuple[bytes, bytes]]]:
    for name, value in (("insecure", insecure), ("disable_server_verification", disable_server_verification)):
        if value is not None and not isinstance(value, bool):
            raise TypeError(f"{name} must be a bool or None, not {type(value).__name__}")
    if insecure and disable_server_verification:
        raise ValueError("insecure and disable_server_verification cannot both be true")

    protocol = "tls"
    if insecure:
        protocol = "tcp"
    elif disable_server_verification:
        flight_client_kwargs["disable_server_verification"] = True

    url = f"grpc+{protocol}://{host}:{port}"
    client = flight.FlightClient(url, **flight_client_kwargs)

    headers = []
    if user or password:
        headers.append(client.authenticate_basic_token(user, password))
    elif token:
        headers.append((b"authorization", f"Bearer {token}".encode("utf-8")))

    for k, v in (metadata or {}).items():
        headers.append((k.encode("utf-8"), v.encode("utf-8")))

    return client, headers


def _protobuf_fields(payload: bytes) -> Optional[Dict[int, bytes]]:
    """Decode the length-delimited fields of a protobuf message, or None."""

    def varint(pos: int) -> Tuple[int, int]:
        value = shift = 0
        while True:
            if pos >= len(payload) or shift > 63:
                raise ValueError("truncated varint")
            byte = payload[pos]
            pos += 1
            value |= (byte & 0x7F) << shift
            shift += 7
            if not byte & 0x80:
                return value, pos

    fields: Dict[int, bytes] = {}
    pos = 0
    try:
        while pos < len(payload):
            key, pos = varint(pos)
            number, wire_type = key >> 3, key & 0x07
            if wire_type == 0:
                _, pos = varint(pos)
            elif wire_type == 2:
                length, pos = varint(pos)
                if pos + length > len(payload):
                    return None
                fields[number] = payload[pos : pos + length]
                pos += length
            elif wire_type == 1:
                pos += 8
            elif wire_type == 5:
                pos += 4
            else:
                return None
    except ValueError:
        return None
    return fields


_DO_PUT_PREPARED_RESULT_URL = "type.googleapis.com/arrow.flight.protocol.sql.DoPutPreparedStatementResult"


def _updated_prepared_handle(payload: bytes) -> Optional[bytes]:
    """Return DoPutPreparedStatementResult.prepared_statement_handle, if any.

    The message (field 1, optional bytes) postdates the bundled protobuf
    module, so it is decoded directly; an ``Any`` wrapper is also accepted.
    """
    if not payload:
        return None
    fields = _protobuf_fields(payload)
    if fields and fields.get(1) == _DO_PUT_PREPARED_RESULT_URL.encode() and 2 in fields:
        fields = _protobuf_fields(fields[2])
    if not fields:
        return None
    return fields.get(1) or None


def _read_ipc_schema(serialized: bytes) -> Optional[pa.Schema]:
    """Decode an optional IPC-serialized schema from a prepared statement result."""
    if not serialized:
        return None
    try:
        return pa.ipc.read_schema(pa.py_buffer(serialized))
    except (pa.ArrowInvalid, OSError):
        return None


def flight_descriptor(command: Any) -> flight.FlightDescriptor:
    any = any_pb2.Any()
    any.Pack(command)
    return flight.FlightDescriptor.for_command(any.SerializeToString())


def flight_action(action_type: str, body: Any) -> flight.Action:
    any = any_pb2.Any()
    any.Pack(body)
    return flight.Action(action_type, any.SerializeToString())
