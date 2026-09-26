> [!WARNING]
> This experimental project is Preset's maintained fork of the
> [archived InfluxData repository](https://github.com/influxdata/flightsql-dbapi).
> Preset maintains the DB API and SQLAlchemy integration used by its Superset
> distribution; this is not the recommended general-purpose InfluxDB client.
> See [Maintenance and release policy](MAINTENANCE.md) for ownership and scope.

## Overview

This library provides a [DB API 2](https://peps.python.org/pep-0249/) interface
and [SQLAlchemy](https://www.sqlalchemy.org) Dialect for [Flight
SQL](https://arrow.apache.org/docs/format/FlightSql.html), for example to interact with InfluxDB IOx.

Initially, this library aims to ease the process of connecting to Flight SQL
APIs in [Apache Superset](https://superset.apache.org).

The primary SQLAlchemy Dialect provided by `flightsql-dbapi` targets the
[DataFusion](https://arrow.apache.org/datafusion) SQL execution engine. However,
there are extension points to create custom dialects using Flight SQL as a transport
layer and for metadata discovery.

## Installation

The `flightsql-dbapi` project on public PyPI belongs to InfluxData and does not
contain Preset's fork changes. Preset consumers install this repository from an
immutable, reviewed commit, retaining the distribution name for compatibility:

```text
flightsql-dbapi @ git+https://github.com/preset-io/flightsql-dbapi.git@<full-commit-sha>
```

Put that exact direct reference in the consumer's constraints file as well as
its input requirements, and pass the constraints file to **every** later pip
operation that can resolve dependencies:

```console
python -m pip install --constraint constraints-flightsql.txt --requirement requirements.txt
```

Without the constraint, a later broad install/upgrade can select the public
distribution because this fork deliberately retains its name. After installing,
use a trusted copy of the self-contained verifier as the primary assertion path:

```console
python -I /trusted/verify-installed-provenance \
  --expected-commit <full-commit-sha>
```

Copy `scripts/verify-installed-provenance` from a separately verified checkout
into `superset-shell`; it does not import or execute the target package. It
selects the one `importlib.metadata` distribution, rejects duplicate/decoy
`.dist-info` directories and unsafe RECORD paths/hashes, and matches RECORD
SHA-256 for non-bytecode files in the installed `flightsql` package tree and
the selected distribution-metadata tree, except for RECORD itself. The actual
selected `direct_url.json` must itself be listed and SHA-256-covered before any
of its PEP 610 source fields can set `commit_verified: true`. The installed
console entry point was removed because code supplied by the package under
inspection is not an independent verifier.

This is a package-provenance consistency check, not a signature or whole
environment attestation. It excludes `.pyc`, site-level `.pth` files, other
site-packages, dependencies, interpreter integrity, and later
`sys.path`/import selection. An actor able to rewrite both files and RECORD can
forge the check. `-I` usefully ignores user-site and environment path inputs,
but still runs site processing and cannot detect arbitrary `.pth` or
site-packages compromise. Use a fresh,
access-controlled environment. `--json` names the narrow SHA-256 scopes and
reports whether the hash-covered PEP 610 commit claim matched.

When Git is unavailable, pin the equivalent full-SHA source archive **and its
SHA-256**, then pass the same digest to the verifier. Local wheel/sdist identity
mode reopens the regular non-symlink file named by the local PEP 610 URL and
matches its bytes to the externally supplied digest; it does not trust optional
installer `archive_info` and reports `commit_verified: false`. This verifies the
referenced file, not a build linkage between that file and the installed tree.
Do not publish this fork as `flightsql-dbapi` on a public package index. The
internal fork version is `0.2.2.2`; see
[MAINTENANCE.md](MAINTENANCE.md) for the complete threat boundary and matrix.

## Usage

### DB API 2 Interface ([PEP-249](https://peps.python.org/pep-0249))

```python3
from flightsql import connect, FlightSQLClient

client = FlightSQLClient(host='upstream.server.dev')
conn = connect(client)
cursor = conn.cursor()
cursor.execute('select * from runs limit 10')
print("columns:", cursor.description)
print("rows:", [r for r in cursor])
```

#### DB API value policy

Arrow columns are converted by position, so duplicate SQL column names are
preserved and every row has the same width as `cursor.description`. Arrow NULLs
become `None`; decimals remain `decimal.Decimal`; dates, times, timestamps, and
durations become the corresponding standard-library `date`, `time`, `datetime`,
and `timedelta` values. Because those Python types have microsecond precision,
nanosecond timestamps, times, and durations are deliberately truncated to
microseconds. Truncation divides the signed nanosecond count by 1,000 **toward
zero**: positive values round down, negative values round up, and pre-epoch
timestamps move toward the Unix epoch. The policy is applied recursively through
Arrow list, large-list, fixed-size-list, struct, map, dictionary, and dense/sparse
union values, preserving nested NULLs and timestamp timezones. The result types
and precision do not depend on pandas being installed. A container that embeds a
nanosecond temporal value but cannot be reconstructed safely is rejected with
`NotSupportedError` rather than returning environment-dependent values.

`Cursor.executemany()` is compatibility-oriented rather than a bulk transport:
it prepares once, sends one single-row parameter record/RPC per tuple, discards
any returned rows, and does not report an aggregate affected-row count. Use a
backend-native ingestion path when batching or throughput is required.

### SQLAlchemy

```python3
from sqlalchemy import func, select
from sqlalchemy.engine import create_engine
from sqlalchemy.schema import MetaData, Table

engine = create_engine("datafusion+flightsql://john:appleseeds@upstream.server.dev:443")
runs = Table("runs", MetaData(), autoload_with=engine)
statement = select(func.count()).select_from(runs)
with engine.connect() as connection:
    count = connection.execute(statement).scalar_one()
print("runs count:", count)
print("columns:", [(r.name, r.type) for r in runs.columns])

# Reflection
metadata = MetaData(schema="iox")
metadata.reflect(bind=engine)
print("tables:", [table for table in metadata.sorted_tables])
```

When prepared statements are disabled, normal execution uses SQLAlchemy's
cache-safe post-compile literal rendering. Explicit
`compile_kwargs={"literal_binds": True}` remains fully literal and produces an
executable SQL string. Scalar and tuple-valued empty expanding binds, including
reuse of the same compiled statement after nonempty values, are covered at the
SQLAlchemy 1.4.6 floor and against the bundled SQLite Flight SQL server. That is
not a claim that every live DataFusion/InfluxDB version accepts every empty-set
SQL form, so consumer staging must cover queries that depend on it.

Every non-NULL value in literal mode must have a concrete SQLAlchemy type,
either from a typed column expression or an explicit `bindparam(..., type_=...)`.
An untyped (`NullType`) non-NULL value fails closed because it cannot be quoted
unambiguously. `None` is the exception: `String`, `Integer`, and `NullType` binds
all compile to the SQL expression `NULL` (never the string `'NULL'`) during both
normal execution and `literal_binds` compilation. Types that explicitly set
`should_evaluate_none` keep their own literal processor semantics. SQLAlchemy
executemany with post-compile literal parameters is not supported; execute
individual statements or enable the server's prepared-statement feature.

If a bulk `GetTables(include_schema=True)` response reports more names than
parseable schemas, the dialect issues filtered probes only for those cache
misses and emits one bounded warning per requested schema. Successfully probed
columns are cached, confirmed stale names are excluded, and names confirmed to
exist without schema permission remain visible to `has_table()` while
`get_columns()` fails explicitly with `UnreflectableTableError`—they are never
materialized as false columnless tables. A missing table retains
`NoSuchTableError` semantics. A genuinely empty catalog returns without a
warning, and transport/reader failures propagate.

### Custom Dialects

If your database of choice can't make use of the Dialects provided by this
library directly, you can extend `flightsql.sqlalchemy.FlightSQLDialect` as a
starting point for your own custom Dialect.

```python3
from flightsql.sqlalchemy import FlightSQLDialect
from sqlalchemy.dialects import registry

class CustomDialect(FlightSQLDialect):
    name = "custom"
    paramstyle = 'named'

    # For more information about what's available to override, visit:
    # https://docs.sqlalchemy.org/en/14/core/internals.html#sqlalchemy.engine.default.DefaultDialect

registry.register("custom.flightsql", "path.to.your.module", "CustomDialect")
```

DB API 2 Connection creation is provided by `FlightSQLDialect`. Custom DB API
modules should override one `@classmethod` named `import_dbapi`. The legacy
SQLAlchemy 1.4 spelling `dbapi` remains supported, but a dialect must not define
both names; the selected loader is inherited consistently by child dialects on
both SQLAlchemy generations.

The core reflection APIs of `get_columns`, `get_table_names` and
`get_schema_names` are implemented in terms of Flight SQL API calls so you
shouldn't have to override those unless you have very specific needs.

### Directly with `flightsql.FlightSQLClient`

```python3
from flightsql import FlightSQLClient


client = FlightSQLClient(host='upstream.server.dev',
                         port=443,
                         token='rosebud-motel-bearer-token')
info = client.execute("select * from runs limit 10")
reader = client.do_get(info.endpoints[0].ticket)

table = reader.read_all()
rows = table.to_pylist()
```

### Authentication

Both [Basic and Bearer Authentication](https://arrow.apache.org/docs/format/Flight.html#authentication) are supported.

To authenticate using Basic Authentication, supply a DSN as follows:

```
datafusion+flightsql://user:password@host:443
```

A handshake will be performed with the upstream server to obtain a Bearer token.
That token will be used for the remainder of the engine's lifetime.

To authenticate using Bearer Authentication directly, supply a `token` query parameter
instead:

```
datafusion+flightsql://host:443?token=TOKEN
```

The token will be placed in an appropriate `Authentication: Bearer ...` HTTP header.

### Additional Query Parameters

| Name | Description | Default |
| ---- | ----------- | ------- |
| `insecure` | Connect without SSL/TLS (h2c) | `false` |
| `disable_server_verification` | Disable certificate verification of the upstream server | `false` |
| `token` | Bearer token to use instead of Basic Auth | empty |

Boolean parameters accept `true`, `1`, `yes`, or `on` and `false`, `0`, `no`,
`off`, or an empty value (case-insensitive). Any other value is rejected rather
than silently weakening transport security. `insecure=true` and
`disable_server_verification=true` are mutually exclusive. Direct
`FlightSQLClient` calls likewise require actual `bool` values for these options.

Any query parameters *not* specified in the above table will be sent to the
upstream server as gRPC metadata.
