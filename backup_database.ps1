$ErrorActionPreference = "Stop"

Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class CurrentUserDpapi {
    [StructLayout(LayoutKind.Sequential)] private struct DATA_BLOB { public int cbData; public IntPtr pbData; }
    [DllImport("crypt32.dll", SetLastError=true, CharSet=CharSet.Unicode)]
    private static extern bool CryptProtectData(ref DATA_BLOB input, string description, IntPtr entropy, IntPtr reserved, IntPtr prompt, uint flags, out DATA_BLOB output);
    [DllImport("crypt32.dll", SetLastError=true, CharSet=CharSet.Unicode)]
    private static extern bool CryptUnprotectData(ref DATA_BLOB input, IntPtr description, IntPtr entropy, IntPtr reserved, IntPtr prompt, uint flags, out DATA_BLOB output);
    [DllImport("kernel32.dll")] private static extern IntPtr LocalFree(IntPtr memory);
    private static byte[] Transform(byte[] value, bool protect) {
        DATA_BLOB input = new DATA_BLOB(); input.cbData = value.Length; input.pbData = Marshal.AllocHGlobal(value.Length);
        Marshal.Copy(value, 0, input.pbData, value.Length); DATA_BLOB output;
        try {
            bool ok = protect ? CryptProtectData(ref input, "SchoolRegistration backup", IntPtr.Zero, IntPtr.Zero, IntPtr.Zero, 1, out output)
                              : CryptUnprotectData(ref input, IntPtr.Zero, IntPtr.Zero, IntPtr.Zero, IntPtr.Zero, 1, out output);
            if (!ok) throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
            byte[] result = new byte[output.cbData]; Marshal.Copy(output.pbData, result, 0, output.cbData); LocalFree(output.pbData); return result;
        } finally { Marshal.FreeHGlobal(input.pbData); }
    }
    public static byte[] Protect(byte[] value) { return Transform(value, true); }
    public static byte[] Unprotect(byte[] value) { return Transform(value, false); }
}
'@

Set-Location -LiteralPath $PSScriptRoot

$envFile = Join-Path $PSScriptRoot ".env"
if (-not (Test-Path -LiteralPath $envFile)) {
    throw "No .env file found. Configure the database before creating a backup."
}

$databaseLine = Get-Content -LiteralPath $envFile |
    Where-Object { $_ -match '^DATABASE_URL=' } |
    Select-Object -First 1
if (-not $databaseLine) {
    throw "DATABASE_URL is missing from .env."
}

$databaseUrl = $databaseLine.Substring("DATABASE_URL=".Length).Trim()
try {
    $connection = [System.Uri]::new($databaseUrl)
} catch {
    throw "DATABASE_URL is not a valid PostgreSQL connection URL."
}
if ($connection.Scheme -notin @("postgresql", "postgresql+psycopg")) {
    throw "DATABASE_URL must use PostgreSQL."
}

$credentials = $connection.UserInfo.Split(":", 2)
if ($credentials.Count -ne 2 -or -not $credentials[0]) {
    throw "DATABASE_URL must include a database username and password."
}
$databaseName = [System.Uri]::UnescapeDataString($connection.AbsolutePath.TrimStart("/"))
if (-not $databaseName) {
    throw "DATABASE_URL must include a database name."
}

$pgDumpPath = Join-Path $env:ProgramFiles "PostgreSQL\18\bin\pg_dump.exe"
if (-not (Test-Path -LiteralPath $pgDumpPath)) {
    $pgDumpCommand = Get-Command "pg_dump.exe" -ErrorAction SilentlyContinue
    if ($pgDumpCommand) {
        $pgDumpPath = $pgDumpCommand.Source
    } else {
        throw "pg_dump.exe was not found. Install PostgreSQL command-line tools or add them to PATH."
    }
}

$backupDirectory = Join-Path $env:LOCALAPPDATA "SchoolRegistration\backups"
New-Item -ItemType Directory -Path $backupDirectory -Force | Out-Null
$timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
$backupFile = Join-Path $backupDirectory "$($databaseName)_$timestamp.backup"
$encryptedFile = "$backupFile.dpapi"
if (Test-Path -LiteralPath $backupFile) {
    throw "Backup file already exists: $backupFile"
}

$previousPassword = $env:PGPASSWORD
$previousSslMode = $env:PGSSLMODE
try {
    $env:PGPASSWORD = [System.Uri]::UnescapeDataString($credentials[1])
    $arguments = @(
        "--host", $connection.Host,
        "--port", $(if ($connection.Port -gt 0) { $connection.Port } else { 5432 }),
        "--username", [System.Uri]::UnescapeDataString($credentials[0]),
        "--dbname", $databaseName,
        "--format=custom",
        "--file", $backupFile
    )

    foreach ($parameter in $connection.Query.TrimStart("?").Split("&")) {
        if ($parameter -match "^sslmode=(.+)$") {
            $env:PGSSLMODE = [System.Uri]::UnescapeDataString($Matches[1])
        }
    }

    & $pgDumpPath @arguments
    if ($LASTEXITCODE -ne 0) {
        if (Test-Path -LiteralPath $backupFile) {
            Remove-Item -LiteralPath $backupFile -Force
        }
        throw "pg_dump failed with exit code $LASTEXITCODE."
    }

    try {
        $plainBytes = [System.IO.File]::ReadAllBytes($backupFile)
        $encryptedBytes = [CurrentUserDpapi]::Protect($plainBytes)
        [System.IO.File]::WriteAllBytes($encryptedFile, $encryptedBytes)
        Remove-Item -LiteralPath $backupFile -Force
    } catch {
        if (Test-Path -LiteralPath $encryptedFile) { Remove-Item -LiteralPath $encryptedFile -Force }
        if (Test-Path -LiteralPath $backupFile) { Remove-Item -LiteralPath $backupFile -Force }
        throw "DPAPI encryption failed; the unencrypted temporary backup was removed. $($_.Exception.Message)"
    }

    $backup = Get-Item -LiteralPath $encryptedFile
    Write-Host "Encrypted database backup created: $($backup.FullName)"
    Write-Host ("Backup size: {0:N2} MB" -f ($backup.Length / 1MB))
    Write-Host "Only the same Windows user profile on this machine can decrypt this file."
} finally {
    if ($null -eq $previousPassword) {
        Remove-Item Env:PGPASSWORD -ErrorAction SilentlyContinue
    } else {
        $env:PGPASSWORD = $previousPassword
    }
    if ($null -eq $previousSslMode) {
        Remove-Item Env:PGSSLMODE -ErrorAction SilentlyContinue
    } else {
        $env:PGSSLMODE = $previousSslMode
    }
}
