from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pyarrow as pa
import pyarrow.ipc as ipc
import pytest
from sqlalchemy.sql import sqltypes

from flightsql.dbapi import (
    Connection,
    ParameterRecordBuilder,
    TableMetadataResult,
    dbapi_results,
    resolve_sql_type,
)
from flightsql.exceptions import Error, NotSupportedError


def test_parameter_record_builder():
    params = [20, "hello", 3.14, b"data", True]
    builder = ParameterRecordBuilder(params)
    record = builder.build_record()

    assert record.num_rows == 1
    assert record.num_columns == 5
    assert record.column(0).to_pylist() == [20]
    assert record.column(1).to_pylist() == ["hello"]
    assert record.column(2).to_pylist() == [3.14]
    assert record.column(3).to_pylist() == [b"data"]
    assert record.column(4).to_pylist() == [True]


def test_parameter_record_builder_unsupported_type():
    class Something:
        pass

    params = [Something()]
    builder = ParameterRecordBuilder(params)
    with pytest.raises(Error) as err:
        builder.build_record()
    assert str(err.value) == 'unable to map "Something" type to PyArrow datatype'
    assert err.type == Error


def test_dbapi_results():
    days = pa.array([1, 12, 17, 23, 28], type=pa.int8())
    months = pa.array([1, 3, 5, 7, 1], type=pa.int8())
    years = pa.array([1990, 2000, 1995, 2000, 1995], type=pa.int16())
    names = pa.array(["john", "jim", "jack", "jake", "jerry"], type=pa.string())
    table = pa.table([days, months, years, names], names=["days", "months", "years", "names"])
    values, descriptions = dbapi_results(table)

    assert values == [
        [1, 1, 1990, "john"],
        [12, 3, 2000, "jim"],
        [17, 5, 1995, "jack"],
        [23, 7, 2000, "jake"],
        [28, 1, 1995, "jerry"],
    ]
    assert descriptions == [
        ("days", sqltypes.INTEGER),
        ("months", sqltypes.INTEGER),
        ("years", sqltypes.INTEGER),
        ("names", sqltypes.TEXT),
    ]


def test_dbapi_results_preserves_null_values():
    table = pa.table(
        {
            "integer": pa.array([1, None], type=pa.int64()),
            "text": pa.array(["one", None], type=pa.string()),
        }
    )

    values, _ = dbapi_results(table)

    assert values == [[1, "one"], [None, None]]


def test_dbapi_results_preserves_duplicate_column_names_by_position():
    table = pa.Table.from_arrays(
        [pa.array([1, 2]), pa.array(["one", "two"]), pa.array([9, 8])],
        names=["id", "label", "id"],
    )

    values, descriptions = dbapi_results(table)

    assert values == [[1, "one", 9], [2, "two", 8]]
    assert [description[0] for description in descriptions] == ["id", "label", "id"]
    assert all(len(row) == len(descriptions) for row in values)


@pytest.mark.parametrize(
    ("unit", "raw_value", "expected"),
    [
        ("s", 1, datetime(1970, 1, 1, 0, 0, 1)),
        ("ms", 1_234, datetime(1970, 1, 1, 0, 0, 1, 234_000)),
        ("us", 1_234_567, datetime(1970, 1, 1, 0, 0, 1, 234_567)),
        # The final 890 nanoseconds are deliberately truncated.
        ("ns", 1_234_567_890, datetime(1970, 1, 1, 0, 0, 1, 234_567)),
    ],
)
def test_dbapi_results_returns_stdlib_timestamps_at_microsecond_precision(unit, raw_value, expected):
    table = pa.table({"value": pa.array([raw_value, None], type=pa.timestamp(unit))})

    values, _ = dbapi_results(table)

    assert values == [[expected], [None]]
    assert type(values[0][0]) is datetime


def test_dbapi_results_preserves_timestamp_timezones_while_truncating_nanoseconds():
    table = pa.table({"value": pa.array([1_234_567_890, None], type=pa.timestamp("ns", tz="UTC"))})

    values, _ = dbapi_results(table)

    assert values == [[datetime(1970, 1, 1, 0, 0, 1, 234_567, tzinfo=timezone.utc)], [None]]


