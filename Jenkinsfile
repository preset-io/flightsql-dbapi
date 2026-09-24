// Internal publication path for flightsql-dbapi.
//
// Publishes to the Preset-internal package index, which is backed by the
// s3://preset-pypi bucket.  Every operation in this file talks to the bucket
// over the AWS API using 'ci-user', bound only for explicit main releases. This
// repository is PUBLIC, so the index's public hostname is deliberately not
// referenced here, in any commit message, or in any pull request: consumers
// resolve the artifact through the documented internal index base URL, which
// is held in internal documentation rather than in this repository.
//
// Modelled on the existing Preset publishers (sqlalchemy-exasol,
// sqlalchemy-drill, pinot-dbapi).  No credential material lives in this file:
// the AWS keys are bound at runtime by Jenkins and are never echoed.
//
// JENKINS ADMIN PRECONDITION (see MAINTENANCE.md)
// ---------------------------------------------
// This public repository's branch-controlled Jenkinsfile is NOT a security
// boundary. Never execute an untrusted PR on a credential-capable job/agent.
// Disable fork PR discovery on this publisher; if enabled in a separate,
// credential-free validation job, use GitHub Branch Source trust "Nobody"
// (merge target's Jenkinsfile), NEVER "Everyone". Origin PR authors must be
// trusted writers. A trusted Jenkinsfile alone does not make PR build/test
// code safe. Isolate untrusted agents from publisher credentials, pull secrets,
// service accounts and workspaces. Admin verification is required before use.
//
// WHAT IS PUBLISHED
// -----------------
// The pure-Python wheel ONLY.  Both the wheel and the sdist are built, and
// BOTH are checked for byte-reproducibility, but only the wheel is uploaded.
//
// The sdist-vs-wheel choice was made by measurement, not by copying a sibling,
// because the sibling publishers disagree and both were right about their own
// backend.  Drill (setuptools) found its sdist unreproducible even with
// SOURCE_DATE_EPOCH pinned, so it publishes the wheel.  Pinot (poetry) found
// its wheel hash not byte-stable across builders, so it publishes the sdist.
// This project uses hatchling, and hatchling normalises member order and
// mtimes for both targets: rebuilding this commit produces a byte-identical
// wheel AND a byte-identical sdist, across `python -m build` and `uv build`
// alike.  Reproducibility therefore does not force the choice here, so the
// wheel is published for the consumer's benefit: it installs with no build
// step, so the Superset image never has to resolve and run the pinned
// `hatchling<=1.18.0` build backend at install time.  The reproducibility gate
// below still covers both artifacts, so a future backend change that breaks
// either one fails the build instead of silently publishing.
//
// VERSIONING
// ----------
// Four-component convention already used in this bucket (upstream PyHive
// 0.7.0 -> Preset 0.7.0.1, Exasol 7.1.3.1, Drill 1.1.11.1, pinotdb 9.1.2.1).
// Upstream's last public PyPI release is 0.2.2, so the Preset build is
// 0.2.2.1, declared in pyproject.toml so the published version is auditable in
// git rather than synthesised here.
//
//   * main        -> stable 0.2.2.1; build and verify on every push, publish
//                    ONLY with PUBLISH_RELEASE=true after human release review.
//   * PR branches -> 0.2.2.1+pr.<n>.<shortsha>, for build/install checks ONLY.
//                    No S3 access, credential binding or index publication.
//                    Local versions match ==0.2.2.1 and sort above it, so
//                    immutable PR URLs in the shared index are NOT safe here.
//
// A four-component version is used rather than a PEP 440 local version such as
// 0.2.3+preset.2 because a local version is MATCHED by the corresponding
// public specifier: `==0.2.3` admits `0.2.3+preset.2`, so a resolver asked for
// the public release can silently select the Preset build and vice versa.
// `==0.2.2` does not admit 0.2.2.1.  Avoiding '+' also keeps the pinned URL
// free of %2B encoding.  0.2.2.1 still sorts ABOVE upstream 0.2.2, so it must
// never be offered to a resolver as a candidate for the plain
// `flightsql-dbapi` name.  Consumers pin the immutable wheel URL directly.
//
// IMMUTABILITY
// ------------
// Published artifacts are never overwritten.  `aws s3 sync` is deliberately
// NOT used, because it replaces an object whose content has changed.  The
// upload is a conditional write with IfNoneMatch='*', which S3 rejects with a
// 412 if the key already exists, making the no-overwrite guarantee atomic
// rather than a check-then-write race.
//
// `aws s3api put-object --if-none-match` would express the same thing, but it
// requires AWS CLI >= 2.18 and the 'ci' image ships AWS CLI v1, which cannot
// express the header at all.  Drill hit this in its fork PR #6.  Rather than
// falling back to `aws s3 sync` -- which would silently permit an overwrite --
// the conditional put is issued through a current boto3 client, which
// preserves the atomic 412 guard.  Do not "fix" this by weakening it; if the
// conditional write is unavailable the build must fail closed and publish
// nothing.

