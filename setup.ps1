<#
.SYNOPSIS
Gatekey one-command setup for Windows.

.DESCRIPTION
Generates the two required secrets (no Python needed), writes .env from
.env.example, and starts the docker-compose stack.

.EXAMPLE
.\setup.ps1            # generate secrets, write .env, start docker compose
.\setup.ps1 -Cache     # also start Redis and enable rate limiting/caching
.\setup.ps1 -Sso       # also start the bundled dev-only Keycloak IdP
.\setup.ps1 -NoStart   # only generate .env, don't start containers
#>
[CmdletBinding()]
param(
    [switch]$Cache,
    [switch]$Sso,
    [switch]$NoStart
)

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

function Fail([string]$Message) {
    Write-Host "ERROR: $Message" -ForegroundColor Red
    exit 1
}

if (-not (Test-Path ".env.example")) {
    Fail ".env.example not found - run this from the Gatekey repo root."
}
if (Test-Path ".env") {
    Fail ".env already exists - refusing to overwrite it.`n       To start over, delete .env and re-run. To just start the stack:`n       docker compose up -d --build"
}

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Fail "Docker is not installed (or not on PATH). Install Docker Desktop first: https://docs.docker.com/get-docker/"
}
docker info *> $null
if ($LASTEXITCODE -ne 0) {
    Fail "The Docker daemon isn't running. Start Docker Desktop and re-run."
}

docker compose version *> $null
if ($LASTEXITCODE -eq 0) {
    $composeExe = "docker"
    $composePrefix = @("compose")
} elseif (Get-Command docker-compose -ErrorAction SilentlyContinue) {
    $composeExe = "docker-compose"
    $composePrefix = @()
} else {
    Fail "Neither 'docker compose' nor 'docker-compose' is available."
}
$composeDisplay = (@($composeExe) + $composePrefix) -join " "

function Invoke-Compose([string[]]$CmdArgs) {
    & $composeExe @($composePrefix + $CmdArgs)
}

# --- Generate the two required secrets (no host Python needed) --------------
$rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
$tokenBytes = New-Object byte[] 32
$rng.GetBytes($tokenBytes)
$adminToken = ($tokenBytes | ForEach-Object { $_.ToString("x2") }) -join ""
$keyBytes = New-Object byte[] 32
$rng.GetBytes($keyBytes)
$masterKey = [Convert]::ToBase64String($keyBytes)
$rng.Dispose()

# --- Write .env from the template (UTF-8, no BOM - compose reads it) --------
$envContent = [System.IO.File]::ReadAllText((Join-Path $PSScriptRoot ".env.example"))
$envContent = $envContent -replace "(?m)^GATEKEY_ADMIN_TOKEN=.*$", "GATEKEY_ADMIN_TOKEN=$adminToken"
$envContent = $envContent -replace "(?m)^GATEKEY_MASTER_KEY=.*$", "GATEKEY_MASTER_KEY=$masterKey"

$profileArgs = @()
if ($Cache) {
    # Enabling Redis takes both the profile AND the URL - do both here so
    # the features actually turn on.
    $envContent = $envContent -replace "(?m)^# GATEKEY_REDIS_URL=redis://redis:6379/0", "GATEKEY_REDIS_URL=redis://redis:6379/0"
    $profileArgs += @("--profile", "cache")
}
if ($Sso) {
    foreach ($line in @(
        "GATEKEY_OIDC_ISSUER_URL=http://keycloak:8080/realms/gatekey-dev",
        "GATEKEY_OIDC_CLIENT_ID=gatekey-backend",
        "GATEKEY_OIDC_CLIENT_SECRET=gatekey-dev-client-secret",
        "GATEKEY_OIDC_REDIRECT_URI=http://localhost:8000/v1/auth/sso/callback",
        "GATEKEY_SESSION_COOKIE_SECURE=false"
    )) {
        $envContent = $envContent -replace ("(?m)^# " + [regex]::Escape($line)), $line
    }
    $profileArgs += @("--profile", "sso")
}

$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText((Join-Path $PSScriptRoot ".env"), $envContent, $utf8NoBom)

Write-Host ""
Write-Host "Wrote .env with freshly generated secrets." -ForegroundColor Green
Write-Host ""
Write-Host "  Admin token (sign in to the console with this):"
Write-Host "    $adminToken" -ForegroundColor Cyan
Write-Host ""
Write-Host "  IMPORTANT: back up the GATEKEY_MASTER_KEY value in .env somewhere" -ForegroundColor Yellow
Write-Host "  safe (password manager / secrets vault). If it is lost, every" -ForegroundColor Yellow
Write-Host "  provider key stored in the database becomes permanently" -ForegroundColor Yellow
Write-Host "  unrecoverable." -ForegroundColor Yellow
Write-Host ""

if ($NoStart) {
    $startCmd = (@($composeDisplay) + $profileArgs + @("up", "-d", "--build")) -join " "
    Write-Host "Skipping container start (-NoStart). Start later with:"
    Write-Host "  $startCmd"
    exit 0
}

Write-Host "Building and starting containers (first build takes a few minutes)..."
Invoke-Compose ($profileArgs + @("up", "-d", "--build"))
if ($LASTEXITCODE -ne 0) {
    Fail "docker compose up failed - see the output above."
}

Write-Host "Waiting for the backend to become healthy" -NoNewline
$healthy = $false
for ($i = 0; $i -lt 90; $i++) {
    try {
        $resp = Invoke-WebRequest -Uri "http://localhost:8000/healthz" -UseBasicParsing -TimeoutSec 3
        if ($resp.StatusCode -eq 200) { $healthy = $true; break }
    } catch {}
    Write-Host "." -NoNewline
    Start-Sleep -Seconds 2
}
Write-Host ""
if (-not $healthy) {
    Fail "Backend did not become healthy within 3 minutes. Check logs with: $composeDisplay logs backend"
}

Write-Host ""
Write-Host "Gatekey is running." -ForegroundColor Green
Write-Host ""
Write-Host "  Admin console:  http://localhost:3000   (sign in with the admin token above)"
Write-Host "  Gateway API:    http://localhost:8000"
if ($Sso) {
    Write-Host "  Dev Keycloak:   http://localhost:8080   (admin/admin; test user: testuser/testpassword)"
}
Write-Host ""
Write-Host "Next: open the console, connect your first provider key, then follow"
Write-Host "the Quick start in README.md to make your first proxied request."