def test_dbapi_results_truncates_negative_timestamp_counts_toward_the_epoch():
    table = pa.table({"value": pa.array([-1_234_567_890, -999], type=pa.timestamp("ns"))})

    values, _ = dbapi_results(table)

    assert values == [
        [datetime(1969, 12, 31, 23, 59, 58, 765_433)],
        [datetime(1970, 1, 1)],
    ]


@pytest.mark.parametrize("arrow_type", [pa.date32(), pa.date64()])
def test_dbapi_results_returns_stdlib_dates(arrow_type):
    expected = date(2026, 8, 26)
    table = pa.table({"value": pa.array([expected, None], type=arrow_type)})

    values, _ = dbapi_results(table)

    assert values == [[expected], [None]]
    assert type(values[0][0]) is date


@pytest.mark.parametrize(
    ("arrow_type", "raw_value", "expected"),
    [
        (pa.time32("s"), 1, time(0, 0, 1)),
        (pa.time32("ms"), 1_234, time(0, 0, 1, 234_000)),
        (pa.time64("us"), 1_234_567, time(0, 0, 1, 234_567)),
        # The final 890 nanoseconds are deliberately truncated.
        (pa.time64("ns"), 1_234_567_890, time(0, 0, 1, 234_567)),
    ],
)
def test_dbapi_results_returns_stdlib_times_at_microsecond_precision(arrow_type, raw_value, expected):
    table = pa.table({"value": pa.array([raw_value, None], type=arrow_type)})

    values, _ = dbapi_results(table)

    assert values == [[expected], [None]]
    assert type(values[0][0]) is time


@pytest.mark.parametrize(
    ("unit", "raw_value", "expected"),
    [
        ("s", 1, timedelta(seconds=1)),
        ("ms", 1_234, timedelta(seconds=1, milliseconds=234)),
        ("us", 1_234_567, timedelta(seconds=1, microseconds=234_567)),
        # The final 890 nanoseconds are deliberately truncated.
        ("ns", 1_234_567_890, timedelta(seconds=1, microseconds=234_567)),
    ],
)
def test_dbapi_results_returns_stdlib_durations_at_microsecond_precision(unit, raw_value, expected):
    table = pa.table({"value": pa.array([raw_value, None], type=pa.duration(unit))})

    values, descriptions = dbapi_results(table)

    assert values == [[expected], [None]]
    assert type(values[0][0]) is timedelta
    assert descriptions == [("value", sqltypes.Interval)]


def test_dbapi_results_returns_decimal_values_without_float_conversion():
    expected = Decimal("1234.5678")
    table = pa.table({"value": pa.array([expected, None], type=pa.decimal128(12, 4))})

    values, _ = dbapi_results(table)

    assert values == [[expected], [None]]
    assert type(values[0][0]) is Decimal


def test_dbapi_results_recursively_normalizes_nested_temporals_in_list_variants():
    list_base = pa.array(
        [[0], [1_234_567_890, None], None, [-1_234_567_890]],
        type=pa.list_(pa.timestamp("ns", tz="UTC")),
    )
    list_column = pa.chunked_array([list_base.slice(1, 2), list_base.slice(3, 1)])
    large_list_column = pa.chunked_array(
        [
            pa.array(
                [[1_234_567_890, None], None],
                type=pa.large_list(pa.time64("ns")),
            )
        ]
    )
    fixed_list_column = pa.chunked_array(
        [
            pa.array(
                [[1_234_567_890, -1_234_567_890], None],
                type=pa.list_(pa.duration("ns"), 2),
            )
        ]
    )

    list_values, _ = dbapi_results(pa.Table.from_arrays([list_column], names=["value"]))
    large_list_values, _ = dbapi_results(pa.Table.from_arrays([large_list_column], names=["value"]))
    fixed_list_values, _ = dbapi_results(pa.Table.from_arrays([fixed_list_column], names=["value"]))

    assert list_values == [
        [[datetime(1970, 1, 1, 0, 0, 1, 234_567, tzinfo=timezone.utc), None]],
        [None],
        [[datetime(1969, 12, 31, 23, 59, 58, 765_433, tzinfo=timezone.utc)]],
    ]
    assert large_list_values == [[[time(0, 0, 1, 234_567), None]], [None]]
    assert fixed_list_values == [
        [[timedelta(seconds=1, microseconds=234_567), timedelta(seconds=-1, microseconds=-234_567)]],
        [None],
    ]
    assert type(list_values[0][0][0]) is datetime
    assert type(large_list_values[0][0][0]) is time
    assert type(fixed_list_values[0][0][0]) is timedelta


