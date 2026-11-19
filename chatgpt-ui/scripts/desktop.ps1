param(
  [ValidateSet('dev', 'build')]
  [string]$Mode = 'dev'
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$localCargo = Join-Path $root '.cargo\bin\cargo.exe'

if (Test-Path -LiteralPath $localCargo) {
  $env:RUSTUP_HOME = Join-Path $root '.rustup'
  $env:CARGO_HOME = Join-Path $root '.cargo'
  $env:Path = "$(Join-Path $root '.cargo\bin');$env:Path"
}

Push-Location $root
try {
  & npm.cmd run "desktop:$Mode"
  if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
} finally {
  Pop-Location
}
