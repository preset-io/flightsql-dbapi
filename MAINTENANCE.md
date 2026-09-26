# Maintenance and release policy

## Status and ownership

This repository is Preset's long-lived fork of
[`influxdata/flightsql-dbapi`](https://github.com/influxdata/flightsql-dbapi).
InfluxData archived the upstream repository, making it read-only; upstream
issues and pull requests cannot be opened or merged. Preset therefore owns the
correctness, security, compatibility, and dependency maintenance needed by its
Superset distribution indefinitely.

- **Owning organization:** Preset
- **Maintenance function:** FlightSQL fork maintenance for the Preset Superset
  distribution
- **Current maintenance coordinator:** [`@aminghadersohi`](https://github.com/aminghadersohi)
- **GitHub assignment status:** no multi-member team or repository permission is
  claimed here. On 2026-08-26 the authenticated API exposed real Preset teams,
  but the repository-team endpoint returned 404 and team/repository permission
  checks required unavailable organization-admin scope. A repository admin
  must recheck before the consumer pin. Repository lint validates only the
  syntax and presence of CODEOWNERS and these fields; it cannot infer access,
  assignment, or enforcement from a user/team name.
- **External administrator gates:** branch protection, CODEOWNERS enforcement,
  required human review approvals, and required status checks are GitHub settings
  that only a repository administrator can configure and verify. Repository
  files and green local/CI commands do not prove those gates are active.
- **Review policy:** the coordinator manages each `superset-shell` pin and must
  obtain human review from a `superset-shell` dependency owner. TLS/auth changes
  additionally require a Preset security reviewer. Automated review, including
  an exact-SHA independent review, is diagnostic evidence and is not human
  approval. Do not open a PR for this compatibility series until the requested
  next exact-SHA independent review completes; do not merge or move the consumer
  pin without the external human/admin gates above.

## Supported matrix

The maintained compatibility window is:

| Component | Supported window | CI evidence |
| --- | --- | --- |
| Python | 3.10–3.14 | Every minor on Ubuntu 24.04 |
| SQLAlchemy 1.4 | 1.4.6–1.4.54 | 1.4.6 lower-bound leg; 1.4.54 on every Python |
| SQLAlchemy 2 | 2.0.0–2.0.52 | 2.0.0 lower-bound leg; 2.0.52 on every Python |
| PyArrow | 16.0.0 or newer; reviewed through 25.0.1 | Exact lower/current pins |
| protobuf | 4.21.12 or newer; reviewed through 7.36.0 | Exact lower/current pins |

The package metadata intentionally allows future SQLAlchemy 2.x, PyArrow, and
protobuf patch/minor releases, but versions newer than the reviewed maxima are
not claimed merely because the resolver accepts them. CI pins every reviewed
upper boundary so a dependency bump is an explicit maintenance change rather
than an ambient resolver change. Python or SQLAlchemy support is removed only
in a reviewed fork version after the active `superset-shell` baseline no longer
needs it.

The SQLAlchemy 1.4 floor is deliberately 1.4.6. On 1.4.0, execution-time
literal rendering of one named bind used in multiple SQL positions raises a
post-compile `KeyError`; 1.4.6 handles every occurrence and is covered by the
repeated-bind cache regression. The fork additionally bypasses a 1.4.6 compiler
assertion for empty tuple-valued expanding binds and covers empty/nonempty cache
reuse. The dialect does not claim compatibility with 1.4.0–1.4.5.

The non-prepared compiler renders every bind as a literal. An untyped bind
(for example `text(":v")`) is typed from its Python value with SQLAlchemy's own
literal resolver (str, int, float, Decimal, bool, date/datetime/time, bytes);
a value that resolver cannot map stays untyped and is rejected. Temporal and
binary values render as typed SQL literals (`TIMESTAMP '...'`, `DATE '...'`,
`TIME '...'`, `X'...'`) rather than quoted strings. A `None` value is
handled separately by the compiler and emitted as SQL `NULL` for String,
Integer, and NullType binds on both declared SQLAlchemy floors. A type that opts
into `should_evaluate_none` retains its own literal processor. SQLAlchemy
executemany is not available for these post-compile literal parameters, and the
DB API's direct `executemany()` implementation sends one prepared RPC per row
rather than a true bulk parameter batch.

The bundled SQLite Flight SQL reference server proves the DB API transport,
reflection contracts, literal/prepared compiler paths, and installed-wheel
entry point. It does **not** certify production InfluxDB/IOx semantics or live
Basic authentication. Changes in those areas require bounded unit coverage here
and a staging smoke test by the consuming service before its pin moves.

### DataFusion Flight SQL service compatibility

`0.2.2.2` was qualified live against a local server built on
[`datafusion-flight-sql-server`](https://github.com/datafusion-contrib/datafusion-flight-sql-server)
0.4.19 (DataFusion 55.1), including verified TLS with a private CA and bearer
authentication. That service differs from the reference server in ways the
client now handles, each covered by `tests/test_datafusion_server_compat.py`:

- **GetSqlInfo is unimplemented.** Dialect initialization falls back to the
  `"` identifier quote and the default read/write capability flags instead of
  failing every connection.
- **Metadata is catalog-scoped.** GetDbSchemas/GetTables with no catalog return
  zero rows instead of every catalog. A catalog named in the URL
  (`datafusion://host:port/<catalog>`) scopes every metadata RPC. Without one,
  an unscoped empty answer triggers one GetCatalogs lookup per connection
  (`datafusion` if present, else the only catalog) and a scoped retry. Servers
  that answer unscoped requests never pay for the extra RPC.
- **Views are reported with `table_type = VIEW`.** `get_table_names()` excludes
  them, `get_view_names()` lists them, and `has_table()` covers both.
- **Strings arrive as Utf8View.** Arrow string, binary and list view types map
  to SQL types instead of an untyped blob. Reflected and described types are
  type instances carrying decimal precision/scale, timestamp time zone
  awareness and list element types; unsigned 64-bit integers reflect as
  `NUMERIC(20, 0)`. Cursor descriptions are PEP 249 seven-item tuples.
- **Prepared statements.** The server returns a typed `parameter_schema`, binds
  numbered placeholders (`$1`, `$2`; every bare `?` is the same placeholder to
  it), and returns the bound handle from DoPut as a
  `DoPutPreparedStatementResult`. The client binds against the returned schema
  (nullable, so `NULL` binds work), executes the returned handle, and the
  opt-in prepared-statement feature renders `$n` placeholders on SQLAlchemy 2.
  The default literal path keeps qmark: SQLAlchemy's numeric paramstyles
  rescan the post-compiled statement for `%(name)s`, which would corrupt
  literal values containing that text. The server cannot type a placeholder
  that has no column context (`SELECT $1`); that is a server-side limit.
- **Errors.** PyArrow/Flight failures are re-raised as PEP 249 exceptions
  (`OperationalError` for unavailable/unauthenticated, `NotSupportedError` for
  unimplemented, `ProgrammingError`/`InternalError`/`DatabaseError` otherwise)
  with the original chained, so SQLAlchemy wraps them and pool pre-ping
  recycles connections after a server restart.
- **No placeholder credentials.** Without a token or user/password no
  `authorization` header is sent (previously `Bearer None`).

Writes (DoPut CommandStatementUpdate), transactions and key reflection
(GetPrimaryKeys) are unimplemented by that service and remain unsupported
through it.

Reflection treats a successful zero-row GetTables result as an empty catalog.
If reported names exceed parseable included schemas, only the missing table keys
receive filtered live probes. The cache key includes schema and table name, so
same-named tables cannot cross-contaminate schemas. Confirmed stale rows are
excluded; confirmed existing but permission-scoped/unparseable tables remain
visible to `has_table()` and raise `UnreflectableTableError` for columns instead
of becoming columnless tables. A warning is bounded to once per requested
schema and reports probe/recovery/stale counts. Missing `table_name` columns and
malformed metadata shapes raise `DataError`; GetTables, DoGet, reader, and probe
transport failures retain their original exceptions.

DB API nanosecond temporal normalization supports timestamp, time64, and
duration values recursively through list, large-list, fixed-size-list, struct,
map, dictionary, and dense/sparse union containers. Signed unit counts are
divided by 1,000 toward zero. Nested containers that PyArrow cannot reconstruct
without changing their type/metadata fail explicitly. The CI matrix exercises
the policy with pandas absent and present because pandas must never alter DB API
types or precision.

The generated `flightsql_pb2.pyi` is packaged, but the overall project does not
ship `py.typed` or complete public stubs and therefore makes no PEP 561 typed
package claim. Runtime annotations and the generated stub are checked locally;
consumers must not assume complete third-party type information.

## Release and installation channel

Preset does not own the existing public PyPI project named `flightsql-dbapi`.
This fork must never upload an artifact to public PyPI under that distribution
name. The public-publish workflow has therefore been removed.

The release channel is the internal Preset package index, backed by the
`preset-pypi` object store. `Jenkinsfile` in this repository builds and
publishes the wheel only from an explicit `main` release build; `superset-shell` pins the resulting immutable
artifact URL together with its SHA-256. A PEP 508 direct reference to
`https://github.com/preset-io/flightsql-dbapi.git` at a full commit SHA remains
supported where a source pin is required instead.

1. Record the internal fork version in `pyproject.toml` and
   `flightsql/__init__.py` (currently `0.2.2.2`). Published artifacts are
   immutable and are never rebuilt in place, so any subsequent change ships as
   a new fourth component (`0.2.2.3`, and so on). The pipeline refuses to
   overwrite an existing key rather than relying on this being remembered.
2. Run the installed-wheel matrix and the reference-server suite, and merge the
   reviewed, green version change.
3. In the Jenkins **main** branch job, use **Build with Parameters** and explicitly
   select `PUBLISH_RELEASE=true` for the reviewed main head. The first automatic
   main build installs the parameter with a false default. Ordinary main pushes
   and PR builds run tests, double-build reproducibility and installed-artifact
   checks without S3 calls or AWS credential binding. Do not configure a trigger
   to pass true by default. A release retry for an existing key fails closed;
   inspect the original release rather than overwriting it.
4. Pin the consumer to the immutable artifact URL and its SHA-256. Where a
   source pin is used instead, pin the immutable 40-character commit SHA, or a
   source archive generated from that same SHA and pinned by SHA-256.
5. Record the tested version, digest and matrix result in the consumer change.

### Jenkins administrator prerequisite

Before enabling this publisher, an administrator must verify its GitHub Branch
Source trust configuration and agent/credential isolation. This is a **public**
repository: branch checks and `PUBLISH_RELEASE` in a branch-controlled
Jenkinsfile are release policy, **not** protection against a malicious pipeline.

- Disable **Discover pull requests from forks** on the credential-capable
  publisher. The inspected job currently discovers origin PRs, not fork PRs;
  this observation is not a guarantee of future configuration.
- If fork validation is needed, use a separate credential-free job and agents,
  with fork trust **Nobody** (use the merge target's Jenkinsfile), never
  **Everyone**. Origin PR authors must be trusted repository writers. Restrict
  job configuration, release triggering and repository write access to trusted
  maintainers. Do not equate an external contributor with a trusted writer.
- Using the target Jenkinsfile does not make checked-out PR code safe: tests,
  package installation and build hooks execute it. Untrusted jobs must not have
  access to `ci-user`, publisher workspaces, registry pull secrets or privileged
  service accounts, including ambient cloud credentials. Keep the publisher
  disabled if these conditions cannot be met.

See Jenkins' [SCM credential security guidance](https://www.jenkins.io/doc/book/security/securing-org-folders-and-multibranch-pipelines/)
and [GitHub Branch Source trust behavior](https://www.jenkins.io/doc/pipeline/steps/workflow-scm-step/).
Repository tests do not certify these external controls.

The credentialed `ci` container is pinned by manifest digest, sourced from the
successful `preset-io/docker-images` `build-ci` job **3679**, source commit
`2226e0e250f1bd1c2e4c07a50e7638415f2a14b6`. Its `2025-10-08` tag is rebuilt in
place, so a date tag is not an immutability guarantee. Image updates require a
reviewed digest change and a green build.

### Why Drill's PR publication policy is not used here

Drill deliberately publishes immutable PR local versions for consumers that
pin exact artifact URLs. That protects the bytes at that URL; it does not
protect name/version resolution. For this distribution,
`0.2.2.1+pr.7.abc1234` matches `==0.2.2.1` and sorts above `0.2.2.1` under
[PEP 440 version matching](https://packaging.python.org/en/latest/specifications/version-specifiers/#version-matching).
Publishing both into the shared `flightsql-dbapi/` prefix would reintroduce this
PR's stated shadowing hazard. This public repo also requires the independent
Jenkins trust controls above; URL immutability does not protect AWS credentials.

PR local versions are therefore used only for build/install checks, never
uploaded to the shared index. The published stable URL has no `+` or `%2B`.
This policy prevents **new** PR uploads; it does not remove previously published
PR objects. Before consumer rollout, the index owner must inventory and exclude
any historical PR local-version candidates from name-based index resolution.
Do not delete or replace immutable artifacts that existing URL pins may use.

The consumer must repeat the direct reference in a constraints file and pass it
to every resolver invocation. After installation, copy the self-contained
`scripts/verify-installed-provenance` from a separately verified checkout and
run it with `python -I` as a path-narrowing consumer practice. It does not import
the target package. It derives the selected `.dist-info` directory from the
`importlib.metadata` distribution path rather than RECORD, rejects
duplicate/decoy metadata directories and unsafe RECORD path/hash forms, and
matches RECORD SHA-256 for every non-bytecode regular file in the selected
`flightsql` package and distribution-metadata trees except RECORD itself. The
actual selected `direct_url.json` must be present and SHA-256-covered before its
PEP 610 Git/archive fields are parsed. An archive additionally requires its
externally recorded SHA-256. `--json` uses the explicit fields
`package_source_record_sha256_verified`,
`distribution_metadata_record_sha256_verified`, and
`direct_url_record_sha256_verified`; `commit_verified` means only that those
hash-checked recorded source fields match the asserted SHA/repository.

There is deliberately no installed provenance console entry point: its launcher
and implementation would be supplied by the package under inspection. The
standalone check is still not an authenticity proof. RECORD and PEP 610 are not
signed, so an actor able to rewrite files and RECORD together can forge it. The
scope expressly excludes `.pyc`, site-level `.pth` files, arbitrary
site-packages, dependencies, the Python interpreter, runtime import selection,
and the rest of the environment. `-I` ignores user-site and environment path
inputs but still runs site processing; it neither blocks malicious `.pth` files
in the active environment nor attests site-packages. The trusted boundary
therefore includes
the standalone script, interpreter, `packaging` dependency, externally stored
SHA/digest, resolver inputs, and filesystem access controls. Run in a fresh,
access-controlled environment and never describe the result as environment,
replacement, or compromise detection.

A local wheel/sdist is accepted only in explicit artifact-test mode with an
externally recorded digest and the original file still available at its PEP 610
URL. The verifier reopens that regular non-symlink file and matches its bytes;
it does not rely on pip/uv's optional `archive_info`. The result uses
`local_artifact_file_sha256_verified` and sets `commit_verified` to false. This
does not prove the installed tree was built from that artifact, and the supplied
expected commit remains context rather than build evidence.

The distribution name remains `flightsql-dbapi` because Superset requirements,
the `flightsql` import package, and SQLAlchemy entry-point metadata already use
that identity. The version carries the fork identity instead: upstream `X.Y.Z`
becomes Preset `X.Y.Z.N`. That is the four-component convention already used
throughout this index (PyHive `0.7.0.1`, Exasol `7.1.3.1`, Drill `1.1.11.1`,
pinotdb `9.1.2.1`). Upstream's last public release is `0.2.2`, so the Preset
build is `0.2.2.1`.

This replaced an earlier PEP 440 local version, `0.2.3+preset.2`, which was
unsafe for one specific reason: a local version is *matched* by the
corresponding public specifier. `==0.2.3` admits `0.2.3+preset.2`, so a
resolver asked for the public release could silently receive the Preset build,
and an unconstrained upgrade could silently replace the Preset build with a
public one. It also claimed a base release, `0.2.3`, that upstream never
published.

`0.2.2.1` is a distinct public release rather than a variant of one. No
`==0.2.2` specifier admits it, it sorts above the last public release `0.2.2`
and below `0.2.4`, and it therefore never shadows a version upstream could
still publish. Avoiding `+` also keeps the pinned artifact URL free of `%2B`
encoding.

A distinct version is an identity, not an attestation. The layered source
controls remain the pinned immutable artifact URL and its externally recorded
SHA-256 -- or the direct-reference constraint and immutable full SHA where a
source pin is used -- fresh-environment installation, and the scoped
post-install assertion. They do not turn mutable installed metadata into an
attestation. Never treat version equality alone as proof of source.

## Dependency and security cadence

- During the first full work week of each month, the maintenance coordinator checks supported
  Python, SQLAlchemy 2.x, PyArrow, protobuf, build, test, and GitHub Action
  versions. Reviewed upper-version pins in CI are updated together with a full
  matrix run.
- SQLAlchemy 1.4.54 is final; its leg remains fixed while a supported consumer
  needs SQLAlchemy 1.4.
- Security advisories affecting transport, TLS/auth, Arrow, protobuf, or build
  tooling bypass the monthly cadence and are assessed immediately.
- Before each `superset-shell` dependency refresh, revalidate the CODEOWNER,
  run the installed provenance assertion, `pip check`, lower-bound tests,
  pandas-free and pandas-present temporal tests, lint/type/actionlint, sdist and
  wheel builds, cold-start entry-point loading, and the full installed-artifact
  reference-server matrix.
- Unsupported production-backend behavior is documented as residual risk; a
  green SQLite reference-server run must not be presented as live
  InfluxDB/DataFusion/TLS/auth certification.