// Bucket prefix / requirement name vs. artifact filename.  These differ:
// hatchling emits the UNDERSCORED distribution name in the filename, while the
// bucket prefix and the Shell requirement use the DASHED project name.
// Conflating the two makes the existence check probe a key that never exists,
// so it always reports "absent" and an overwrite slips through silently.
// Drill documents this exact bug.
LIB_NAME = 'flightsql-dbapi'
DIST_NAME = 'flightsql_dbapi'
BUCKET = 'preset-pypi'

// Default-off release intent prevents docs/CI merges from republishing the
// unchanged version. Releasing an existing version still fails closed.
properties([parameters([
    booleanParam(name: 'PUBLISH_RELEASE', defaultValue: false,
                 description: 'Publish a reviewed main release with a NEW version; never enabled for PRs.')
])])

String baseVersion = ""
String publishVersion = ""
String wheelName = ""
String sdistName = ""
String key = ""
String digest = ""

podTemplate(
    imagePullSecrets: ['preset-pull'],
    nodeUsageMode: 'NORMAL',
    containers: [
        containerTemplate(
            alwaysPullImage: true,
            name: 'ci',
            // preset-io/docker-images build-ci #3679, source 2226e0e250f1.
            // Use the push digest, not the rebuildable 2025-10-08 tag.
            image: 'preset/ci@sha256:4b63a26fefede56a1bc0ba566dc2078f0bbeb1c563735b73ec55661f8505a5d2',
            ttyEnabled: true,
            command: 'cat',
            resourceRequestCpu: '100m',
            resourceLimitCpu: '200m',
            resourceRequestMemory: '1000Mi',
            resourceLimitMemory: '2000Mi',
        ),
        containerTemplate(
            alwaysPullImage: true,
            // flightsql-dbapi requires python >=3.10, so the 3.9 image used by
            // the older publishers is not usable here.  Same image as pinotdb.
            name: 'py-ci',
            image: 'preset/python:3.11.14-2026-05-22-ci',
            ttyEnabled: true,
            command: 'cat'
        )
    ]
) {
    node(POD_LABEL) {
        def repo = checkout scm
        sh(script: "git config --global --add safe.directory '*'", label: 'Trust workspace')
        def shortGitRev = sh(
                returnStdout: true,
                script: 'git rev-parse --short HEAD'
        ).trim()

        boolean isPullRequest = env.CHANGE_ID || env.BRANCH_NAME.startsWith('PR-')
        boolean isMain = (env.BRANCH_NAME == 'main' && !isPullRequest)
        boolean publishes = isMain && params.PUBLISH_RELEASE == true

        container('py-ci') {
            stage('Resolve version') {
                // pyproject.toml is the single source of truth.  Read it
                // without importing or building so a broken build cannot fake
                // the version.
                baseVersion = sh(
                        script: "grep '^version' pyproject.toml | head -1 | cut -d'\"' -f2",
                        returnStdout: true,
                        label: 'Read declared version'
                ).trim()
                if (!baseVersion) {
                    error('Could not read version from pyproject.toml')
                }

                if (isPullRequest) {
                    // PEP 440 normalises a local version segment: it is
                    // lower-cased and '-'/'_' become '.'.  The build backend
                    // therefore writes 'pr.7.abc1234', not 'PR-7.abc1234', into
                    // the artifact filename.  Normalising here rather than
                    // predicting the raw branch name keeps the build filename
                    // and local install check identical to the actual artifact.
                    // PR artifacts never reach the bucket. The post-build
                    // assertion fails if backend normalisation changes.
                    String localSegment = "${env.BRANCH_NAME}.${shortGitRev}".toLowerCase().replaceAll(/[-_]/, '.')
                    publishVersion = "${baseVersion}+${localSegment}"
                } else {
                    publishVersion = baseVersion
                }

                // A stable release may only come from reviewed, merged history.
                if (!isMain && publishVersion == baseVersion) {
                    error("Refusing to build stable version ${baseVersion} from branch " +
                          "'${env.BRANCH_NAME}'. Stable releases are published only from main.")
                }
                if (isPullRequest && !publishVersion.contains('+')) {
                    error("PR build produced a non-local version ${publishVersion}; refusing.")
                }

                wheelName = "${DIST_NAME}-${publishVersion}-py3-none-any.whl"
                sdistName = "${DIST_NAME}-${publishVersion}.tar.gz"
                key = "${LIB_NAME}/${wheelName}"
                echo "Base version: ${baseVersion}"
                echo "Publish version: ${publishVersion}${isMain ? ' (STABLE)' : ' (local test build; NOT published)'}"
            }

            stage('Tests') {
                sh(script: 'python -m pip install --upgrade pip', label: 'Upgrade pip')
                sh(script: 'python -m pip install ".[test,lint]"', label: 'Install project with test and lint extras')
                // The integration cases skip themselves without a live Flight
                // SQL reference server, so this is effectively the unit suite.
                // The full Python x SQLAlchemy matrix is covered by the GitHub
                // Actions workflow on this repo; this run is a gate on the
                // commit being published, not a substitute for it.
                //
                // Runs BEFORE the local test version is applied, so the
                // version linter and the packaging tests see the declared
                // version rather than the rewritten one.
                sh(
                    script: 'SQLALCHEMY_SILENCE_UBER_WARNING=1 python -m pytest -q',
                    label: 'Tests'
                )
                sh(script: 'VENV_BIN="$(dirname "$(command -v python)")" scripts/lint-version', label: 'Version agreement')
                sh(script: 'python scripts/lint-maintenance', label: 'Ownership metadata')
            }
        }

        container('ci') {
            stage('Reject an already-published version') {
                if (!publishes) {
                    echo 'No explicit main release requested; skipping S3 lookup and credentials.'
                    return
                }
                withCredentials([
                    [
                        $class           : 'AmazonWebServicesCredentialsBinding',
                        credentialsId    : 'ci-user',
                        accessKeyVariable: 'AWS_ACCESS_KEY_ID',
                        secretKeyVariable: 'AWS_SECRET_ACCESS_KEY',
                    ]
                ]) {
                    // Queried against the bucket over the AWS API rather than
                    // over the index's public HTTP front door: it is the
                    // authoritative source, it needs no hostname, and it is
                    // not subject to any caching in front of the index.
                    //
                    // This is a courtesy check that fails early with a clear
                    // message.  The conditional write below is what actually
                    // guarantees no overwrite.
                    def exists = sh(
                            script: "aws s3api head-object --bucket ${BUCKET} --key ${key} >/dev/null 2>&1",
                            returnStatus: true,
                            label: 'Check for an existing wheel'
                    )
                    if (exists == 0) {
                        error("${key} is already published. Published artifacts are " +
                              "immutable; bump the version in pyproject.toml and " +
                              "flightsql/__init__.py.")
                    }
                }
            }
        }

        container('py-ci') {
            stage('Build and verify reproducibility') {
                if (isPullRequest) {
                    // Both files are rewritten together: scripts/lint-version
                    // ties them, and the artifact identity depends on both.
                    sh(
                        script: """
                            set -eu
                            sed -i 's/^version = \"${baseVersion}\"/version = \"${publishVersion}\"/' pyproject.toml
                            sed -i 's/^__version__ = \"${baseVersion}\"/__version__ = \"${publishVersion}\"/' flightsql/__init__.py
                            grep -q '^version = \"${publishVersion}\"' pyproject.toml
                            grep -q '^__version__ = \"${publishVersion}\"' flightsql/__init__.py
                        """,
                        label: 'Apply local test version'
                    )
                }

                sh(script: 'python -m pip install build', label: 'Install build tooling')
                sh(script: 'rm -rf dist upload /tmp/build-a /tmp/build-b', label: 'Clean build outputs')

                // Pin the build clock to the commit so the artifacts are
                // byte-reproducible from this exact revision.  Jenkins rejects
                // the repository as dubiously owned unless the workspace is
                // marked safe, which silently yields an EMPTY epoch and an
                // unreproducible build; Drill hit this in its fork PR #5.
                // Validate that the epoch is numeric and fail closed.
                //
                // Both builds write OUTSIDE the source tree.  This is not
                // cosmetic: hatchling's sdist includes any untracked directory
                // that .gitignore does not cover, so building into ./build-a
                // and ./build-b makes the second sdist swallow the first
                // build's artifacts.  The two sdists then differ on every run
                // and the reproducibility gate below fails permanently.  The
                // source tree must be identical for both builds.
                sh(
                    script: '''
                        set -eu
                        SOURCE_DATE_EPOCH="$(git -c safe.directory="$PWD" log -1 --pretty=%ct)"
                        case "$SOURCE_DATE_EPOCH" in
                            ''|*[!0-9]*) echo "invalid SOURCE_DATE_EPOCH: $SOURCE_DATE_EPOCH" >&2; exit 1 ;;
                        esac
                        export SOURCE_DATE_EPOCH
                        python -m build --outdir /tmp/build-a
                        python -m build --outdir /tmp/build-b
                    ''',
                    label: 'Build twice'
                )

                // Fail closed if the backend did not produce exactly the
                // filenames the bucket key was derived from -- a normalisation
                // difference must stop the build, not silently publish to, or
                // probe, the wrong key.
                sh(
                    script: """
                        set -eu
                        for d in /tmp/build-a /tmp/build-b; do
                            test -f "\$d/${wheelName}" || { echo "missing \$d/${wheelName}"; ls -1 "\$d"; exit 1; }
                            test -f "\$d/${sdistName}" || { echo "missing \$d/${sdistName}"; ls -1 "\$d"; exit 1; }
                        done
                    """,
                    label: 'Assert expected artifact filenames'
                )

                // A rebuild from the same commit must produce byte-identical
                // bytes, otherwise the published digest cannot be re-derived
                // by anyone auditing the artifact later.  Both the wheel and
                // the sdist are compared even though only the wheel ships.
                sh(
                    script: """
                        set -eu
                        for artifact in '${wheelName}' '${sdistName}'; do
                            a="\$(sha256sum "/tmp/build-a/\$artifact" | cut -d' ' -f1)"
                            b="\$(sha256sum "/tmp/build-b/\$artifact" | cut -d' ' -f1)"
                            echo "\$artifact"
                            echo "  build A sha256: \$a"
                            echo "  build B sha256: \$b"
                            if [ "\$a" != "\$b" ]; then
                                echo "Build is not reproducible; refusing to publish." >&2
                                exit 1
                            fi
                        done
                    """,
                    label: 'Compare digests'
                )

                sh(script: 'python -m pip install twine', label: 'Install twine')
                sh(script: 'python -m twine check --strict /tmp/build-a/*', label: 'Verify artifact metadata')

                // Stage the wheel alone.  The sdist is intentionally left
                // behind and never reaches the bucket.
                sh(
                    script: "mkdir -p upload && cp /tmp/build-a/${wheelName} upload/",
                    label: 'Stage wheel for upload'
                )
                digest = sh(
                    script: "sha256sum upload/${wheelName} | cut -d' ' -f1",
                    returnStdout: true,
                    label: 'Record built digest'
                ).trim()
                echo "Artifact ${wheelName} sha256 ${digest}"
            }

            stage('Verify installable artifact') {
                // Install the exact artifact that would be published, into a
                // clean interpreter, and assert the packaged version and BOTH
                // documented SQLAlchemy dialect entry points resolve to
                // DataFusionDialect.  Superset reaches the dialect purely
                // through this entry point metadata, so an artifact that
                // builds but does not register is worthless.
                //
                // Resolution goes through make_url(...).get_dialect(), which
                // exercises the real URL -> entry point -> class path with no
                // I/O.  create_engine() is avoided deliberately: this dialect
                // constructs its Flight client eagerly in
                // create_connect_args, so it would require a live server.
                sh(
                    script: """
                        set -eu
                        rm -rf /tmp/verify
                        python -m venv /tmp/verify
                        /tmp/verify/bin/pip install --quiet 'upload/${wheelName}'
                        /tmp/verify/bin/python - <<'EOF'
import importlib.metadata as md

import sqlalchemy as sa
from sqlalchemy.engine.url import make_url

dist = md.distribution("flightsql-dbapi")
assert dist.version == "${publishVersion}", dist.version

groups = {(e.group, e.name) for e in dist.entry_points}
for name in ("datafusion", "datafusion.flightsql"):
    assert ("sqlalchemy.dialects", name) in groups, name

for url in ("datafusion://h:443", "datafusion+flightsql://h:443"):
    dialect = make_url(url).get_dialect()
    assert dialect.__name__ == "DataFusionDialect", (url, dialect)

print("verified", dist.version, "on sqlalchemy", sa.__version__)
EOF
                    """,
                    label: 'Install and inspect artifact'
                )
            }
        }

        container('ci') {
            stage('Publish wheel') {
                if (!publishes) {
                    echo 'No explicit main release requested; built and verified only, without publication.'
                    return
                }
                withCredentials([
                    [
                        $class           : 'AmazonWebServicesCredentialsBinding',
                        credentialsId    : 'ci-user',
                        accessKeyVariable: 'AWS_ACCESS_KEY_ID',
                        secretKeyVariable: 'AWS_SECRET_ACCESS_KEY',
                    ]
                ]) {
                    // Atomic no-overwrite.  See the IMMUTABILITY note above:
                    // the 'ci' image has AWS CLI v1, which cannot express
                    // If-None-Match, so a current boto3 client issues the
                    // conditional put and S3 answers 412 if the key exists.
                    sh(
                        script: """
                            set -eu
                            python -m pip install --quiet 'boto3>=1.36,<2'
                            BUCKET='${BUCKET}' KEY='${key}' ARTIFACT='upload/${wheelName}' \
                              python -c 'import os, boto3; artifact = open(os.environ["ARTIFACT"], "rb"); boto3.client("s3").put_object(Bucket=os.environ["BUCKET"], Key=os.environ["KEY"], Body=artifact, IfNoneMatch="*")'
                        """,
                        label: 'Upload wheel (no-overwrite)'
                    )

                    // Read the stored object back and digest THAT, rather than
                    // trusting the local build.  This is the SHA-256 a
                    // consumer pins.  It verifies the bytes at rest in the
                    // bucket; the index front door is a pass-through over the
                    // same object.
                    sh(
                        script: """
                            set -eu
                            aws s3api get-object \
                              --bucket ${BUCKET} \
                              --key ${key} \
                              stored.whl >/dev/null
                            STORED="\$(sha256sum stored.whl | cut -d' ' -f1)"
                            LOCAL='${digest}'
                            if [ "\$STORED" != "\$LOCAL" ]; then
                                echo "Stored digest \$STORED does not match built digest \$LOCAL" >&2
                                exit 1
                            fi
                            printf '%s  %s\\n' "\$STORED" '${wheelName}' > published.sha256
                            echo "=============================================================="
                            echo " Pin this in Shell:"
                            echo "   <internal-index-base>/${LIB_NAME}/${wheelName}"
                            echo "   sha256=\$STORED"
                            echo "=============================================================="
                        """,
                        label: 'Digest the stored artifact'
                    )
                }
                archiveArtifacts artifacts: 'published.sha256', fingerprint: true
                echo "✅ Published ${LIB_NAME} ${publishVersion}"
            }
        }
    }
}
