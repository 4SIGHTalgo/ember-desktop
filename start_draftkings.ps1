# Advanced scripts reserve -EV as the ErrorVariable common-parameter alias.
# Standard script binding lets -EV name the numeric expected-return setting.
param(
    [switch]$BrowserOnly,
    [switch]$DeferNavigation,
    [string]$ProfileDirectory,
    [int]$Port = 9222,
    [switch]$FollowElite,
    [switch]$Headless,
    [switch]$ManualLogin,
    [string]$ResumeState,
    [ValidateRange(30, 2147483647)]
    [int]$MinPriorGames = 30,
    [double]$SplitThreshold = 0,
    [switch]$PlaceBet,
    # Optional dollar cap on half-Kelly stakes; zero means uncapped.
    [double]$Stake = 0,
    [switch]$NoStakeCap,
    [ValidateSet('accept', 'better', 'none')]
    [string]$Slippage = 'accept',
    [ValidateRange(0, 86400)]
    [int]$KeepAlive = 300,
    [ValidateScript({ (-not [double]::IsNaN($_)) -and (-not [double]::IsInfinity($_)) -and $_ -ge 0 })]
    [double]$EV = 17.5,
    [ValidateSet('conservative', 'point')]
    [string]$EVMode = 'conservative',
    [ValidateScript({ $_ -ge 50 -and $_ -lt 100 })]
    [double]$Confidence = 95,
    [ValidateScript({ (-not [double]::IsNaN($_)) -and (-not [double]::IsInfinity($_)) -and $_ -ge 0 })]
    [double]$Cost = 0
)

$ErrorActionPreference = 'Stop'

try {
    $pythonPath = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
    if (-not $BrowserOnly -and -not (Test-Path -LiteralPath $pythonPath)) {
        throw 'The project Python environment was not found.'
    }
    if (-not $BrowserOnly) {
        if ($ResumeState -and -not $FollowElite) {
            throw '-ResumeState requires -FollowElite.'
        }
        if ($FollowElite) {
            try { $Host.UI.RawUI.WindowTitle = 'SPORTSBOOK BOT - KEEP THIS TERMINAL OPEN' } catch { }
        }
        $loginArguments = @()
        if ($Headless) { $loginArguments += '--headless' }
        if ($ManualLogin) { $loginArguments += '--manual-login' }
        if ($ResumeState) { $loginArguments += @('--resume-state', $ResumeState) }
        if ($FollowElite) {
            $culture = [Globalization.CultureInfo]::InvariantCulture
            $loginArguments += @('--follow-elite', '--min-prior-games', $MinPriorGames)
            $loginArguments += @('--split-threshold', $SplitThreshold.ToString($culture),
                '--ev', $EV.ToString($culture), '--ev-mode', $EVMode, '--confidence', $Confidence.ToString($culture),
                '--cost', $Cost.ToString($culture))
            if ($PlaceBet) {
                $loginArguments += @('--place-bet', '--stake', $Stake.ToString($culture),
                    '--slippage', $Slippage, '--keep-alive', $KeepAlive)
                if ($NoStakeCap) { $loginArguments += '--no-stake-cap' }
            }
        }
        & $pythonPath (Join-Path $PSScriptRoot 'draftkings_login.py') @loginArguments
        exit $LASTEXITCODE
    }
    if (-not $ProfileDirectory) {
        $ProfileDirectory = Join-Path $PSScriptRoot '.chrome-profile'
    }
    $chromePath = Join-Path $env:ProgramFiles 'Google\Chrome\Application\chrome.exe'
    if (-not (Test-Path -LiteralPath $chromePath)) {
        $chromePath = Join-Path ${env:ProgramFiles(x86)} 'Google\Chrome\Application\chrome.exe'
    }
    if (-not (Test-Path -LiteralPath $chromePath)) {
        throw 'Chrome was not found in Program Files.'
    }
    $profilePath = [IO.Path]::GetFullPath($ProfileDirectory)
    if ($Port -lt 1024 -or $Port -gt 65535) { throw 'Invalid local debugging port.' }
    $listeners = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
    if ($listeners.Count) {
        if ($listeners | Where-Object { $_.LocalAddress -notin @('127.0.0.1', '::1') }) {
            throw 'The debugging port is not restricted to localhost; refusing to attach.'
        }
        $browserProcess = Get-CimInstance Win32_Process -Filter ('ProcessId={0}' -f $listeners[0].OwningProcess)
        $profileArgument = [regex]::Match($browserProcess.CommandLine, '--user-data-dir=(?:"([^"]+)"|(\S+))')
        $runningProfile = $profileArgument.Groups[1].Value
        if (-not $runningProfile) { $runningProfile = $profileArgument.Groups[2].Value }
        if ($browserProcess.Name -ne 'chrome.exe' -or -not $runningProfile -or
                [IO.Path]::GetFullPath($runningProfile) -ne $profilePath) {
            throw 'The debugging port belongs to a different browser profile; refusing to attach.'
        }
    }
    $initialUrl = if ($DeferNavigation) { 'about:blank' } else { 'https://sportsbook.draftkings.com/' }
    $chromeArguments = @(
        '--remote-debugging-address=127.0.0.1'
        ('--remote-debugging-port={0}' -f $Port)
        ('--user-data-dir="{0}"' -f $profilePath)
        '--profile-directory=Default'
        '--no-first-run'
        $initialUrl
    )
    if ($Headless) { $chromeArguments += @('--headless=new', '--window-size=1920,1080') }
    if (-not $listeners.Count) {
        Start-Process -FilePath $chromePath -ArgumentList $chromeArguments -WindowStyle Normal
    }

    $deadline = (Get-Date).AddSeconds(20)
    $browserReady = $false
    do {
        try {
            $version = Invoke-RestMethod -Uri ('http://127.0.0.1:{0}/json/version' -f $Port) -TimeoutSec 1
            $browserReady = $version.Browser -like 'Chrome/*'
        }
        catch {
            Start-Sleep -Milliseconds 250
        }
    } until ($browserReady -or (Get-Date) -ge $deadline)
    if (-not $browserReady) {
        throw 'Chrome did not open its debugging connection. Close the previous script Chrome window and try again.'
    }
    if ($BrowserOnly) {
        Write-Host ('Private Chrome is ready on localhost port {0}.' -f $Port)
        exit 0
    }

}
catch {
    Write-Host $_.Exception.Message -ForegroundColor Red
    if (-not $BrowserOnly) { Read-Host 'Press Enter to close this launcher' }
    exit 1
}
