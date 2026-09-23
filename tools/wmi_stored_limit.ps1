<#
  wmi_stored_limit.ps1 — write the EC "Stored Limit" xram[0x087F] via the
  firmware's own WMI mailbox (AcpiTest_MULong.GetSetULong -> AMW0 WMBC -> OEMG
  -> RKBC/WKBC). This is the official Uniwill channel; it reaches addresses
  above 0x07FF that the H2RAM window cannot.

  RUN ELEVATED (right-click -> Run with PowerShell as admin, or an elevated
  terminal). Payload layout per uniwill_wmi.c / DSDT:
    read  : Data = (0x0100 -shl 32) -bor addr
    write : Data = (value -shl 16) -bor addr
  Output: 'Return' u32, low byte = value; 0xFEFEFEFE = mailbox error.

  Default action: probe only (reads). Add -Write to actually set 0x087F=60.
#>
param(
    [switch]$Write,
    [uint16]$Addr = 0x087F,
    [byte]$Value = 60,
    # Project ID observed on the reference machine (MECHREVO JIAOLONG = 0x1A).
    # Other Uniwill models report different IDs - that is expected; the check
    # below only aborts on the mailbox error sentinel.
    [byte]$ExpectProjectId = 0x1A
)

$ins = Get-CimInstance -Namespace 'root\WMI' -ClassName 'AcpiTest_MULong' -ErrorAction Stop |
       Select-Object -First 1

function MB-Read([uint16]$a) {
    $d = ([uint64]0x0100 -shl 32) -bor [uint64]$a
    $r = Invoke-CimMethod -InputObject $ins -MethodName 'GetSetULong' `
         -Arguments @{ Data = $d } -ErrorAction Stop
    return $r.Return
}

function MB-Write([uint16]$a, [byte]$v) {
    $d = ([uint64]$v -shl 16) -bor [uint64]$a
    $r = Invoke-CimMethod -InputObject $ins -MethodName 'GetSetULong' `
         -Arguments @{ Data = $d } -ErrorAction Stop
    return $r.Return
}

# sanity: mailbox-read the project-id register.
# 0xFEFEFEFE is the firmware's mailbox timeout/not-implemented sentinel (DSDT).
$probe = MB-Read 0x0740
Write-Host ("mailbox[0x0740] = 0x{0:X8}  (project id = 0x{1:X2})" -f $probe, ($probe -band 0xFF))
if ($probe -eq 0xFEFEFEFE) {
    Write-Host "MAILBOX NOT RESPONDING (0xFEFEFEFE) - aborting, nothing written."
    exit 1
}
if (($probe -band 0xFF) -ne $ExpectProjectId) {
    Write-Host ("NOTE: project id 0x{0:X2} differs from the reference machine (0x{1:X2})." -f ($probe -band 0xFF), $ExpectProjectId)
    Write-Host "Register semantics may differ on your model - verify before using -Write."
}

# read the hidden stored limit
$stored = MB-Read $Addr
Write-Host ("mailbox[0x{0:X4}] = 0x{1:X8}  (current stored limit = {2})" -f $Addr, $stored, ($stored -band 0x7F))

if (-not $Write) {
    Write-Host "`nProbe only. Re-run with -Write to set xram[0x087F] = $Value."
    exit 0
}

$w = MB-Write $Addr $Value
Write-Host ("write result  = 0x{0:X8}" -f $w)
$verify = MB-Read $Addr
Write-Host ("verify read   = 0x{0:X8}  (stored limit = {1})" -f $verify, ($verify -band 0x7F))
if (($verify -band 0x7F) -eq $Value) {
    Write-Host "OK: stored limit is now $Value%. The 0x0770=4 pin can be removed (pin_limit.py --off)."
} else {
    Write-Host "Write did NOT stick - firmware likely ignores mailbox writes or needs a different commit."
}
