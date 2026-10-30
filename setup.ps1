# Installs LectureLab for this Windows user, or repairs an install. Safe to run again: your
# recordings, notes, settings and .env are never touched. Run it through setup.cmd.
#
#   -InstallDir    where the app goes (default: %LOCALAPPDATA%\Programs\LectureLab)
#   -Yes           install missing tools without asking (LibreOffice is still asked about)
#   -NoTasks       copy and set up only: no background tasks, desktop shortcut or window (for testing)
#   -ReplaceTasks  point the background tasks at this copy even if another copy owns them
#
# Plain ASCII on purpose: Windows PowerShell 5.1 reads a file without a BOM as ANSI.
param(
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA 'Programs\LectureLab'),
    [switch]$Yes,
    [switch]$NoTasks,
    [switch]$ReplaceTasks
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$Log = Join-Path $env:TEMP 'lecturelab-setup.log'
Start-Transcript -Path $Log -Force | Out-Null
$script:Stopped = $false  # set once setup has stopped a running LectureLab
$Port = if ($env:LECTURELAB_PORT) { $env:LECTURELAB_PORT } else { '8765' }

function Say($text) { Write-Host $text }
function Step($text) { Write-Host ''; Write-Host "== $text" -ForegroundColor Cyan }
function Fail($text) {
    Write-Host ''; Write-Host $text -ForegroundColor Red
    if ($script:Stopped) {  # don't leave LectureLab down because setup failed halfway
        Write-Host 'Starting LectureLab again the way it was.'
        Start-ScheduledTask -TaskName 'LectureLab Server' -ErrorAction SilentlyContinue
    }
    Stop-Transcript | Out-Null
    exit 1
}
function Ask($question, [bool]$default) {
    $hint = if ($default) { '[Y/n]' } else { '[y/N]' }
    $a = Read-Host "$question $hint"
    if ([string]::IsNullOrWhiteSpace($a)) { return $default }
    return $a.Trim().ToLower().StartsWith('y')
}
function Native([scriptblock]$cmd) {
    # In PowerShell 5.1 a native command's stderr is a terminating error while ErrorActionPreference
    # is Stop, so `py -3.13` saying "not found" would end setup. Callers read $LASTEXITCODE instead.
    $old = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    try { & $cmd } finally { $ErrorActionPreference = $old }
}
function Run($what, [scriptblock]$cmd) {
    Native $cmd
    if ($LASTEXITCODE -ne 0) { Fail "$what failed (exit code $LASTEXITCODE). The full log is in $Log." }
}
function Update-Path {  # winget adds to PATH for new windows only; this window needs it now
    $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' + [Environment]::GetEnvironmentVariable('Path', 'User')
}
function Same-Path($a, $b) {  # after resolving short names and links, as Python's resolve() does
    try { $a = (Resolve-Path -LiteralPath $a).Path } catch { }
    try { $b = (Resolve-Path -LiteralPath $b).Path } catch { }
    return "$a".TrimEnd('\') -eq "$b".TrimEnd('\')
}

function Test-Python($exe) {  # 64-bit 3.13: onnxruntime and ctranslate2 have no 32-bit wheels
    if (-not $exe -or -not (Test-Path -LiteralPath $exe)) { return $false }
    Native { & $exe -c "import struct, sys; sys.exit(0 if sys.version_info[:2] == (3, 13) and struct.calcsize('P') == 8 else 1)" 2>$null } | Out-Null
    return $LASTEXITCODE -eq 0
}
function Find-Python {
    # Where python.org and winget put it, before asking py: a newer py may offer to download Python and wait.
    foreach ($c in @("$env:LOCALAPPDATA\Programs\Python\Python313\python.exe", "$env:ProgramFiles\Python313\python.exe")) {
        if (Test-Python $c) { return $c }
    }
    if (-not (Get-Command py -ErrorAction SilentlyContinue)) { return $null }
    $enc = [Console]::OutputEncoding
    try {  # so a path like C:\Users\Jose\... with an accent comes back intact
        [Console]::OutputEncoding = [Text.Encoding]::UTF8; $env:PYTHONIOENCODING = 'utf-8'
        $p = Native { '' | & py -3.13 -c "import sys; print(sys.executable)" 2>$null }
    } finally { [Console]::OutputEncoding = $enc; Remove-Item Env:PYTHONIOENCODING -ErrorAction SilentlyContinue }
    if ($LASTEXITCODE -ne 0 -or -not $p) { return $null }
    $p = "$($p | Select-Object -Last 1)".Trim()
    if (Test-Python $p) { return $p }
    return $null
}
function Find-Ffmpeg {  # the same places lecturelab.find_tool looks
    if (Get-Command ffmpeg -ErrorAction SilentlyContinue) { return $true }
    return [bool](Get-ChildItem "$env:LOCALAPPDATA\Microsoft\WinGet\Packages\Gyan.FFmpeg*\*\bin\ffmpeg.exe" -ErrorAction SilentlyContinue)
}
function Find-LibreOffice {
    return [bool]((Get-Command soffice -ErrorAction SilentlyContinue) -or (Test-Path -LiteralPath "$env:ProgramFiles\LibreOffice\program\soffice.exe"))
}
function Winget-Install($id, $name, [string[]]$extra = @()) {
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        Fail "winget isn't available, so $name can't be installed. Install 'App Installer' from the Microsoft Store, then run setup.cmd again."
    }
    Say "Installing $name with winget..."
    Native { & winget install --id $id -e --source winget --silent --accept-source-agreements --accept-package-agreements @extra }
    Update-Path  # the exit code isn't trusted ("already installed" is an error too): the caller looks again
}
function Task-Owner {  # which cli.py the server task runs, or $null when there's no task
    $t = Get-ScheduledTask -TaskName 'LectureLab Server' -ErrorAction SilentlyContinue
    if (-not $t) { return $null }
    if ($t.Actions[0].Arguments -match '"([^"]+cli\.py)"') { return $Matches[1] }
    return $t.Actions[0].Arguments
}
function Live-Root {  # folder of the LectureLab answering on the port: $null for none, '' for one too old to say
    try { $h = Invoke-RestMethod "http://127.0.0.1:$Port/api/health" -TimeoutSec 3 } catch { return $null }
    if ($h.root) { return [string]$h.root }
    return ''
}

