import pytest
from sqlalchemy import Column, Integer, String, bindparam, create_engine, event, select
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session, declarative_base
from sqlalchemy.schema import MetaData, Table
from sqlalchemy.sql import compiler
from sqlalchemy.sql.sqltypes import NullType

import flightsql.flightsql_pb2 as flightsql
from flightsql.sqlalchemy import FEATURE_PREPARED_STATEMENTS, LiteralBindCompiler

from . import integration


def new_sqlalchemy_engine(features=None):
    host, port = integration.host_port()
    query = {"insecure": "true"}
    for k, v in (features or {}).items():
        query[f"feature-{k}"] = v
    url = URL.create(drivername="datafusion+flightsql", host=host, port=int(port), query=query)
    return create_engine(url)


@pytest.mark.skipif(integration.is_disabled(), reason=integration.disabled_message)
def test_integration_dialect_configuration():
    engine = new_sqlalchemy_engine()
    # Force the connection so we get our SQL information.
    with engine.connect():
        pass
    info = engine.dialect.sql_info
    assert info[flightsql.FLIGHT_SQL_SERVER_READ_ONLY] is False
    assert info[flightsql.FLIGHT_SQL_SERVER_NAME] == "db_name"
    assert info[flightsql.FLIGHT_SQL_SERVER_ARROW_VERSION] == "11.0.0-SNAPSHOT"
    engine.dispose()


@pytest.mark.skipif(integration.is_disabled(), reason=integration.disabled_message)
def test_integration_sqlalchemy_preserves_duplicate_column_names_and_width():
    engine = new_sqlalchemy_engine()

    with engine.connect() as connection:
        result = connection.exec_driver_sql("select id, keyName, id from intTable order by id")
        keys = list(result.keys())
        rows = [tuple(row) for row in result]

    assert keys == ["id", "keyName", "id"]
    assert rows == [
        (1, "one", 1),
        (2, "zero", 2),
        (3, "negative one", 3),
        (4, None, 4),
    ]
    assert all(len(row) == len(keys) for row in rows)
    engine.dispose()


@pytest.mark.skipif(integration.is_disabled(), reason=integration.disabled_message)
def test_integration_metadata_reflect_handles_tables_without_column_metadata():
    engine = new_sqlalchemy_engine()
    metadata = MetaData()

    metadata.reflect(bind=engine)

    assert "intTable" in metadata.tables
    assert [column.name for column in metadata.tables["intTable"].columns] == [
        "id",
        "keyName",
        "value",
        "foreignId",
    ]
    engine.dispose()


@pytest.mark.skipif(integration.is_disabled(), reason=integration.disabled_message)
def test_integration_literal_bind_queries_do_not_reuse_cached_values():
    engine = new_sqlalchemy_engine()
    table = Table("intTable", MetaData(), autoload_with=engine)
    compiled_cache = {}

    with engine.connect().execution_options(compiled_cache=compiled_cache) as connection:
        rows = []
        for identifier in (1, 2, 3):
            statement = select(table.c.id, table.c["keyName"]).where(table.c.id == identifier)
            rows.append(tuple(connection.execute(statement).one()))

    assert rows == [(1, "one"), (2, "zero"), (3, "negative one")]
    assert len(compiled_cache) == 1
    engine.dispose()


@pytest.mark.skipif(integration.is_disabled(), reason=integration.disabled_message)
def test_integration_repeated_bind_is_rendered_for_every_occurrence_and_cache_reuse():
    engine = new_sqlalchemy_engine()
    table = Table("intTable", MetaData(), autoload_with=engine)
    repeated = bindparam("candidate", type_=Integer)
    statement = (
        select(table.c.id).where((table.c.id == repeated) | (table.c.foreignId == repeated)).order_by(table.c.id)
    )
    compiled_cache = {}
    executed = []

    def capture_sql(_connection, _cursor, sql, parameters, _context, _executemany):
        if "WHERE" in sql:
            executed.append((sql, parameters))

    event.listen(engine, "before_cursor_execute", capture_sql)
    with engine.connect().execution_options(compiled_cache=compiled_cache) as connection:
        assert [row[0] for row in connection.execute(statement, {"candidate": 1})] == [1, 2, 3]
        assert [row[0] for row in connection.execute(statement, {"candidate": 3})] == [3]

    assert len(compiled_cache) == 1
    assert len(executed) == 2
    assert executed[0][0].count(" = 1") == 2
    assert executed[1][0].count(" = 3") == 2
    assert executed[0][1] in ((), {})
    assert executed[1][1] in ((), {})
    engine.dispose()


@pytest.mark.skipif(integration.is_disabled(), reason=integration.disabled_message)
def test_integration_literal_binds_compile_string_is_directly_executable_and_escaped():
    engine = new_sqlalchemy_engine()
    table = Table("intTable", MetaData(), autoload_with=engine)
    candidate = bindparam("candidate", type_=String)
    statement = select(table.c.id).where(table.c["keyName"] == candidate).order_by(table.c.id)

    with engine.connect() as connection:
        compiled = statement.params(candidate="one").compile(
            dialect=engine.dialect,
            compile_kwargs={"literal_binds": True},
        )
        assert "POSTCOMPILE" not in str(compiled)
        assert [row[0] for row in connection.exec_driver_sql(str(compiled))] == [1]

        hostile = statement.params(candidate="one' OR 1=1 --").compile(
            dialect=engine.dialect,
            compile_kwargs={"literal_binds": True},
        )
        assert "one'' OR 1=1 --" in str(hostile)
        assert connection.exec_driver_sql(str(hostile)).all() == []

    engine.dispose()


