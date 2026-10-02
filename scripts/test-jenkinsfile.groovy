// Run with: groovy scripts/test-jenkinsfile.groovy [Jenkinsfile]
// Compiles and executes the real scripted pipeline with a deliberately small
// Jenkins DSL stub. No shell script is executed except through bash -n.
import org.codehaus.groovy.control.CompilerConfiguration

abstract class PublicationPipelineStub extends Script {
    Map scenario
    List<Map> shells = []
    List<String> stages = []
    List<String> credentialStages = []
    List<Map> images = []
    List<Map> archived = []
    List<Map> jobProperties = []
    String currentStage

    def getEnv() { [BRANCH_NAME: scenario.branch, CHANGE_ID: scenario.changeId] }
    def getParams() { scenario.containsKey('release') ? [PUBLISH_RELEASE: scenario.release] : [:] }
    def getScm() { 'scm' }
    def getPOD_LABEL() { 'test-pod' }
    def properties(List props) { jobProperties.addAll(props) }
    def parameters(List params) { [parameters: params] }
    def booleanParam(Map param) { param }
    def podTemplate(Map cfg, Closure body) { body() }
    def containerTemplate(Map cfg) { images << cfg; cfg }
    def node(label, Closure body) { body() }
    def container(String name, Closure body) { body() }
    def stage(String name, Closure body) { currentStage = name; stages << name; body() }
    def withCredentials(List creds, Closure body) {
        assert creds*.credentialsId == ['ci-user']
        credentialStages << currentStage
        body()
    }
    def archiveArtifacts(Map cfg) { archived << cfg }
    def checkout(scm) { [:] }
    def echo(message) { }
    def error(String message) { throw new IllegalStateException(message) }

    def sh(Map step) {
        shells << step
        if (step.returnStatus) return scenario.exists ? 0 : 1
        if (step.returnStdout) {
            if (step.script.contains('rev-parse')) return 'abc1234\n'
            if (step.label == 'Read declared version') return '0.2.2.4\n'
            if (step.label == 'Record built digest') return ('d' * 64) + '\n'
            throw new AssertionError("Unexpected stdout command: ${step}")
        }
        return 0
    }
}

