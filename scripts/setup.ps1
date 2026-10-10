# One attended installation; account login and Funnel consent remain in the admin card.
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath (Split-Path -Parent $PSScriptRoot)
try {
    if (!(Test-Path -LiteralPath '.venv/Scripts/python.exe')) {
        $pythonCommand = Get-Command py -ErrorAction SilentlyContinue
        if (!$pythonCommand) {
            throw 'Install Python 3.13 from https://www.python.org/downloads/windows/ (include the Python launcher), then run setup.cmd again.'
        }
        & $pythonCommand.Source -3.13 -m venv .venv
        if ($LASTEXITCODE -ne 0) { throw 'Python 3.13 is required. Install it, then run setup.cmd again.' }
    }
    $projectPython = (Resolve-Path -LiteralPath '.venv/Scripts/python.exe').Path
    & $projectPython -m pip install -r requirements.lock
    if ($LASTEXITCODE -ne 0) { throw 'Could not install application dependencies. Check the connection and run setup.cmd again.' }
    $tailscalePath = Join-Path $env:ProgramFiles 'Tailscale/tailscale.exe'
    if (!(Test-Path -LiteralPath $tailscalePath) -and !(Get-Command tailscale -ErrorAction SilentlyContinue)) {
        $wingetCommand = Get-Command winget -ErrorAction SilentlyContinue
        if ($wingetCommand) {
            Write-Host 'Installing Tailscale. Allow the Windows permission prompt if shown.'
            & $wingetCommand.Source install --id Tailscale.Tailscale --exact --source winget --accept-package-agreements --accept-source-agreements
            if ($LASTEXITCODE -ne 0) { Write-Warning 'Tailscale installation did not complete. You can retry in the admin card.' }
        } else {
            Write-Warning 'Tailscale can be installed from the admin card or https://tailscale.com/download/windows.'
        }
    }
    $adminInitialized = & $projectPython -c 'from app.store import Store
s=Store()
with s.connect() as db:
    print(int(bool(s.get(db,"admin_password_hash",""))))'
    if ($LASTEXITCODE -ne 0) { throw 'Could not check the application database.' }
    if ($adminInitialized[-1] -eq '0') {
        & $projectPython -m app init-admin
        if ($LASTEXITCODE -ne 0) { throw 'Admin password was not initialized.' }
    }
    $adminPort = & $projectPython -c 'from app.ports import config_path, read_config; print(read_config(config_path())["admin_port"])'
    if ($LASTEXITCODE -ne 0 -or "$adminPort" -notmatch '^\d+$') { throw 'Could not read the saved admin port.' }
    $adminUrl = "http://localhost:$adminPort/admin"
    Write-Host "Open $adminUrl, log in, and follow the Tailscale steps in the frontend link card."
    # Open the browser only once HTTP is ready, while the server stays in this window.
    $browserScript = '
        for ($attempt = 0; $attempt -lt 90; $attempt++) {
            try {
                $reply = Invoke-WebRequest -UseBasicParsing -Uri ADMIN_URL -TimeoutSec 1
                if ($reply.StatusCode -eq 200) { Start-Process ADMIN_URL; exit 0 }
            } catch {}
            Start-Sleep -Milliseconds 500
        }'
    Start-Process -FilePath powershell.exe -WindowStyle Hidden -ArgumentList @('-NoProfile', '-Command', $browserScript.Replace('ADMIN_URL', $adminUrl))
    & $projectPython -m app serve --restart
    if ($LASTEXITCODE -ne 0) { throw 'Application startup failed.' }
} catch {
    Write-Host $_.Exception.Message -ForegroundColor Red
    exit 1
}
