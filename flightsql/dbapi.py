from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple, Union

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.ipc as ipc
from sqlalchemy import types

from flightsql.client import FlightSQLClient, TableRef
from flightsql.exceptions import DataError, Error, NotSupportedError
from flightsql.util import check_closed

paramstyle = "qmark"
apilevel = "2.0"

ExecuteParams = Union[Tuple[Any, ...], List[Any]]


def check_result(f):
    def g(self, *args, **kwargs):
        if self._results is None:
            raise Error("called before execute")
        return f(self, *args, **kwargs)

    return g


class Cursor:
    def __init__(self, client: FlightSQLClient):
        self.client = client
        self.arraysize = 1
        self.closed = False
        self.description: Optional[List[Any]] = None
        self._results: List[Sequence[Any]] = []

    @check_closed
    def __iter__(self) -> Iterator[Sequence[Any]]:
        return iter(self._results)

    def __enter__(self) -> "Cursor":
        return self

    def __exit__(self, *args) -> None:
        self.close()

    @check_closed
    def close(self) -> None:
        self.closed = True

    @check_closed
    def execute(self, query: str, params: Optional[ExecuteParams] = None) -> "Cursor":
        self.description = None
        self._results = []

        if params is None or len(params) == 0:
            info = self.client.execute(query)
            reader = self.client.do_get(info.endpoints[0].ticket)
            self._results, self.description = dbapi_results(reader.read_all())
            return self

        with self.client.prepare(query) as stmt:
            builder = ParameterRecordBuilder(params or ())
            record = builder.build_record()
            info = stmt.execute(record)
            reader = self.client.do_get(info.endpoints[0].ticket)
            self._results, self.description = dbapi_results(reader.read_all())
            return self

    def executemany(self, query: str, param_seq: Sequence[ExecuteParams]) -> "Cursor":
        self.description = None
        self._results = []

        if param_seq is None or len(param_seq) == 0:
            return self.execute(query)

        with self.client.prepare(query) as stmt:
            for params in param_seq:
                builder = ParameterRecordBuilder(params or ())
                record = builder.build_record()
                info = stmt.execute(record)
                self.client.do_get(info.endpoints[0].ticket).read_all()
            return self

    @check_result
    @check_closed
    def fetchone(self) -> Optional[Sequence[Any]]:
        try:
            return self._results.pop(0)
        except IndexError:
            return None

    @check_result
    @check_closed
    def fetchmany(self, size: Optional[int] = None) -> Sequence[Sequence[Any]]:
        size = size or self.arraysize
        out = self._results[:size]
        self._results = self._results[size:]
        return out

    @check_result
    @check_closed
    def fetchall(self) -> Sequence[Sequence[Any]]:
        out = self._results[:]
        self._results = []
        return out

    @check_closed
    def setinputsizes(self, sizes):
        pass

    @check_closed
    def setoutputsizes(self, sizes):
        pass

    @property
    @check_result
    @check_closed
    def rowcount(self) -> int:
        return len(self._results)


