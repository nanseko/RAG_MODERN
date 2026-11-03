[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][ValidateSet('init','start','status','stop')][string]$Action,
    [Parameter(Mandatory=$true)][ValidateSet('postgres','opensearch')][string]$Service,
    [string]$StateRoot = (Join-Path $PSScriptRoot '..\data\windows-services'),
    [ValidateRange(1024,65535)][int]$Port = 0,
    [ValidatePattern('^[a-zA-Z_][a-zA-Z0-9_]{0,62}$')][string]$PostgresUser = 'rag',
    [switch]$LocalOnly
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$Service = $Service.ToLowerInvariant()
$Action = $Action.ToLowerInvariant()
$project = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..')).TrimEnd('\','/')
$StateRoot = [IO.Path]::GetFullPath($StateRoot).TrimEnd('\','/')
if (-not $StateRoot.StartsWith($project + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'StateRoot must be inside the project; existing external services are left unchanged'
}
$ancestor = $StateRoot
while ($ancestor.Length -gt $project.Length) {
    if (Test-Path -LiteralPath $ancestor) {
        if ((Get-Item -LiteralPath $ancestor -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
            throw 'StateRoot must not traverse a junction or symbolic link'
        }
    }
    $ancestor = Split-Path -Parent $ancestor
}
if ($Port -eq 0) { if ($Service -eq 'postgres') { $Port = 5432 } else { $Port = 9200 } }
$serviceRoot = Join-Path $StateRoot $Service
foreach ($relative in @('', 'data', 'config', 'logs', 'tmp', 'service.json', 'server.log', 'opensearch.pid')) {
    $candidate = $serviceRoot
    if ($relative) { $candidate = Join-Path $serviceRoot $relative }
    if (Test-Path -LiteralPath $candidate) {
        if ((Get-Item -LiteralPath $candidate -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
            throw 'Service state must not traverse a junction or symbolic link'
        }
    }
}
$data = Join-Path $serviceRoot 'data'
$marker = Join-Path $serviceRoot 'service.json'
$log = Join-Path $serviceRoot 'server.log'
$pidFile = Join-Path $serviceRoot 'opensearch.pid'
$utf8 = [Text.UTF8Encoding]::new($false)

function Write-JsonFile($Path, $Value) {
    [IO.File]::WriteAllText($Path, ($Value | ConvertTo-Json -Depth 5), $utf8)
}
function Invoke-Native($Exe, $Arguments, [int]$Seconds=30) {
    $outFile = Join-Path $serviceRoot ('command-' + [guid]::NewGuid().ToString('N') + '.out')
    $errFile = $outFile + '.err'
    # Disk redirection is intentional: pg_ctl children may inherit pipe handles on Windows.
    $process = Start-Process -FilePath $Exe -ArgumentList $Arguments -WindowStyle Hidden -RedirectStandardOutput $outFile -RedirectStandardError $errFile -PassThru
    try {
        if (-not $process.WaitForExit($Seconds * 1000)) {
            $process.Kill()
            throw "Native command timed out; inspect $log (no data was reset)"
        }
        $process.Refresh()
        $output = ([IO.File]::ReadAllText($outFile) + [IO.File]::ReadAllText($errFile)).Trim()
        return @{ Code=$process.ExitCode; Output=$output }
    } finally {
        Remove-Item -LiteralPath $outFile,$errFile -Force -ErrorAction SilentlyContinue
    }
}
function Assert-PortFree {
    $listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, $Port)
    try { $listener.Start() } catch { throw "Port $Port is occupied; existing process was left unchanged" } finally { $listener.Stop() }
}
function Read-Marker {
    if (-not (Test-Path -LiteralPath $marker)) { throw "Service is not initialized. Run -Action init -Service $Service first" }
    $saved = Get-Content -LiteralPath $marker -Raw | ConvertFrom-Json
    if ($saved.service -ne $Service -or $saved.state_directory -ne $serviceRoot) { throw 'Service identity does not match this project state directory' }
    return $saved
}
function Get-OwnedPostgres {
    $postgresPidFile = Join-Path $data 'postmaster.pid'
    if (-not (Test-Path -LiteralPath $postgresPidFile)) { return $null }
    $serverPid = 0
    $lines = [IO.File]::ReadAllLines($postgresPidFile)
    if ($lines.Count -eq 0 -or -not [int]::TryParse($lines[0].Trim(), [ref]$serverPid)) { throw 'Invalid PostgreSQL PID file; no process stopped' }
    $process = Get-CimInstance Win32_Process -Filter "ProcessId=$serverPid"
    if ($null -eq $process) { return $null }
    $normalized = ([string]$process.CommandLine).Replace('\','/')
    $expected = $data.Replace('\','/')
    if ($process.Name -ne 'postgres.exe' -or $normalized.IndexOf($expected,[StringComparison]::OrdinalIgnoreCase) -lt 0) {
        throw 'PostgreSQL PID identity mismatch; no process stopped'
    }
    return $process
}
function Get-OwnedOpenSearch {
    if (-not (Test-Path -LiteralPath $pidFile)) { return $null }
    $serverPid = 0
    if (-not [int]::TryParse(([IO.File]::ReadAllText($pidFile)).Trim(), [ref]$serverPid)) { throw 'Invalid OpenSearch PID file; no process stopped' }
    $process = Get-CimInstance Win32_Process -Filter "ProcessId=$serverPid"
    if ($null -eq $process) { return $null }
    $normalized = $process.CommandLine.Replace('\','/')
    $expected = (Join-Path $serviceRoot 'config').Replace('\','/')
    if ($process.Name -ne 'java.exe' -or -not $normalized.Contains($expected) -or -not $normalized.Contains('org.opensearch.bootstrap.OpenSearch')) {
        throw 'OpenSearch PID identity mismatch; no process stopped'
    }
    return $process
}
function Emit-State($State, $ServerPid=$null) {
    $result = [ordered]@{service=$Service; state=$State; host='127.0.0.1'; port=$Port; state_directory=$serviceRoot; pid=$ServerPid; log=$log}
    if ($Service -eq 'opensearch') { $result.url="http://127.0.0.1:$Port"; $result.pid_file=$pidFile; $result.profile='loopback-local-only' }
    $result | ConvertTo-Json
}

if ($Action -eq 'status' -and -not (Test-Path -LiteralPath $marker)) { Emit-State 'not_initialized'; exit 0 }
if ($Action -eq 'init') {
    if (Test-Path -LiteralPath $serviceRoot) {
        if (@(Get-ChildItem -LiteralPath $serviceRoot -Force).Count -gt 0) { throw 'Service directory is not empty; initialization never overwrites existing data' }
    }
    if ($Service -eq 'postgres') {
        $bin = Join-Path $project 'vendor\runtime\postgresql18\pgsql\bin'
        $initdb = Join-Path $bin 'initdb.exe'
        if ([string]::IsNullOrWhiteSpace($env:RAG_POSTGRES_PASSWORD)) { throw 'Set RAG_POSTGRES_PASSWORD before PostgreSQL initialization; it is never printed or stored in service metadata' }
        if ($env:RAG_POSTGRES_PASSWORD.Length -lt 12 -or $env:RAG_POSTGRES_PASSWORD -match '[\r\n]') { throw 'RAG_POSTGRES_PASSWORD must contain at least 12 characters and no newlines' }
        if (-not (Test-Path -LiteralPath $initdb)) { throw 'Bundled native PostgreSQL runtime is missing' }
        [IO.Directory]::CreateDirectory($serviceRoot) | Out-Null
        $passwordFile = Join-Path $serviceRoot ('.password-' + [guid]::NewGuid().ToString('N'))
        try {
            [IO.File]::WriteAllText($passwordFile, '', $utf8)
            $acl = [Security.AccessControl.FileSecurity]::new()
            $acl.SetAccessRuleProtection($true,$false)
            $identity = [Security.Principal.WindowsIdentity]::GetCurrent().User
            $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($identity,'FullControl','Allow'))
            Set-Acl -LiteralPath $passwordFile -AclObject $acl
            [IO.File]::WriteAllText($passwordFile, $env:RAG_POSTGRES_PASSWORD, $utf8)
            $result = Invoke-Native $initdb @('-D', ('"'+$data+'"'), '-U', $PostgresUser, '--auth=scram-sha-256', '--encoding=UTF8', '--no-locale', ('--pwfile="'+$passwordFile+'"')) 90
            if ($result.Code -ne 0) { throw "initdb failed: $($result.Output)" }
        } finally { Remove-Item -LiteralPath $passwordFile -Force -ErrorAction SilentlyContinue }
        Write-JsonFile $marker @{service=$Service; state_directory=$serviceRoot; port=$Port; user=$PostgresUser; database='postgres'; auth='scram-sha-256'}
    } else {
        if (-not $LocalOnly) { throw 'OpenSearch init requires -LocalOnly for its explicit loopback-only profile' }
        if ($Port -gt 65435) { throw 'OpenSearch port must leave room for the transport port (+100)' }
        $openSearchRuntime = Join-Path $project 'vendor\runtime\opensearch-3.9.0'
        if (-not (Test-Path -LiteralPath (Join-Path $openSearchRuntime 'bin\opensearch.bat'))) { throw 'Bundled native OpenSearch runtime is missing' }
        $config = Join-Path $serviceRoot 'config'
        foreach ($directory in @($config,$data,(Join-Path $serviceRoot 'logs'),(Join-Path $serviceRoot 'tmp'))) { [IO.Directory]::CreateDirectory($directory) | Out-Null }
        foreach ($name in @('jvm.options','log4j2.properties')) { Copy-Item -LiteralPath (Join-Path $openSearchRuntime "config\$name") -Destination (Join-Path $config $name) }
        $yaml = @"
cluster.name: rag-windows-local
node.name: rag-windows-node
network.host: 127.0.0.1
http.port: $Port
transport.port: $($Port + 100)
discovery.type: single-node
path.data: '$($data.Replace('\','/'))'
path.logs: '$((Join-Path $serviceRoot 'logs').Replace('\','/'))'
plugins.security.disabled: true
"@
        [IO.File]::WriteAllText((Join-Path $config 'opensearch.yml'),$yaml,$utf8)
        Write-JsonFile $marker @{service=$Service; state_directory=$serviceRoot; port=$Port; profile='loopback-local-only'}
    }
    Emit-State 'initialized'; exit 0
}
$saved = Read-Marker
# An initialized service keeps its original port; changing ports requires editing its own configuration deliberately.
$Port = [int]$saved.port
if ($Service -eq 'postgres') {
    $pgctl = Join-Path $project 'vendor\runtime\postgresql18\pgsql\bin\pg_ctl.exe'
    $ownedPostgres = Get-OwnedPostgres
    if ($Action -eq 'status') {
        if ($null -ne $ownedPostgres) { Emit-State 'running' $ownedPostgres.ProcessId } else { Emit-State 'stopped' }
        exit 0
    }
    if ($Action -eq 'stop') {
        if ($null -ne $ownedPostgres) {
            $result = Invoke-Native $pgctl @('-D', ('"'+$data+'"'), '-m','fast','-w','-t','20','stop') 25
            if ($result.Code -ne 0) { throw "PostgreSQL stop failed: $($result.Output)" }
        }
        Emit-State 'stopped'; exit 0
    }
    if ($null -eq $ownedPostgres) {
        Assert-PortFree
        $result = Invoke-Native $pgctl @('-D', ('"'+$data+'"'), '-l', ('"'+$log+'"'), '-o', ('"-h 127.0.0.1 -p '+$Port+' -c password_encryption=scram-sha-256"'), '-w','-t','30','start') 35
        if ($result.Code -ne 0) { throw "PostgreSQL start failed: $($result.Output). Inspect $log" }
    }
    Emit-State 'running' ([int]([IO.File]::ReadAllLines((Join-Path $data 'postmaster.pid'))[0])); exit 0
}
$owned = Get-OwnedOpenSearch
if ($Action -eq 'status') { if ($null -eq $owned) { Emit-State 'stopped' } else { Emit-State 'running' $owned.ProcessId }; exit 0 }
if ($Action -eq 'stop') {
    if ($null -ne $owned) { Stop-Process -Id $owned.ProcessId }
    Emit-State 'stopped'; exit 0
}
if (-not $LocalOnly) { throw 'OpenSearch start requires -LocalOnly; this profile is restricted to trusted local Windows users' }
if ($null -eq $owned) {
    Assert-PortFree
    $openSearchRuntime = Join-Path $project 'vendor\runtime\opensearch-3.9.0'
    $launcher = Join-Path $openSearchRuntime 'bin\opensearch.bat'
    $env:OPENSEARCH_PATH_CONF = Join-Path $serviceRoot 'config'
    $env:OPENSEARCH_JAVA_HOME = Join-Path $openSearchRuntime 'jdk'
    $env:OPENSEARCH_JAVA_OPTS = '-Xms512m -Xmx512m'
    $env:OPENSEARCH_TMPDIR = Join-Path $serviceRoot 'tmp'
    $wrapper = Start-Process -FilePath $env:ComSpec -ArgumentList @('/d','/s','/c', ('""'+$launcher+'" -s -p "'+$pidFile+'""')) -WorkingDirectory $openSearchRuntime -WindowStyle Hidden -RedirectStandardOutput $log -RedirectStandardError ($log+'.err') -PassThru
    Write-JsonFile (Join-Path $serviceRoot 'launch.json') @{wrapper_pid=$wrapper.Id; state_directory=$serviceRoot; pid_file=$pidFile}
}
$deadline = [DateTime]::UtcNow.AddSeconds(60)
do {
    try {
        $health = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/_cluster/health" -TimeoutSec 2
        $owned = Get-OwnedOpenSearch
        if ($null -ne $owned -and $health.status -in @('green','yellow')) { Emit-State 'running' $owned.ProcessId; exit 0 }
    } catch { }
    Start-Sleep -Milliseconds 500
} while ([DateTime]::UtcNow -lt $deadline)
throw "OpenSearch did not become healthy within 60 seconds; inspect $log and logs under $serviceRoot. No data was reset; use -Action status/stop to inspect or stop the owned process."