File pipeline = new File(args ? args[0] : 'Jenkinsfile')
def config = new CompilerConfiguration(scriptBaseClass: 'PublicationPipelineStub')
def cases = [
    [branch: 'main', exists: true], // First run, parameter not installed yet.
    [branch: 'main', release: false, exists: true], // Non-release merge after publication.
    [branch: 'main', release: true, exists: false, publishes: true],
    [branch: 'main', release: true, exists: true, failure: 'already published'],
    [branch: 'PR-7', release: false],
    [branch: 'PR-7', release: true], // A PR cannot opt into publication.
    [branch: 'main', changeId: '7', release: true], // CHANGE_ID also excludes main.
    // Any other ref builds and verifies a local version and never publishes,
    // even when a release is requested: stable publication is main-only.
    [branch: 'release-readback-awscli', version: '0.2.2.4+branch.release.readback.awscli.abc1234'],
    [branch: 'release-readback-awscli', release: false, version: '0.2.2.4+branch.release.readback.awscli.abc1234'],
    [branch: 'feature/nope', release: true, version: '0.2.2.4+branch.feature.nope.abc1234'],
    [branch: 'v0.2.2.4', release: true, version: '0.2.2.4+branch.v0.2.2.4.abc1234'],
    [branch: 'Fix/007__Hot-0', version: '0.2.2.4+branch.fix.7.hot.0.abc1234'],
    // Mutation: if main-only gating were ever lost, the publish step still
    // refuses a non-stable version.
    [branch: 'feature/nope', release: true, exists: false, mutate: true, failure: 'Refusing to publish'],
]
int checkedShells = 0
cases.each { scenario ->
    String text = pipeline.text
    if (scenario.mutate) {
        String gate = 'boolean publishes = isMain && params.PUBLISH_RELEASE == true'
        assert text.contains(gate)
        text = text.replace(gate, 'boolean publishes = params.PUBLISH_RELEASE == true')
    }
    def script = new GroovyShell(this.class.classLoader, new Binding(), config).parse(text)
    script.setScenario(scenario)
    String failure = null
    try {
        script.run()
    } catch (IllegalStateException ex) {
        failure = ex.message
    }
    if (scenario.failure) {
        assert failure?.contains(scenario.failure): "${scenario}: ${failure}"
    } else {
        assert failure == null: "${scenario}: ${failure}"
        assert script.stages.containsAll(['Tests', 'Build and verify reproducibility', 'Verify installable artifact'])
    }
    assert script.jobProperties[0].parameters[0].name == 'PUBLISH_RELEASE'
    assert script.jobProperties[0].parameters[0].defaultValue == false
    assert script.images.find { it.name == 'ci' }.image ==~ /preset\/ci@sha256:[0-9a-f]{64}/
    if (scenario.publishes) {
        assert script.credentialStages == ['Reject an already-published version', 'Publish wheel']
        def upload = script.shells.find { it.label == 'Upload wheel (no-overwrite)' }.script
        assert upload.contains('IfNoneMatch="*"')
        assert upload.contains('KEY=\'flightsql-dbapi/flightsql_dbapi-0.2.2.4-py3-none-any.whl\'')
        assert !upload.contains('+')
        assert upload.contains('--target /tmp/publish-deps')
        assert upload.contains('PYTHONPATH=/tmp/publish-deps')
        // boto3 must never be installed into the interpreter the AWS CLI uses.
        assert !script.shells.any { it.script =~ /pip install[^\n]*boto3/ && !it.script.contains('--target') }
        def readback = script.shells.find { it.label == 'Digest the stored artifact' }.script
        assert readback.contains('aws s3api get-object')
        assert readback.contains('scripts/verify-stored-wheel stored.whl \'flightsql_dbapi-0.2.2.4-py3-none-any.whl\' \'0.2.2.4\'')
        assert script.archived*.artifacts == ['published.sha256']
    } else if (scenario.failure == 'already published') {
        assert script.credentialStages == ['Reject an already-published version']
        assert !script.shells.any { it.label == 'Upload wheel (no-overwrite)' }
    } else if (scenario.mutate) {
        assert !script.credentialStages.contains('Publish wheel')
        assert !script.shells.any { it.label == 'Upload wheel (no-overwrite)' || it.script.contains('put_object(') }
        assert script.archived.empty
    } else {
        assert script.credentialStages.empty: scenario
        assert !script.shells.any { it.script.contains('aws s3api') || it.script.contains('put_object(') }
        assert script.archived.empty
    }
    if (scenario.branch.startsWith('PR-') || scenario.changeId) {
        def install = script.shells.find { it.label == 'Install and inspect artifact' }.script
        assert install.contains('0.2.2.4+')
    }
    if (scenario.version) {
        def install = script.shells.find { it.label == 'Install and inspect artifact' }.script
        assert install.contains("assert dist.version == \"${scenario.version}\"")
        assert install.contains("upload/flightsql_dbapi-${scenario.version}-py3-none-any.whl")
        def apply = script.shells.find { it.label == 'Apply local test version' }.script
        assert apply.contains("version = \"${scenario.version}\"")
    }
    if (scenario.branch == 'main' && !scenario.changeId && !scenario.failure) {
        def install = script.shells.find { it.label == 'Install and inspect artifact' }.script
        assert install.contains('assert dist.version == "0.2.2.4"')
        assert !script.shells.any { it.label == 'Apply local test version' }
    }
    script.shells.each { step ->
        def process = new ProcessBuilder('bash', '-n').start()
        process.outputStream.withWriter { it << step.script }
        String stderr = process.errorStream.text
        assert process.waitFor() == 0: "${scenario}: ${step.label}: ${stderr}"
        checkedShells++
    }
    println "PASS ${scenario}"
}
println "PASS ${cases.size()} pipeline scenarios; ${checkedShells} generated shell scripts pass bash -n"