class Connection:
    def __init__(self, client: FlightSQLClient, **kwargs):
        self.client = client
        self.closed = False
        self.cursors: List[Cursor] = []

    def __enter__(self) -> "Connection":
        return self

    def __exit__(self, *args) -> None:
        self.close()

    @check_closed
    def commit(self):
        pass

    @check_closed
    def rollback(self):
        pass

    @check_closed
    def close(self) -> None:
        self.closed = True
        for cursor in self.cursors:
            if not cursor.closed:
                cursor.close()

    @check_closed
    def cursor(self) -> Cursor:
        cursor = Cursor(self.client)
        self.cursors.append(cursor)
        return cursor

    @check_closed
    def execute(self, *args, **kwargs) -> Cursor:
        cursor = self.cursor()
        return cursor.execute(*args, **kwargs)

    @check_closed
    def executemany(self, *args, **kwargs) -> Cursor:
        cursor = self.cursor()
        return cursor.executemany(*args, **kwargs)

    @check_closed
    def flightsql_get_columns(self, table_name: str, schema: Optional[str] = None) -> List[Dict]:
        """Get the columns of a table using Flight SQL."""
        info = self.client.get_tables(
            table_name_filter_pattern=table_name, db_schema_filter_pattern=schema, include_schema=True
        )
        metadata = self._table_metadata_from_info(info)
        if metadata is None:
            return []
        return metadata.get(table_name, [])

    @check_closed
    def flightsql_get_table_metadata(self, schema: Optional[str] = None) -> Optional[Dict[str, List[Dict]]]:
        """Get all reflectable table names and included schemas in one request.

        ``None`` means the server did not provide bulk included-schema rows and
        the dialect must use its compatibility fallback. An empty dictionary
        means the server provided the response shape and no table was
        reflectable. Flight transport errors deliberately propagate.
        """
        info = self.client.get_tables(db_schema_filter_pattern=schema, include_schema=True)
        return self._table_metadata_from_info(info)

    @check_closed
    def flightsql_get_table_names(self, schema: Optional[str] = None) -> List[str]:
        """Get the names of all tables within the schema."""
        info = self.client.get_tables(db_schema_filter_pattern=schema)
        names: List[str] = []
        for table in self._tables_from_info(info):
            if "table_name" in table.column_names:
                names.extend(table.column("table_name").to_pylist())
        return names

    @check_closed
    def flightsql_get_schema_names(self) -> List[str]:
        """Get the names of all schemas."""
        info = self.client.get_db_schemas()
        names: List[str] = []
        for table in self._tables_from_info(info):
            if "db_schema_name" in table.column_names:
                names.extend(table.column("db_schema_name").to_pylist())
        return names

    @check_closed
    def flightsql_get_sql_info(self, info: List[int]) -> Dict[int, Any]:
        """Get metadata about the server and its SQL features."""
        finfo = self.client.get_sql_info(info)
        reader = self.client.do_get(finfo.endpoints[0].ticket)
        values = reader.read_all().to_pylist()
        return {v["info_name"]: v["value"] for v in values}

    @check_closed
    def flightsql_get_primary_keys(self, table: str, schema: Optional[str] = None) -> List[Dict[str, Any]]:
        ref = TableRef(table=table, db_schema=schema)
        info = self.client.get_primary_keys(ref)
        reader = self.client.do_get(info.endpoints[0].ticket)
        return reader.read_all().to_pylist()

    @check_closed
    def flightsql_get_foreign_keys(self, table: str, schema: Optional[str] = None) -> List[Dict[str, Any]]:
        ref = TableRef(table=table, db_schema=schema)
        info = self.client.get_imported_keys(ref)
        reader = self.client.do_get(info.endpoints[0].ticket)
        return reader.read_all().to_pylist()

    def _tables_from_info(self, info: Any) -> List[pa.Table]:
        return [self.client.do_get(endpoint.ticket).read_all() for endpoint in info.endpoints]

    def _table_metadata_from_info(self, info: Any) -> Optional[Dict[str, List[Dict]]]:
        tables = self._tables_from_info(info)
        if not tables:
            return None
        if any("table_schema" not in table.column_names for table in tables):
            return None
        if sum(table.num_rows for table in tables) == 0:
            return None

        metadata: Dict[str, List[Dict]] = {}
        for table in tables:
            names = table.column("table_name").to_pylist()
            schemas = table.column("table_schema").to_pylist()
            for name, serialized_schema in zip(names, schemas):
                if not isinstance(name, str) or serialized_schema is None:
                    continue
                try:
                    schema = ipc.open_stream(serialized_schema).schema
                except (OSError, pa.ArrowInvalid, pa.ArrowTypeError):
                    # The metadata row itself is stale or malformed. Only
                    # parsing happens inside this catch; get_tables/do_get and
                    # read_all transport failures continue to propagate.
                    continue
                metadata[name] = column_specs(schema)
        return metadata

    @property
    def features(self) -> Dict[str, str]:
        return self.client.features


def connect(client: FlightSQLClient, **kwargs) -> Connection:
    """Connects to a Flight SQL server."""
    return Connection(client, **kwargs)


def column_specs(schema: pa.Schema) -> List[Dict]:
    cols = []
    for i in range(0, len(schema)):
        field = schema.field(i)
        cols.append(
            {
                "name": field.name,
                "type": resolve_sql_type(field.type),
                "default": None,
                "comment": None,
                "nullable": field.nullable,
            }
        )
    return cols


