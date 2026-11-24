$ErrorActionPreference = "Stop"

Set-Location -LiteralPath $PSScriptRoot

if (Test-Path -LiteralPath ".env") {
    throw "A .env file already exists. It was left unchanged; resolve the existing database configuration before rerunning setup."
}

$service = Get-Service -Name "postgresql-x64-18" -ErrorAction SilentlyContinue
if (-not $service -or $service.Status -ne "Running") {
    throw "PostgreSQL 18 service is not running. Start the installed PostgreSQL service, then rerun this script."
}

$psqlPath = Join-Path $env:ProgramFiles "PostgreSQL\18\bin\psql.exe"
if (-not (Test-Path -LiteralPath $psqlPath)) {
    $psqlCommand = Get-Command "psql.exe" -ErrorAction SilentlyContinue
    if ($psqlCommand) {
        $psqlPath = $psqlCommand.Source
    } else {
        throw "psql.exe was not found. Install PostgreSQL command-line tools or add psql.exe to PATH."
    }
}

$adminUser = Read-Host "PostgreSQL administrator username [postgres]"
if ([string]::IsNullOrWhiteSpace($adminUser)) {
    $adminUser = "postgres"
}

$secureAdminPassword = Read-Host "PostgreSQL password for $adminUser (input is hidden)" -AsSecureString
$adminPassword = [System.Net.NetworkCredential]::new("", $secureAdminPassword).Password
$env:PGPASSWORD = $adminPassword

try {
    & $psqlPath -X -w -h 127.0.0.1 -p 5432 -U $adminUser -d postgres -tAc "SELECT 1" *> $null
    if ($LASTEXITCODE -ne 0) {
        throw "Could not authenticate to PostgreSQL as '$adminUser'. Check the username/password and try again."
    }

    $existingRoleOutput = & $psqlPath -X -w -h 127.0.0.1 -p 5432 -U $adminUser -d postgres -tAc "SELECT 1 FROM pg_roles WHERE rolname = 'school_admin'"
    if ($LASTEXITCODE -ne 0) {
        throw "Could not check for the school_admin role."
    }
    $existingRole = -not [string]::IsNullOrWhiteSpace(($existingRoleOutput -join "").Trim())

    $existingDatabaseOutput = & $psqlPath -X -w -h 127.0.0.1 -p 5432 -U $adminUser -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname = 'school_registration'"
    if ($LASTEXITCODE -ne 0) {
        throw "Could not check for the school_registration database."
    }
    $existingDatabase = -not [string]::IsNullOrWhiteSpace(($existingDatabaseOutput -join "").Trim())
    if ($existingRole -or $existingDatabase) {
        throw "The role or database already exists. No database changes were made; inspect the existing setup before proceeding."
    }

    $random = [byte[]]::new(36)
    $generator = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $generator.GetBytes($random)
    } finally {
        $generator.Dispose()
    }
    $appPassword = [Convert]::ToBase64String($random).TrimEnd("=").Replace("+", "-").Replace("/", "_")
    $secretBytes = [byte[]]::new(32)
    $generator = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $generator.GetBytes($secretBytes)
    } finally {
        $generator.Dispose()
    }
    $secretKey = [BitConverter]::ToString($secretBytes).Replace("-", "").ToLowerInvariant()

    "CREATE ROLE school_admin LOGIN PASSWORD '$appPassword';" |
        & $psqlPath -X -q -v ON_ERROR_STOP=1 -w -h 127.0.0.1 -p 5432 -U $adminUser -d postgres
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to create the school_admin database role."
    }

    & $psqlPath -X -q -v ON_ERROR_STOP=1 -w -h 127.0.0.1 -p 5432 -U $adminUser -d postgres -c "CREATE DATABASE school_registration OWNER school_admin;"
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to create the school_registration database. The role may have been created; inspect PostgreSQL before retrying."
    }

    @(
        "SECRET_KEY=$secretKey"
        "DATABASE_URL=postgresql+psycopg://school_admin:$appPassword@127.0.0.1:5432/school_registration"
    ) | Set-Content -LiteralPath ".env" -Encoding ascii

    Write-Host "Database and application role created. Local credentials saved to the ignored .env file."
    Write-Host "Next: run 'py -m pip install -r requirements.txt' if needed, then 'py run.py'."
} finally {
    Remove-Item Env:PGPASSWORD -ErrorAction SilentlyContinue
    $adminPassword = $null
    $secureAdminPassword.Dispose()
}