@pytest.mark.skipif(integration.is_disabled(), reason=integration.disabled_message)
@pytest.mark.parametrize(
    ("column_name", "bind_type", "operator", "expected_ids"),
    [
        ("keyName", String(), "equality", []),
        ("keyName", String(), "is-not-distinct", [4]),
        ("keyName", String(), "is-distinct", [1, 2, 3]),
        ("id", Integer(), "equality", []),
        ("id", Integer(), "is-not-distinct", []),
        ("id", Integer(), "is-distinct", [1, 2, 3, 4]),
        ("keyName", NullType(), "equality", []),
        ("keyName", NullType(), "is-not-distinct", [4]),
        ("keyName", NullType(), "is-distinct", [1, 2, 3]),
    ],
    ids=lambda value: type(value).__name__ if not isinstance(value, str) else value,
)
def test_integration_none_typed_binds_execute_as_sql_null_in_normal_and_literal_modes(
    column_name, bind_type, operator, expected_ids
):
    engine = new_sqlalchemy_engine()
    reflected = Table("intTable", MetaData(), autoload_with=engine)
    candidate = bindparam("candidate", type_=bind_type)
    value_column = reflected.c[column_name]
    if operator == "equality":
        predicate = value_column == candidate
    elif operator == "is-not-distinct":
        predicate = value_column.isnot_distinct_from(candidate)
    else:
        predicate = value_column.is_distinct_from(candidate)
    statement = select(reflected.c.id).where(predicate).order_by(reflected.c.id)

    with engine.connect() as connection:
        assert [row[0] for row in connection.execute(statement, {"candidate": None})] == expected_ids
        literal = statement.params(candidate=None).compile(
            dialect=engine.dialect,
            compile_kwargs={"literal_binds": True},
        )
        assert "NULL" in str(literal)
        assert "'NULL'" not in str(literal)
        assert [row[0] for row in connection.exec_driver_sql(str(literal))] == expected_ids

    engine.dispose()


@pytest.mark.skipif(integration.is_disabled(), reason=integration.disabled_message)
def test_integration_none_and_non_null_typed_binds_share_cache_without_reusing_rendered_sql():
    engine = new_sqlalchemy_engine()
    reflected = Table("intTable", MetaData(), autoload_with=engine)
    statement = select(reflected.c.id).where(
        reflected.c["keyName"].isnot_distinct_from(bindparam("candidate", type_=String()))
    )
    compiled_cache = {}

    with engine.connect().execution_options(compiled_cache=compiled_cache) as connection:
        assert connection.execute(statement, {"candidate": "one"}).scalar_one() == 1
        assert connection.execute(statement, {"candidate": None}).scalar_one() == 4

    assert len(compiled_cache) == 1
    engine.dispose()


@pytest.mark.skipif(integration.is_disabled(), reason=integration.disabled_message)
def test_integration_sqlalchemy_generated_empty_in_is_executable_on_reference_server():
    engine = new_sqlalchemy_engine()
    table = Table("intTable", MetaData(), autoload_with=engine)
    statement = select(table.c.id).where(table.c.id.in_([]))

    with engine.connect() as connection:
        assert connection.execute(statement).all() == []
        compiled = statement.compile(
            dialect=engine.dialect,
            compile_kwargs={"literal_binds": True},
        )
        assert "POSTCOMPILE" not in str(compiled)
        assert connection.exec_driver_sql(str(compiled)).all() == []

    engine.dispose()


@pytest.mark.skipif(integration.is_disabled(), reason=integration.disabled_message)
def test_integration_dialect_basic_orm():
    engine = new_sqlalchemy_engine()
    base = declarative_base()
    metadata = MetaData()

    # Connect to ensure we're using the literal binding compiler.
    with engine.connect():
        pass
    assert engine.dialect.statement_compiler == LiteralBindCompiler

    class Record(base):
        __tablename__ = Table("intTable", metadata, autoload_with=engine)
        id = Column(Integer, primary_key=True)
        key_name = Column(Integer, name="keyName")
        value = Column(String)

    session = Session(engine)
    stmt = select(Record).where(Record.id.in_([1, 2, 3]))
    results = session.execute(stmt).scalars().all()
    assert [r.key_name for r in results] == ["one", "zero", "negative one"]
    assert [r.value for r in results] == [1, 0, -1]
    session.close()
    engine.dispose()


@pytest.mark.skipif(integration.is_disabled(), reason=integration.disabled_message)
def test_integration_dialect_basic_orm_with_prepared_statements():
    engine = new_sqlalchemy_engine(features={FEATURE_PREPARED_STATEMENTS: "on"})
    base = declarative_base()
    metadata = MetaData()

    # Connect to ensure we're using the default compiler.
    with engine.connect():
        pass
    assert engine.dialect.statement_compiler == compiler.SQLCompiler

    class Record(base):
        __tablename__ = Table("intTable", metadata, autoload_with=engine)
        id = Column(Integer, primary_key=True)
        key_name = Column(Integer, name="keyName")
        value = Column(String)

    session = Session(engine)
    stmt = select(Record).where(Record.id.in_([1, 2, 3]))
    results = session.execute(stmt).scalars().all()
    assert [r.key_name for r in results] == ["one", "zero", "negative one"]
    assert [r.value for r in results] == [1, 0, -1]
    session.close()
    engine.dispose()