def dbapi_results(table: pa.Table) -> Tuple[List, List]:
    """
    Convert an Arrow table into DB API values and column descriptions.

    Columns are read by index rather than name because Arrow permits duplicate
    field names. Values are converted directly from Arrow so SQL NULL values
    remain ``None`` and using the DB API does not require pandas.

    Python's standard temporal values have microsecond precision. Nanosecond
    timestamps, times, and durations are therefore truncated to microseconds
    before conversion. This makes the returned types and precision independent
    of whether the optional pandas package happens to be installed.
    """
    descriptions = arrow_column_descriptions(table.schema)
    columns = [_dbapi_column_values(table.column(index)) for index in range(table.num_columns)]
    return [list(row) for row in zip(*columns)], descriptions


def _dbapi_column_values(column: pa.ChunkedArray) -> List[Any]:
    """Convert one Arrow column to stable, pandas-independent DB API values."""
    normalized = _normalize_temporal_chunked_array(column)
    try:
        return normalized.to_pylist()
    except (OverflowError, ValueError, pa.ArrowException) as error:
        raise DataError(f"unable to convert Arrow column of type {normalized.type} to DB API values") from error


def _microsecond_temporal_type(arrow_type: pa.DataType) -> Optional[pa.DataType]:
    if pa.types.is_timestamp(arrow_type) and arrow_type.unit == "ns":
        return pa.timestamp("us", tz=arrow_type.tz)
    if pa.types.is_time64(arrow_type) and arrow_type.unit == "ns":
        return pa.time64("us")
    if pa.types.is_duration(arrow_type) and arrow_type.unit == "ns":
        return pa.duration("us")
    return None


def _contains_nanosecond_temporal(arrow_type: pa.DataType) -> bool:
    if _microsecond_temporal_type(arrow_type) is not None:
        return True
    if isinstance(arrow_type, pa.BaseExtensionType):
        return _contains_nanosecond_temporal(arrow_type.storage_type)
    if pa.types.is_dictionary(arrow_type):
        return _contains_nanosecond_temporal(arrow_type.value_type)
    return any(_contains_nanosecond_temporal(arrow_type.field(index).type) for index in range(arrow_type.num_fields))


def _normalize_temporal_field(field: pa.Field, path: str) -> pa.Field:
    return pa.field(
        field.name,
        _normalized_temporal_type(field.type, path),
        nullable=field.nullable,
        metadata=field.metadata,
    )


def _normalized_temporal_type(arrow_type: pa.DataType, path: str) -> pa.DataType:
    temporal_type = _microsecond_temporal_type(arrow_type)
    if temporal_type is not None:
        return temporal_type

    if pa.types.is_list(arrow_type):
        return pa.list_(_normalize_temporal_field(arrow_type.value_field, f"{path}[]"))
    if pa.types.is_large_list(arrow_type):
        return pa.large_list(_normalize_temporal_field(arrow_type.value_field, f"{path}[]"))
    if pa.types.is_fixed_size_list(arrow_type):
        return pa.list_(
            _normalize_temporal_field(arrow_type.value_field, f"{path}[]"),
            arrow_type.list_size,
        )
    if pa.types.is_struct(arrow_type):
        return pa.struct([_normalize_temporal_field(field, f"{path}.{field.name}") for field in arrow_type])
    if pa.types.is_map(arrow_type):
        return pa.map_(
            _normalize_temporal_field(arrow_type.key_field, f"{path}.key"),
            _normalize_temporal_field(arrow_type.item_field, f"{path}.value"),
            keys_sorted=arrow_type.keys_sorted,
        )
    if pa.types.is_dictionary(arrow_type):
        return pa.dictionary(
            arrow_type.index_type,
            _normalized_temporal_type(arrow_type.value_type, f"{path}.dictionary"),
            ordered=arrow_type.ordered,
        )
    if pa.types.is_union(arrow_type):
        return pa.union(
            [_normalize_temporal_field(field, f"{path}.{field.name}") for field in arrow_type],
            mode=arrow_type.mode,
            type_codes=arrow_type.type_codes,
        )

    if _contains_nanosecond_temporal(arrow_type):
        raise NotSupportedError(
            f"cannot safely normalize nanosecond temporal values inside unsupported Arrow type "
            f"{arrow_type} at {path}"
        )
    return arrow_type


def _normalize_temporal_chunked_array(column: pa.ChunkedArray) -> pa.ChunkedArray:
    target_type = _normalized_temporal_type(column.type, "column")
    if target_type == column.type:
        return column
    chunks = [_normalize_temporal_array(chunk, "column") for chunk in column.chunks]
    return pa.chunked_array(chunks, type=target_type)


