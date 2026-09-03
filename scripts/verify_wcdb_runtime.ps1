$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$dllPath = Join-Path $projectRoot 'native\windows\wcdb_api.dll'
if (-not (Test-Path -LiteralPath $dllPath -PathType Leaf)) { throw "Missing: $dllPath" }
$dll = Get-Item -LiteralPath $dllPath
$hash = Get-FileHash -LiteralPath $dllPath -Algorithm SHA256
$signature = Get-AuthenticodeSignature -LiteralPath $dllPath
[pscustomobject]@{
    Path = $dll.FullName
    Length = $dll.Length
    SHA256 = $hash.Hash
    SignatureStatus = $signature.Status
    Signer = $signature.SignerCertificate.Subject
} | Format-List
