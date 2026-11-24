param(
    [Parameter(Mandatory = $true)][string]$BackupPath,
    [Parameter(Mandatory = $true)][ValidatePattern("^[A-Za-z0-9_]+$")][string]$DatabaseName
)
$ErrorActionPreference = "Stop"

Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class CurrentUserDpapiRestore {
    [StructLayout(LayoutKind.Sequential)] private struct DATA_BLOB { public int cbData; public IntPtr pbData; }
    [DllImport("crypt32.dll", SetLastError=true, CharSet=CharSet.Unicode)] private static extern bool CryptUnprotectData(ref DATA_BLOB input, IntPtr description, IntPtr entropy, IntPtr reserved, IntPtr prompt, uint flags, out DATA_BLOB output);
    [DllImport("kernel32.dll")] private static extern IntPtr LocalFree(IntPtr memory);
    public static byte[] Unprotect(byte[] value) {
        DATA_BLOB input = new DATA_BLOB(); input.cbData = value.Length; input.pbData = Marshal.AllocHGlobal(value.Length);
        Marshal.Copy(value, 0, input.pbData, value.Length); DATA_BLOB output;
        try {
            if (!CryptUnprotectData(ref input, IntPtr.Zero, IntPtr.Zero, IntPtr.Zero, IntPtr.Zero, 1, out output)) throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
            byte[] result = new byte[output.cbData]; Marshal.Copy(output.pbData, result, 0, output.cbData); LocalFree(output.pbData); return result;
        } finally { Marshal.FreeHGlobal(input.pbData); }
    }
}
'@

$resolvedBackup = (Resolve-Path -LiteralPath $BackupPath).Path
if (-not $resolvedBackup.EndsWith(".backup.dpapi", [StringComparison]::OrdinalIgnoreCase)) {
    throw "Select an encrypted .backup.dpapi file created by backup_database.ps1."
}
$envFile = Join-Path $PSScriptRoot ".env"
if (-not (Test-Path -LiteralPath $envFile)) { throw "No .env file found." }
$databaseLine = Get-Content -LiteralPath $envFile | Where-Object { $_ -match '^DATABASE_URL=' } | Select-Object -First 1
if (-not $databaseLine) { throw "DATABASE_URL is missing from .env." }
$databaseUrl = $databaseLine.Substring("DATABASE_URL=".Length).Trim()
$connection = [System.Uri]::new($databaseUrl)
$sourceName = [System.Uri]::UnescapeDataString($connection.AbsolutePath.TrimStart("/"))
if ($DatabaseName -eq $sourceName) { throw "Refusing to restore over the configured application database. Choose a separate empty recovery database." }
$credentials = $connection.UserInfo.Split(":", 2)
if ($credentials.Count -ne 2 -or -not $credentials[0]) { throw "DATABASE_URL must include the PostgreSQL username and password." }
$pgRestorePath = Join-Path $env:ProgramFiles "PostgreSQL\18\bin\pg_restore.exe"
if (-not (Test-Path -LiteralPath $pgRestorePath)) {
    $pgRestoreCommand = Get-Command "pg_restore.exe" -ErrorAction SilentlyContinue
    if ($pgRestoreCommand) { $pgRestorePath = $pgRestoreCommand.Source } else { throw "pg_restore.exe was not found." }
}
$backupDirectory = Split-Path -Parent $resolvedBackup
$temporaryBackup = Join-Path $backupDirectory ("restore_" + [Guid]::NewGuid().ToString("N") + ".backup")
$previousPassword = $env:PGPASSWORD
$previousSslMode = $env:PGSSLMODE
try {
    $encryptedBytes = [System.IO.File]::ReadAllBytes($resolvedBackup)
    $plainBytes = [CurrentUserDpapiRestore]::Unprotect($encryptedBytes)
    [System.IO.File]::WriteAllBytes($temporaryBackup, $plainBytes)
    $env:PGPASSWORD = [System.Uri]::UnescapeDataString($credentials[1])
    foreach ($parameter in $connection.Query.TrimStart("?").Split("&")) {
        if ($parameter -match "^sslmode=(.+)$") { $env:PGSSLMODE = [System.Uri]::UnescapeDataString($Matches[1]) }
    }
    Write-Host "Restoring into '$DatabaseName'. The destination must be an empty recovery database."
    $arguments = @("--exit-on-error", "--single-transaction", "--no-owner", "--no-privileges", "--host", $connection.Host,
        "--port", $(if ($connection.Port -gt 0) { $connection.Port } else { 5432 }), "--username", [System.Uri]::UnescapeDataString($credentials[0]),
        "--dbname", $DatabaseName, $temporaryBackup)
    & $pgRestorePath @arguments
    if ($LASTEXITCODE -ne 0) { throw "pg_restore failed with exit code $LASTEXITCODE. The encrypted backup was preserved." }
    Write-Host "Recovery restore completed. Verify the data and migration revision before using it."
} finally {
    if (Test-Path -LiteralPath $temporaryBackup) { Remove-Item -LiteralPath $temporaryBackup -Force }
    if ($null -eq $previousPassword) { Remove-Item Env:PGPASSWORD -ErrorAction SilentlyContinue } else { $env:PGPASSWORD = $previousPassword }
    if ($null -eq $previousSslMode) { Remove-Item Env:PGSSLMODE -ErrorAction SilentlyContinue } else { $env:PGSSLMODE = $previousSslMode }
}