def test_dbapi_results_recursively_normalizes_struct_map_and_deeply_nested_values():
    nested_type = pa.list_(
        pa.struct(
            [
                pa.field("timestamp", pa.timestamp("ns", tz="UTC")),
                pa.field("time", pa.time64("ns")),
                pa.field("durations", pa.map_(pa.string(), pa.list_(pa.duration("ns")))),
            ]
        )
    )
    column = pa.chunked_array(
        [
            pa.array(
                [
                    [
                        {
                            "timestamp": -1_234_567_890,
                            "time": 1_234_567_890,
                            "durations": [("latency", [1_234_567_890, None, -1_234_567_890])],
                        }
                    ],
                    None,
                    [None],
                ],
                type=nested_type,
            )
        ]
    )

    values, _ = dbapi_results(pa.Table.from_arrays([column], names=["nested"]))

    assert values == [
        [
            [
                {
                    "timestamp": datetime(1969, 12, 31, 23, 59, 58, 765_433, tzinfo=timezone.utc),
                    "time": time(0, 0, 1, 234_567),
                    "durations": [
                        (
                            "latency",
                            [
                                timedelta(seconds=1, microseconds=234_567),
                                None,
                                timedelta(seconds=-1, microseconds=-234_567),
                            ],
                        )
                    ],
                }
            ]
        ],
        [None],
        [[None]],
    ]
    nested = values[0][0][0]
    assert type(nested["timestamp"]) is datetime
    assert type(nested["time"]) is time
    assert type(nested["durations"][0][1][0]) is timedelta


def test_dbapi_results_recursively_normalizes_dictionary_encoded_temporals_across_chunks():
    first = pa.DictionaryArray.from_arrays(
        pa.array([0, None, 1], type=pa.int8()),
        pa.array([1_234_567_890, -1_234_567_890], type=pa.timestamp("ns", tz="UTC")),
    )
    second = pa.DictionaryArray.from_arrays(
        pa.array([0], type=pa.int8()),
        pa.array([-999], type=pa.timestamp("ns", tz="UTC")),
    )
    column = pa.chunked_array([first, second], type=first.type)

    values, _ = dbapi_results(pa.Table.from_arrays([column], names=["encoded"]))

    assert values == [
        [datetime(1970, 1, 1, 0, 0, 1, 234_567, tzinfo=timezone.utc)],
        [None],
        [datetime(1969, 12, 31, 23, 59, 58, 765_433, tzinfo=timezone.utc)],
        [datetime(1970, 1, 1, tzinfo=timezone.utc)],
    ]
    assert all(type(row[0]) is datetime for row in (values[0], values[2], values[3]))


def test_dbapi_results_recursively_normalizes_dense_and_sparse_unions():
    dense = pa.UnionArray.from_dense(
        pa.array([5, 5, 7, 5, 7], type=pa.int8()),
        pa.array([0, 1, 0, 2, 1], type=pa.int32()),
        [
            pa.array([0, 1_234_567_890, -1_234_567_890], type=pa.timestamp("ns", tz="UTC")),
            pa.array([1_234_567_890, None], type=pa.duration("ns")),
        ],
        field_names=["timestamp", "duration"],
        type_codes=[5, 7],
    ).slice(1, 4)
    sparse = pa.UnionArray.from_sparse(
        pa.array([2, 2, 4, 2], type=pa.int8()),
        [
            pa.array([0, 1_234_567_890, None, -1_234_567_890], type=pa.time64("ns")),
            pa.array([None, None, 1_234_567_890, None], type=pa.duration("ns")),
        ],
        field_names=["time", "duration"],
        type_codes=[2, 4],
    ).slice(1, 3)

    dense_values, _ = dbapi_results(pa.table({"value": dense}))
    sparse_values, _ = dbapi_results(pa.table({"value": sparse}))

    assert dense_values == [
        [datetime(1970, 1, 1, 0, 0, 1, 234_567, tzinfo=timezone.utc)],
        [timedelta(seconds=1, microseconds=234_567)],
        [datetime(1969, 12, 31, 23, 59, 58, 765_433, tzinfo=timezone.utc)],
        [None],
    ]
    assert sparse_values == [
        [time(0, 0, 1, 234_567)],
        [timedelta(seconds=1, microseconds=234_567)],
        [time(23, 59, 58, 765_433)],
    ]
    assert type(dense_values[0][0]) is datetime
    assert type(dense_values[1][0]) is timedelta
    assert type(sparse_values[0][0]) is time