def _normalize_temporal_array(array: pa.Array, path: str) -> pa.Array:
    arrow_type = array.type
    target_type = _normalized_temporal_type(arrow_type, path)
    if target_type == arrow_type:
        return array

    temporal_type = _microsecond_temporal_type(arrow_type)
    if temporal_type is not None:
        # Arrow's unsafe temporal cast divides the signed epoch/unit count by
        # 1,000 with truncation toward zero. Precision loss is intentional:
        # datetime, time, and timedelta represent only microseconds.
        return pc.cast(array, temporal_type, safe=False)

    if pa.types.is_list(arrow_type):
        offsets, values = _normalized_list_storage(array)
        return pa.ListArray.from_arrays(
            offsets,
            _normalize_temporal_array(values, f"{path}[]"),
            type=target_type,
            mask=array.is_null(),
        )
    if pa.types.is_large_list(arrow_type):
        offsets, values = _normalized_list_storage(array)
        return pa.LargeListArray.from_arrays(
            offsets,
            _normalize_temporal_array(values, f"{path}[]"),
            type=target_type,
            mask=array.is_null(),
        )
    if pa.types.is_fixed_size_list(arrow_type):
        values = array.values.slice(array.offset * arrow_type.list_size, len(array) * arrow_type.list_size)
        return pa.FixedSizeListArray.from_arrays(
            _normalize_temporal_array(values, f"{path}[]"),
            type=target_type,
            mask=array.is_null(),
        )
    if pa.types.is_struct(arrow_type):
        children = [
            _normalize_temporal_array(array.field(index), f"{path}.{field.name}")
            for index, field in enumerate(arrow_type)
        ]
        return pa.StructArray.from_arrays(children, fields=list(target_type), mask=array.is_null())
    if pa.types.is_map(arrow_type):
        offsets = array.offsets
        start = offsets[0].as_py()
        stop = offsets[-1].as_py()
        normalized_offsets = pc.subtract(offsets, pa.scalar(start, type=offsets.type))
        keys = _normalize_temporal_array(array.keys.slice(start, stop - start), f"{path}.key")
        items = _normalize_temporal_array(array.items.slice(start, stop - start), f"{path}.value")
        entries = pa.StructArray.from_arrays(
            [keys, items],
            fields=[target_type.key_field, target_type.item_field],
        )
        # MapArray.from_arrays did not accept a parent null mask in PyArrow 16.
        # Construct the standard list-like buffers directly so nested map NULLs
        # are preserved on every supported PyArrow release.
        return pa.MapArray.from_buffers(
            target_type,
            len(array),
            [array.is_valid().buffers()[1], normalized_offsets.buffers()[1]],
            children=[entries],
        )
    if pa.types.is_dictionary(arrow_type):
        normalized = pa.DictionaryArray.from_arrays(
            array.indices,
            _normalize_temporal_array(array.dictionary, f"{path}.dictionary"),
            ordered=arrow_type.ordered,
        )
        if normalized.type != target_type:
            raise NotSupportedError(f"normalizing {arrow_type} at {path} did not preserve its Arrow type")
        return normalized
    if pa.types.is_union(arrow_type):
        return _normalize_temporal_union_array(array, target_type, path)

    raise NotSupportedError(f"cannot safely normalize Arrow type {arrow_type} at {path}")


def _normalized_list_storage(array: pa.Array) -> Tuple[pa.Array, pa.Array]:
    offsets = array.offsets
    start = offsets[0].as_py()
    stop = offsets[-1].as_py()
    normalized_offsets = pc.subtract(offsets, pa.scalar(start, type=offsets.type))
    return normalized_offsets, array.values.slice(start, stop - start)


def _union_buffer_array(array: pa.UnionArray, arrow_type: pa.DataType, buffer_index: int) -> pa.Array:
    buffer = array.buffers()[buffer_index]
    if buffer is None:
        raise NotSupportedError(f"union array of type {array.type} is missing buffer {buffer_index}")
    logical = pa.Array.from_buffers(arrow_type, len(array), [None, buffer], offset=array.offset)
    # Union factories interpret child offsets as part of the new logical
    # array. Materialize this tiny primitive buffer at offset zero so sliced
    # unions do not accidentally include values before the slice.
    return pa.concat_arrays([logical])


