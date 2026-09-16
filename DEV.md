# Development instructions

## Prerequisites

- One of the supported Python versions (3.10 through 3.14)
- Docker with Compose for the SQLite Flight SQL reference server
- A Superset checkout when testing the consumer integration

See [MAINTENANCE.md](MAINTENANCE.md) for the complete compatibility matrix and
the limits of the reference-server coverage.

## Superset directory

Preset's durable installation path is an immutable commit, not the public PyPI
project. Add a full reviewed SHA to the consumer requirements:

```text
flightsql-dbapi @ git+https://github.com/preset-io/flightsql-dbapi.git@<full-commit-sha>
```

Add the same line to the consumer constraints file and use that constraint for
every pip install/upgrade in the image build. A requirement in only one layer is
not a source guarantee. After installation, copy the self-contained verifier
from a separately verified checkout and invoke it with narrowed path inputs:

```console
python -I /trusted/verify-installed-provenance \
  --expected-commit <full-commit-sha> \
  --expected-version 0.2.2.1
```

This rejects the public 0.2.2 build, any other version, index installs without
PEP 610 provenance, a different repository, a branch/tag request, and a commit
other than the asserted full SHA when the recorded source mode is Git/archive.
It also matches RECORD SHA-256 for non-bytecode files in the selected package
and actual `.dist-info` trees (except RECORD itself) without importing
`flightsql`; `direct_url.json` must be among those hash-covered files before a
source claim is accepted. There is no installed console entry point because the
package being inspected cannot supply an independent verifier. Use `--json`
for the explicitly named package and metadata scopes. `commit_verified` is a
match against hash-covered PEP 610
metadata, not an environment or runtime-import attestation. `-I` does not detect
malicious `.pth`, bytecode, other site-packages, or interpreter compromise.

For an uncommitted local candidate, create `requirements-local.txt` in the
Superset Docker directory and serve a freshly built source distribution. For
example:

```
flightsql-dbapi @ http://docker.for.mac.host.internal:8000/dist/flightsql_dbapi-0.2.2.1.tar.gz
```

Local artifacts do not encode their source commit. For a controlled test, retain
the artifact at its PEP 610 file URL, record its SHA-256 before installation,
and invoke the verifier with both
`--expected-artifact-sha256 <digest>` and `--allow-local-artifact`; this mode
reopens that non-symlink wheel/sdist and hashes its bytes without relying on pip
or uv's optional `archive_info`, and deliberately reports that it did **not**
verify the supplied commit. It does not prove that the installed tree was built
from that file. The build system must separately attest the source commit and
retain the original artifact/digest evidence.
Production source-archive installs use a full-SHA GitHub URL plus
`#sha256=<digest>` and do not use the local-artifact escape hatch.

- Run superset using docker compose:

`docker-compose -f ./docker-compose-non-dev.yml up`

## Building `flightsql-dbapi`

- run `make build`

- expose a http server: `python3 -m http.server 8000`

To verify that Docker is communicating on port 8000, check the HTTP server log
for a request for the exact artifact you built.

## Testing it on Superset

- Select other as the type and name appropriately

- Provide a URI:

`datafusion+flightsql://${host}:${port}/?bucket-name=${my-bucket}&token=${my-token}`

- Create a chart, add a dataset, and select the named database and schema.

This exercise is a consumer smoke test. Use a real staging backend for changes
to DataFusion/InfluxDB, TLS, or authentication; the repository's SQLite server
does not prove those production semantics.

## Dependency Management

- Handle dependencies using `direnv` and an `.envrc`; this sets the virtual
  environment used by the Make targets.

- `brew install direnv`

- create a .envrc, setting the venv and layout to python:

```
$ cat .envrc
export VIRTUAL_ENV=venv
layout python3
 ```

- If using zsh add the following to your .zshrc:

`eval "$(direnv hook zsh)"`

If you make changes to your .envrc it should ask you to accept the changes ie:

`direnv allow .`