@pytest.mark.parametrize("mode", ["dense", "sparse"])
@pytest.mark.parametrize("empty_position", ["leading", "trailing"])
def test_dbapi_results_accepts_empty_union_chunks_with_absent_buffers_without_dropping_rows(mode, empty_position):
    fields = [
        pa.field("timestamp", pa.timestamp("ns", tz="UTC"), nullable=False, metadata={b"source": b"rpc"}),
        pa.field("duration", pa.duration("ns"), nullable=True),
    ]
    union_type = pa.union(fields, mode=mode, type_codes=[5, 7])
    empty_buffers = [None, None, None] if mode == "dense" else [None, None]
    empty = pa.Array.from_buffers(
        union_type,
        0,
        empty_buffers,
        children=[pa.array([], type=field.type) for field in fields],
    )
    if mode == "dense":
        populated = pa.Array.from_buffers(
            union_type,
            1,
            [None, pa.array([5], type=pa.int8()).buffers()[1], pa.array([0], type=pa.int32()).buffers()[1]],
            children=[
                pa.array([1_234_567_890], type=fields[0].type),
                pa.array([], type=fields[1].type),
            ],
        )
    else:
        populated = pa.Array.from_buffers(
            union_type,
            1,
            [None, pa.array([5], type=pa.int8()).buffers()[1]],
            children=[
                pa.array([1_234_567_890], type=fields[0].type),
                pa.array([None], type=fields[1].type),
            ],
        )

    batches = [empty, populated] if empty_position == "leading" else [populated, empty]
    table = pa.Table.from_batches([pa.record_batch([batch], names=["value"]) for batch in batches])
    values, _ = dbapi_results(table)

    assert values == [[datetime(1970, 1, 1, 0, 0, 1, 234_567, tzinfo=timezone.utc)]]


def test_dbapi_results_explicitly_rejects_unsupported_nested_temporal_container():
    encoded = pa.RunEndEncodedArray.from_arrays(
        [2],
        pa.array([1_234_567_890], type=pa.timestamp("ns")),
    )

    with pytest.raises(NotSupportedError, match="unsupported Arrow type run_end_encoded"):
        dbapi_results(pa.table({"value": encoded}))


def test_get_columns_returns_empty_result_for_unknown_table():
    table = pa.table({"table_schema": pa.array([], type=pa.binary())})

    class Reader:
        def read_all(self):
            return table

    class Client:
        def get_tables(self, **kwargs):
            return SimpleNamespace(endpoints=[SimpleNamespace(ticket=b"ticket")])

        def do_get(self, ticket):
            assert ticket == b"ticket"
            return Reader()

    connection = Connection(Client())

    assert connection.flightsql_get_columns("missing") == []


def _serialized_schema(schema):
    sink = pa.BufferOutputStream()
    with ipc.new_stream(sink, schema):
        pass
    return sink.getvalue().to_pybytes()


def test_get_table_metadata_uses_one_included_schema_request_and_filters_unreflectable_rows():
    table = pa.table(
        {
            "table_name": pa.array(["alpha", "stale", "malformed"]),
            "table_schema": pa.array(
                [
                    _serialized_schema(pa.schema([pa.field("id", pa.int64(), nullable=False)])),
                    None,
                    b"not an Arrow IPC stream",
                ],
                type=pa.binary(),
            ),
        }
    )

    class Reader:
        def read_all(self):
            return table

    class Client:
        def __init__(self):
            self.calls = []

        def get_tables(self, **kwargs):
            self.calls.append(("get_tables", kwargs))
            return SimpleNamespace(endpoints=[SimpleNamespace(ticket=b"ticket")])

        def do_get(self, ticket):
            self.calls.append(("do_get", ticket))
            return Reader()

    client = Client()
    connection = Connection(client)

    assert connection.flightsql_get_table_metadata("public") == TableMetadataResult(
        ["alpha", "stale", "malformed"],
        {
            "alpha": [
                {
                    "name": "id",
                    "type": sqltypes.BIGINT,
                    "default": None,
                    "comment": None,
                    "nullable": False,
                }
            ]
        },
        True,
    )
    assert client.calls == [
        ("get_tables", {"db_schema_filter_pattern": "public", "include_schema": True}),
        ("do_get", b"ticket"),
    ]


