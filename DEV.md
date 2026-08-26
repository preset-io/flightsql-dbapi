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
not a replacement guarantee. After installation, run the verifier from a
verified checkout (or copy the standalone script into the consumer):

```console
python scripts/verify-installed-provenance \
  --expected-commit <full-commit-sha> \
  --expected-version 0.2.3+preset.1
```

This rejects the public 0.2.2 build, any other version, index installs without
PEP 610 provenance, a different repository, a branch/tag request, and a commit
other than the asserted full SHA. The installed console command
`flightsql-verify-provenance` performs the same check, but the standalone script
is preferable for a replacement detector because it remains available if the
wrong distribution removed that entry point.

For an uncommitted local candidate, create `requirements-local.txt` in the
Superset Docker directory and serve a freshly built source distribution. For
example:

```
flightsql-dbapi @ http://docker.for.mac.host.internal:8000/dist/flightsql_dbapi-0.2.3+preset.1.tar.gz
```

Local wheels and sdists do not encode their source commit. For a controlled
test, record the artifact SHA-256 and invoke the verifier with both
`--expected-artifact-sha256 <digest>` and `--allow-local-artifact`; this mode
proves artifact identity only, so the build system must separately attest its
source commit. Production source-archive installs use a full-SHA GitHub URL plus
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