$Src = $PSScriptRoot
if (-not [IO.Path]::IsPathRooted($InstallDir)) { $InstallDir = Join-Path (Get-Location).Path $InstallDir }
$InstallDir = [IO.Path]::GetFullPath($InstallDir)
if ($InstallDir.Length -gt 3) { $InstallDir = $InstallDir.TrimEnd('\') }  # a trailing \ would escape the closing quote in pip's argument
Say "LectureLab setup. Installing to $InstallDir"
# Only inside your own user folders, where other accounts on this PC can't change the code that runs
# as you at logon; and only into an empty folder or an earlier LectureLab, because setup mirrors
# and clears folders inside it. (A managed PC may keep AppData apart from the profile.)
$roots = @($env:USERPROFILE, $env:LOCALAPPDATA) | Where-Object { $_ } | ForEach-Object { [IO.Path]::GetFullPath($_).TrimEnd('\') }
if (-not ($roots | Where-Object { $InstallDir.StartsWith($_ + '\', [StringComparison]::OrdinalIgnoreCase) })) {
    Fail "Install LectureLab inside your user folder ($($roots[0])), not $InstallDir. Other accounts on this PC could change files outside it."
}
$earlier = @('lecturelab.py', 'cli.py', 'pyproject.toml') | Where-Object { -not (Test-Path -LiteralPath (Join-Path $InstallDir $_)) }
if ((Test-Path -LiteralPath $InstallDir) -and $earlier -and
        (Get-ChildItem -LiteralPath $InstallDir -Force | Select-Object -First 1)) {
    Fail "$InstallDir already has other files in it. Pick an empty folder, or leave out -InstallDir to use the default."
}

# ---- what this computer has
Step 'Checking this computer'
if (-not (Test-Path -LiteralPath (Join-Path $Src 'lecturelab.py'))) {
    Fail "setup.cmd has to run from the folder it came in. Extract the whole ZIP first (right-click it, Extract All), then run setup.cmd from the extracted folder."
}
$build = [Environment]::OSVersion.Version.Build
if ($build -lt 22000) {
    Say "This is Windows build $build. LectureLab is tested on Windows 11 (build 22000 or later)."
    if (-not (Ask 'Continue anyway?' $false)) { Fail 'Setup stopped.' }
}
try {
    $drive = Get-PSDrive -Name ([IO.Path]::GetPathRoot($InstallDir).Substring(0, 1))
    $freeGb = [math]::Round($drive.Free / 1GB, 1)
    if ($freeGb -lt 5) { Say "Only $freeGb GB free on drive $($drive.Name). The app needs about 3 GB, plus room for recordings." }
} catch { }  # a network path has no drive letter to check

$python = Find-Python
$ffmpeg = Find-Ffmpeg
$office = Find-LibreOffice
# The way the app looks for it: claude.exe itself, since a .cmd shim (an npm install) mangles its prompt
$claude = (Test-Path -LiteralPath "$env:USERPROFILE\.local\bin\claude.exe") -or [bool](Get-Command claude.exe -ErrorAction SilentlyContinue)
Say ("  Python 3.13   " + $(if ($python) { "found ($python)" } else { 'missing' }))
Say ("  ffmpeg        " + $(if ($ffmpeg) { 'found' } else { 'missing' }))
Say ("  LibreOffice   " + $(if ($office) { 'found' } else { 'missing (only needed for PowerPoint slides)' }))
Say ("  Claude Code   " + $(if ($claude) { 'found' } else { 'missing (needed for notes)' }))

# ---- tools, with winget, after asking
$need = @()
if (-not $python) { $need += 'Python 3.13' }
if (-not $ffmpeg) { $need += 'ffmpeg' }
if ($need.Count) {
    Step 'Installing tools'
    if (-not ($Yes -or (Ask "Install $($need -join ' and ') with winget?" $true))) {
        Fail "LectureLab needs $($need -join ' and '). Install them yourself, or run setup.cmd again and answer yes."
    }
    if (-not $python) { Winget-Install 'Python.Python.3.13' 'Python 3.13' @('--scope', 'user'); $python = Find-Python }
    if (-not $ffmpeg) { Winget-Install 'Gyan.FFmpeg' 'ffmpeg'; $ffmpeg = Find-Ffmpeg }
    if (-not $python) { Fail "Python 3.13 (64-bit) still isn't found. Install it from python.org (3.13, not 3.14), then run setup.cmd again." }
    if (-not $ffmpeg) { Fail "ffmpeg still isn't found. Run 'winget install Gyan.FFmpeg' yourself, then run setup.cmd again." }
}
if (-not $office -and (Ask 'Install LibreOffice too? It is about 350 MB and only needed for PowerPoint slides (PDF works without it).' $false)) {
    Winget-Install 'TheDocumentFoundation.LibreOffice' 'LibreOffice'
    if (-not (Find-LibreOffice)) { Say 'LibreOffice still is not found. PDF slides work anyway; you can install it later.' }
}

# ---- whose background tasks are these, and what's running?
$cli = Join-Path $InstallDir 'cli.py'
$venvPy = Join-Path $InstallDir '.venv\Scripts\python.exe'
$owner = Task-Owner
$ours = [bool]($owner -and (Same-Path $owner $cli))
$doTasks = -not $NoTasks
$switch = $false
if ($doTasks -and $owner -and -not $ours) {
    if ($ReplaceTasks) { $switch = $true }
    else {
        Say ''
        Say "LectureLab's background tasks already run another copy: $owner"
        if (Ask "Switch them to the copy in $InstallDir? The other copy stops and won't start by itself any more." $false) { $switch = $true }
        else { $doTasks = $false; Say 'Leaving them alone. This copy gets set up, but not started.' }
    }
}
if ($doTasks) {
    # A running server keeps its packages' DLLs locked, and holds the port. Each copy stops its own,
    # with its own lecturelab stop, so nothing else on the port gets killed.
    $stopPy = $null; $stopCli = $null
    if ($switch) { $stopCli = $owner; $stopPy = Join-Path (Split-Path $owner) '.venv\Scripts\python.exe' }
    elseif ($ours) { $stopCli = $cli; $stopPy = $venvPy }
    if ($stopPy -and (Test-Path -LiteralPath $stopPy)) {
        Step 'Stopping the running copy'
        Native { & $stopPy $stopCli stop }
        if ($LASTEXITCODE -ne 0) { Fail "LectureLab in $(Split-Path $stopCli) didn't stop. Close it (or restart Windows), then run setup.cmd again." }
        $script:Stopped = $true
    }
    $live = Live-Root
    if ($null -ne $live -and -not ($live -and (Same-Path $live $InstallDir))) {
        $who = if ($live) { "from $live" } else { 'from an older copy' }
        Fail "Another LectureLab is running on port $Port $who. Stop it with 'lecturelab stop' in its folder, then run setup.cmd again."
    }
}

# ---- the app itself. data\ and .env are never copied over or deleted.
Step 'Copying the app'
if (Same-Path $Src $InstallDir) {
    Say 'Running from the installed copy: nothing to copy.'
} else {
    New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
    $files = @('lecturelab.py', 'cli.py', 'pyproject.toml', 'requirements.lock', 'README.md', 'LICENSE', '.env.example',
               'setup.ps1', 'setup.cmd')
    foreach ($f in $files) { Copy-Item -LiteralPath (Join-Path $Src $f) -Destination (Join-Path $InstallDir $f) -Force }
    foreach ($d in @('static', 'colab')) {  # mirrored: these folders hold only code
        Native { & robocopy (Join-Path $Src $d) (Join-Path $InstallDir $d) /MIR /NJH /NJS /NFL /NDL /NP } | Out-Null
        if ($LASTEXITCODE -ge 8) { Fail "Copying $d failed (robocopy exit code $LASTEXITCODE)." }
    }
    $global:LASTEXITCODE = 0
    # Downloaded files carry Windows' "came from the internet" mark. Only clear it on what was just copied, never data\.
    foreach ($f in $files) { Unblock-File -LiteralPath (Join-Path $InstallDir $f) -ErrorAction SilentlyContinue }
    Get-ChildItem -LiteralPath (Join-Path $InstallDir 'static'), (Join-Path $InstallDir 'colab') -Recurse -File | Unblock-File -ErrorAction SilentlyContinue
    Say "Copied. Your recordings, notes and settings in $InstallDir\data are kept."
}

# ---- its Python environment
Step 'Setting up Python packages (the first time takes a few minutes)'
$fresh = -not (Test-Python $venvPy)  # missing, the wrong Python, or broken (its base Python was uninstalled)
if ($fresh) { Run 'Creating the Python environment' { & $python -m venv --clear (Join-Path $InstallDir '.venv') } }
# The exact versions that were tested, each checked against its sha256 (requirements.lock), so a package
# replaced on PyPI after this release can't get in. Then LectureLab itself, which needs nothing more, and
# yt-dlp, which floats on purpose: sites change and old versions stop working.
$pip = @('-m', 'pip', 'install', '--disable-pip-version-check', '--quiet')
# Wheels only: building from source would fetch build tools that no hash covers.
Run 'Installing packages' { & $venvPy @pip --require-hashes --no-deps --only-binary=:all: -r (Join-Path $InstallDir 'requirements.lock') }
Run 'Installing LectureLab' { & $venvPy @pip --no-deps --no-build-isolation -e $InstallDir }
Run 'Installing yt-dlp' { & $venvPy @pip --upgrade --no-deps 'yt-dlp>=2026.10.26' }  # a new dependency fails pip check, not slips in
Run 'Checking the packages' { & $venvPy -m pip check }
Run "Installing LectureLab's browser (for Canvas)" { & $venvPy -m playwright install chromium }

# ---- background tasks, shortcut, first window
if ($doTasks) {
    Step 'Starting LectureLab'
    Run 'Registering the background tasks' { & $venvPy $cli install-tasks --start }
    $script:Stopped = $false  # running again, from this copy
    Native { & $venvPy $cli shortcut }
    if ($LASTEXITCODE -ne 0) { Say 'The desktop shortcut could not be made. LectureLab still starts when you log in; run setup.cmd again to retry it.' }
    Native { & $venvPy $cli open }
}

Step 'Done'
if (-not $claude) {
    Say 'Notes need Claude Code: install it from https://claude.com/claude-code, run "claude" once to sign in to your Claude plan, then press "Check Claude''s sign-in" on the Setup page.'
}
if ($doTasks) {
    Say 'LectureLab is open. It starts by itself when you log in; the desktop shortcut opens it.'
    Say 'Running setup.cmd again repairs it.'
} else {
    Say "Set up in $InstallDir without background tasks. Start it with: `"$venvPy`" `"$cli`" serve"
}
Stop-Transcript | Out-Null
exit 0