def test_get_table_metadata_distinguishes_names_only_response_from_empty_catalog():
    names_only = pa.table({"table_name": ["alpha", "beta"]})
    empty = pa.table({"table_name": pa.array([], type=pa.string())})

    class Reader:
        def __init__(self, table):
            self.table = table

        def read_all(self):
            return self.table

    class Client:
        def __init__(self, included_schema_table, names_table=None):
            self.included_schema_table = included_schema_table
            self.names_table = names_table if names_table is not None else included_schema_table
            self.requested_include_schema = True

        def get_tables(self, **kwargs):
            self.requested_include_schema = kwargs.get("include_schema", False)
            return SimpleNamespace(endpoints=[SimpleNamespace(ticket=b"ticket")])

        def do_get(self, ticket):
            assert ticket == b"ticket"
            table = self.included_schema_table if self.requested_include_schema else self.names_table
            return Reader(table)

    assert Connection(Client(names_only)).flightsql_get_table_metadata("public") == TableMetadataResult(
        ["alpha", "beta"], None, False
    )
    assert Connection(Client(empty, names_only)).flightsql_get_table_metadata("public") == TableMetadataResult(
        ["alpha", "beta"], None, False
    )
    assert Connection(Client(empty)).flightsql_get_table_metadata("public") == TableMetadataResult([], {}, True)


def test_get_table_metadata_does_not_mask_transport_errors():
    class Client:
        def get_tables(self, **kwargs):
            return SimpleNamespace(endpoints=[SimpleNamespace(ticket=b"ticket")])

        def do_get(self, ticket):
            raise RuntimeError("transport unavailable")

    with pytest.raises(RuntimeError, match="transport unavailable"):
        Connection(Client()).flightsql_get_table_metadata()


def test_empty_included_schema_response_does_not_mask_names_rpc_errors():
    class Reader:
        def read_all(self):
            return pa.table({"table_name": pa.array([], type=pa.string())})

    class Client:
        def get_tables(self, **kwargs):
            if not kwargs.get("include_schema", False):
                raise RuntimeError("names transport unavailable")
            return SimpleNamespace(endpoints=[SimpleNamespace(ticket=b"ticket")])

        def do_get(self, ticket):
            return Reader()

    with pytest.raises(RuntimeError, match="names transport unavailable"):
        Connection(Client()).flightsql_get_table_metadata()


def test_resolve_sql_type():
    cases = [
        (pa.timestamp("ns"), sqltypes.TIMESTAMP),
        (pa.time64("ns"), sqltypes.TIME),
        (pa.date64(), sqltypes.DATE),
        (pa.duration("ns"), sqltypes.Interval),
        (pa.decimal128(10, 5), sqltypes.DECIMAL),
        (pa.string(), sqltypes.TEXT),
        (pa.utf8(), sqltypes.TEXT),
        (pa.float32(), sqltypes.FLOAT),
        (pa.float64(), sqltypes.FLOAT),
        (pa.int8(), sqltypes.INTEGER),
        (pa.int16(), sqltypes.INTEGER),
        (pa.int32(), sqltypes.INTEGER),
        (pa.int64(), sqltypes.BIGINT),
        (pa.uint8(), sqltypes.INTEGER),
        (pa.uint16(), sqltypes.INTEGER),
        (pa.uint32(), sqltypes.INTEGER),
        (pa.uint64(), sqltypes.BIGINT),
        (pa.bool_(), sqltypes.BOOLEAN),
        (pa.binary(), sqltypes.BINARY),
    ]
    for actual, expected in cases:
        assert resolve_sql_type(actual) == expected