def _normalize_temporal_union_array(array: pa.UnionArray, target_type: pa.DataType, path: str) -> pa.UnionArray:
    arrow_type = array.type
    type_ids = _union_buffer_array(array, pa.int8(), 1)
    children = [
        _normalize_temporal_array(array.field(index), f"{path}.{field.name}") for index, field in enumerate(arrow_type)
    ]
    field_names = [field.name for field in arrow_type]

    if arrow_type.mode == "dense":
        offsets = _union_buffer_array(array, pa.int32(), 2)
        normalized = pa.UnionArray.from_dense(
            type_ids,
            offsets,
            children,
            field_names=field_names,
            type_codes=arrow_type.type_codes,
        )
    elif arrow_type.mode == "sparse":
        normalized = pa.UnionArray.from_sparse(
            type_ids,
            [pa.concat_arrays([child]) for child in children],
            field_names=field_names,
            type_codes=arrow_type.type_codes,
        )
    else:
        raise NotSupportedError(f"unsupported Arrow union mode {arrow_type.mode!r} at {path}")

    if normalized.type != target_type:
        raise NotSupportedError(f"normalizing union type {arrow_type} at {path} cannot safely preserve field metadata")
    return normalized


def arrow_column_descriptions(schema: pa.Schema) -> List[Tuple[str, Any]]:
    """Map Arrow schema fields to SQL types."""
    description = []
    for i, t in enumerate(schema.types):
        description.append((schema.names[i], resolve_sql_type(t)))
    return description


def resolve_sql_type(t: pa.DataType):
    """Resolves an Arrow DataType value to a SQL type."""

    if pa.types.is_timestamp(t):
        return types.TIMESTAMP
    if pa.types.is_time(t):
        return types.TIME
    if pa.types.is_date(t):
        return types.DATE
    if pa.types.is_binary(t):
        return types.BINARY
    if pa.types.is_boolean(t):
        return types.BOOLEAN
    if pa.types.is_decimal(t):
        return types.DECIMAL
    if pa.types.is_duration(t):
        return types.Interval
    if pa.types.is_floating(t):
        return types.FLOAT
    if pa.types.is_string(t):
        return types.TEXT

    if pa.types.is_signed_integer(t) and not pa.types.is_int64(t):
        return types.INTEGER
    if pa.types.is_int64(t):
        return types.BIGINT

    # TODO(brett): Find a way to deal with unsigned integers.
    if pa.types.is_unsigned_integer(t) and not pa.types.is_uint64(t):
        return types.INTEGER
    if pa.types.is_uint64(t):
        return types.BIGINT

    # TODO(brett): I'd like to be permissive of unknown types here, but I'm not
    # sure what type we should fall back to.
    return types.BLOB


class ParameterRecordBuilder:
    """
    Builds a PyArrow RecordBatch from a list of parameters for prepared statements.

    Each parameter value is packed into a UnionArray (dense) of length 1. This
    allows the upstream to accept a different type for each parameter in the
    input Tuple. These UnionArray values are then packed into a RecordBatch.
    """

    union_positions = {t: idx for idx, t in enumerate([int, float, str, bytes, bool])}
    arrow_types = {int: pa.int64, float: pa.float64, str: pa.utf8, bytes: pa.binary, bool: pa.bool_}

    def __init__(self, values: ExecuteParams):
        self.values = values

    def union_array_for_value(self, value: Any) -> pa.UnionArray:
        """Builds a PyArrow UnionArray around a value."""
        pytype = type(value)
        if pytype not in self.union_positions:
            raise Error(f'unable to map "{pytype.__name__}" type to PyArrow datatype')

        children = []
        type_id = -1
        for t, idx in self.union_positions.items():
            arrow_type = self.arrow_types[t]()
            if t == pytype:
                type_id = idx
                values = [value]
            else:
                values = []
            children.append(pa.array(values, type=arrow_type))

        return pa.UnionArray.from_dense(pa.array([type_id], type=pa.int8()), pa.array([0], type=pa.int32()), children)

    def build_record(self) -> pa.RecordBatch:
        """
        Builds a PyArrow RecordBatch containing UnionArrays for all parameters.
        """
        if len(self.values) == 0:
            return pa.RecordBatch.from_arrays([], names=[])

        columns = [self.union_array_for_value(v) for v in self.values]
        names = [f"param_{i}" for i in range(len(columns))]
        return pa.RecordBatch.from_arrays(columns, names=names)
