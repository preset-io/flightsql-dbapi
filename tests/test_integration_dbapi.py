import pytest

import flightsql.flightsql_pb2 as flightsql_pb2
from flightsql.dbapi import connect

from . import integration


def new_conn(features={}):
    return connect(integration.new_client(features=features))


@pytest.mark.skipif(integration.is_disabled(), reason=integration.disabled_message)
def test_integration_update():
    client = integration.new_client()
    update_query = "insert into intTable (id, keyName, value, foreignId) values (5, 'five', 5, 5)"
    result = client.execute_update(update_query)
    assert result == 1
    result = client.execute_update("delete from intTable where id = 5")
    assert result == 1


@pytest.mark.skipif(integration.is_disabled(), reason=integration.disabled_message)
def test_integration_query():
    conn = new_conn()
    cursor = conn.cursor()
    cursor.execute("select * from intTable")

    assert [(d[0], repr(d[1])) for d in cursor.description] == [
        ("id", "BIGINT()"),
        ("keyName", "VARCHAR()"),
        ("value", "BIGINT()"),
        ("foreignId", "BIGINT()"),
    ]
    assert all(len(d) == 7 for d in cursor.description)
    rows = [r for r in cursor]
    assert rows == [
        [1, "one", 1, 1],
        [2, "zero", 0, 1],
        [3, "negative one", -1, 1],
        [4, None, None, None],
    ]
    assert all(type(row[2]) is int for row in rows[:3])
    conn.close()


@pytest.mark.skipif(integration.is_disabled(), reason=integration.disabled_message)
def test_integration_query_preserves_duplicate_column_names_and_width():
    with new_conn() as conn:
        cursor = conn.execute("select id, keyName, id from intTable order by id")
        rows = cursor.fetchall()

        assert [column[0] for column in cursor.description or []] == ["id", "keyName", "id"]
        assert rows == [
            [1, "one", 1],
            [2, "zero", 2],
            [3, "negative one", 3],
            [4, None, 4],
        ]
        assert all(len(row) == len(cursor.description or []) for row in rows)


@pytest.mark.skipif(integration.is_disabled(), reason=integration.disabled_message)
def test_integration_get_table_names():
    conn = new_conn()
    names = conn.flightsql_get_table_names(None)
    assert names == ["foreignTable", "intTable", "sqlite_sequence"]
    conn.close()


@pytest.mark.skipif(integration.is_disabled(), reason=integration.disabled_message)
def test_integration_sql_info():
    conn = new_conn()

    # Some servers respond with a full set of information if an empty list is
    # provided. This SQLite reference implementation seems to require specific
    # codes to be requested.
    info = conn.flightsql_get_sql_info(
        [
            flightsql_pb2.FLIGHT_SQL_SERVER_NAME,
            flightsql_pb2.FLIGHT_SQL_SERVER_VERSION,
            flightsql_pb2.FLIGHT_SQL_SERVER_ARROW_VERSION,
            flightsql_pb2.FLIGHT_SQL_SERVER_READ_ONLY,
        ]
    )
    assert info[flightsql_pb2.FLIGHT_SQL_SERVER_NAME] == "db_name"
    assert info[flightsql_pb2.FLIGHT_SQL_SERVER_VERSION] == "sqlite 3"
    assert info[flightsql_pb2.FLIGHT_SQL_SERVER_ARROW_VERSION] == "11.0.0-SNAPSHOT"
    assert info[flightsql_pb2.FLIGHT_SQL_SERVER_READ_ONLY] is False
    conn.close()


@pytest.mark.skipif(integration.is_disabled(), reason=integration.disabled_message)
def test_integration_get_columns():
    conn = new_conn()
    columns = [{**c, "type": repr(c["type"])} for c in conn.flightsql_get_columns("intTable", None)]
    assert columns == [
        {
            "name": "id",
            "type": "BIGINT()",
            "default": None,
            "comment": None,
            "nullable": False,
        },
        {
            "name": "keyName",
            "type": "VARCHAR()",
            "default": None,
            "comment": None,
            "nullable": False,
        },
        {
            "name": "value",
            "type": "BIGINT()",
            "default": None,
            "comment": None,
            "nullable": False,
        },
        {
            "name": "foreignId",
            "type": "BIGINT()",
            "default": None,
            "comment": None,
            "nullable": False,
        },
    ]
    conn.close()


@pytest.mark.skipif(integration.is_disabled(), reason=integration.disabled_message)
def test_integration_get_schema_names():
    conn = new_conn()

    # SQLite doesn't support schemas, but we'll make sure its going through the
    # motions to arrive at an empty list.
    assert conn.flightsql_get_schema_names() == []
    conn.close()


@pytest.mark.skipif(integration.is_disabled(), reason=integration.disabled_message)
def test_integration_prepared_statement():
    with new_conn() as conn:
        query = 'select * from intTable where id = ? and "keyName" = ?'
        cursor = conn.execute(query, (1, "one"))
        assert cursor.fetchone() == [1, "one", 1, 1]

        query = 'select * from intTable where "keyName" = ?'
        cursor = conn.execute(query, ("negative one",))
        assert cursor.fetchone() == [3, "negative one", -1, 1]

        query = "insert into intTable (keyName, value) values (?, ?)"
        data = [
            ("eight", 8),
            ("nine", 9),
            ("ten", 10),
        ]
        cursor = conn.executemany(query, data)
        assert cursor.fetchone() is None

        query = 'select * from intTable where "keyName" = ?'
        cursor = conn.execute(query, ("eight",))
        assert (cursor.fetchone() or [])[1:] == ["eight", 8, None]
        cursor = conn.execute(query, ("nine",))
        assert (cursor.fetchone() or [])[1:] == ["nine", 9, None]
        cursor = conn.execute(query, ("ten",))
        assert (cursor.fetchone() or [])[1:] == ["ten", 10, None]

        conn.execute('delete from intTable where "keyName" in ("eight", "nine", "ten")')
