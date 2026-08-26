# Maintenance and release policy

## Status and ownership

This repository is Preset's long-lived fork of
[`influxdata/flightsql-dbapi`](https://github.com/influxdata/flightsql-dbapi).
InfluxData archived the upstream repository, making it read-only; upstream
issues and pull requests cannot be opened or merged. Preset therefore owns the
correctness, security, compatibility, and dependency maintenance needed by its
Superset distribution indefinitely.

- **Owning organization:** Preset
- **Primary DRI and CODEOWNER:** [`@aminghadersohi`](https://github.com/aminghadersohi)
- **Owner verification:** on 2026-08-26, GitHub's authenticated organization
  membership API reported an active direct `preset-io` membership and the
  repository collaborator API reported write access. Revalidate both before a
  consumer pin or release; if either check fails, assigning a replacement DRI is
  a hard gate.
- **Review gate:** the DRI coordinates each `superset-shell` pin and must obtain
  review from a `superset-shell` dependency owner. TLS/auth changes additionally
  require a Preset security reviewer. No rewritten compatibility series should
  open a PR until a different engineer has independently approved it.

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
repeated-bind cache regression. The dialect does not claim compatibility with
1.4.0–1.4.5.

The bundled SQLite Flight SQL reference server proves the DB API transport,
reflection contracts, literal/prepared compiler paths, and installed-wheel
entry point. It does **not** certify production InfluxDB/IOx or DataFusion
semantics, real TLS certificate validation, or live Basic/Bearer authentication.
Changes in those areas require bounded unit coverage here and a staging smoke
test by the consuming service before its pin moves.

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
This fork must never upload an artifact under that public distribution name.
The public-publish workflow has therefore been removed.

`superset-shell` historically installs the fork with a PEP 508 direct reference
to `https://github.com/preset-io/flightsql-dbapi.git` at a full commit SHA. That
remains the release channel:

1. Merge a reviewed, green commit in this repository.
2. Record the internal fork version in `pyproject.toml` and
   `flightsql/__init__.py` (currently `0.2.3+preset.1`).
3. Run the installed-wheel matrix and the reference-server suite.
4. Pin the consumer to the immutable 40-character commit SHA. A source archive
   generated from that same SHA and pinned by SHA-256 is acceptable where Git
   installation is not.
5. Record the tested SHA and matrix result in the consumer change.

The consumer must repeat the direct reference in a constraints file and pass it
to every resolver invocation. After installation, run
`scripts/verify-installed-provenance --expected-commit <sha>` from a trusted copy
of this repository (or the installed console equivalent). The verifier requires
the exact internal version and PEP 610 provenance from this repository at the
full requested commit. An archive additionally requires its exact SHA-256. A
local wheel/sdist is accepted only in explicit artifact-test mode with an
externally recorded digest because its `direct_url.json` cannot prove which
source commit built it.

The distribution name remains `flightsql-dbapi` because Superset requirements,
the `flightsql` import package, and SQLAlchemy entry-point metadata already use
that identity. The PEP 440 local version segment (`+preset.N`) distinguishes a
fork build without claiming a public upstream release. If Preset ever needs an
artifact registry rather than source pins, it must first choose a distinct
distribution name (for example, `preset-flightsql-dbapi`) and complete an
explicit packaging, migration, security, and ownership review.

`0.2.3+preset.1` does **not** create a globally unique distribution identity.
Under PEP 440 it sorts after public `0.2.3` but before public `0.2.4`, and a
specifier such as `==0.2.3` also admits the local version. A resolver or later
unconstrained upgrade can therefore select a public build. The local suffix is
diagnostic only; replacement protection comes from the direct-reference
constraint, immutable full SHA, archive/artifact digest where applicable, and
the post-install provenance assertion. Never treat version equality alone as
proof of source.

## Dependency and security cadence

- During the first full work week of each month, the DRI checks supported
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
