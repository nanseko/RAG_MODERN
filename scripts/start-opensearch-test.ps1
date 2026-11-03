param(
    [int]$Port = 19200,
    [string]$OpenSearchHome = (Join-Path $PSScriptRoot '..\vendor\runtime\opensearch-3.9.0')
)
$ErrorActionPreference = 'Stop'
if ($Port -lt 1024 -or $Port -gt 65535) { throw 'Test port must be 1024..65535' }
$OpenSearchHome = [IO.Path]::GetFullPath($OpenSearchHome)
$launcher = Join-Path $OpenSearchHome 'bin\opensearch.bat'
if (-not (Test-Path -LiteralPath $launcher)) { throw 'Extract the verified native OpenSearch archive under vendor/runtime first' }
$probe = [Net.Sockets.TcpClient]::new()
try {
    $connected = $probe.ConnectAsync('127.0.0.1', $Port).Wait(300)
    if ($connected -and $probe.Connected) { throw 'Test port is occupied; existing process was left unchanged' }
} catch [AggregateException] { } finally { $probe.Dispose() }
$testRoot = [IO.Path]::GetFullPath((Join-Path ([IO.Path]::GetTempPath()) ('rag-opensearch-contract-' + [guid]::NewGuid().ToString('N'))))
$config = Join-Path $testRoot 'config'
[IO.Directory]::CreateDirectory($config) | Out-Null
[IO.Directory]::CreateDirectory((Join-Path $testRoot 'data')) | Out-Null
[IO.Directory]::CreateDirectory((Join-Path $testRoot 'logs')) | Out-Null
[IO.Directory]::CreateDirectory((Join-Path $testRoot 'tmp')) | Out-Null
Copy-Item -LiteralPath (Join-Path $OpenSearchHome 'config\jvm.options') -Destination (Join-Path $config 'jvm.options')
Copy-Item -LiteralPath (Join-Path $OpenSearchHome 'config\log4j2.properties') -Destination (Join-Path $config 'log4j2.properties')
$yaml = @"
cluster.name: rag-contract-$Port
node.name: rag-contract-node
network.host: 127.0.0.1
http.port: $Port
transport.port: $($Port + 100)
discovery.type: single-node
path.data: '$($testRoot.Replace('\', '/'))/data'
path.logs: '$($testRoot.Replace('\', '/'))/logs'
plugins.security.disabled: true
"@
[IO.File]::WriteAllText((Join-Path $config 'opensearch.yml'), $yaml, [Text.UTF8Encoding]::new($false))
$env:OPENSEARCH_PATH_CONF = $config
$env:OPENSEARCH_JAVA_HOME = Join-Path $OpenSearchHome 'jdk'
$env:OPENSEARCH_JAVA_OPTS = '-Xms512m -Xmx512m'
$env:OPENSEARCH_TMPDIR = Join-Path $testRoot 'tmp'
$pidFile = Join-Path $testRoot 'opensearch.pid'
$stdout = Join-Path $testRoot 'stdout.log'
$stderr = Join-Path $testRoot 'stderr.log'
$wrapper = Start-Process -FilePath $env:ComSpec -ArgumentList @('/d', '/s', '/c', ('""' + $launcher + '" -s -p "' + $pidFile + '""')) -WorkingDirectory $OpenSearchHome -WindowStyle Hidden -RedirectStandardOutput $stdout -RedirectStandardError $stderr -PassThru
$result = [ordered]@{ url="http://127.0.0.1:$Port"; wrapper_pid=$wrapper.Id; pid_file=$pidFile; state_directory=$testRoot; stdout=$stdout; stderr=$stderr; configuration=$config }
[IO.File]::WriteAllText((Join-Path $testRoot 'runtime.json'), ($result | ConvertTo-Json), [Text.UTF8Encoding]::new($false))
$result | ConvertTo-Json