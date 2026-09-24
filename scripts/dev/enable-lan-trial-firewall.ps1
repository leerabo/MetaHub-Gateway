[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)]
  [string]$RemoteCidr
)

$ErrorActionPreference = "Stop"

if ($RemoteCidr -notmatch '^(?:25[0-5]|2[0-4]\d|1?\d?\d)(?:\.(?:25[0-5]|2[0-4]\d|1?\d?\d)){3}/(?:[0-9]|[12]\d|3[0-2])$') {
  throw "RemoteCidr must be an IPv4 CIDR, for example 172.16.100.0/24."
}

$principal = [Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
  throw "Run this script from an elevated Administrator PowerShell session."
}

$rules = @(
  @{ Name = "MetaHub LAN Trial Console"; Port = 5173 },
  @{ Name = "MetaHub LAN Trial Control"; Port = 8001 },
  @{ Name = "MetaHub LAN Trial Gateway"; Port = 8000 },
  @{ Name = "MetaHub LAN Trial Status"; Port = 8002 }
)
foreach ($rule in $rules) {
  Get-NetFirewallRule -DisplayName $rule.Name -ErrorAction SilentlyContinue |
    Remove-NetFirewallRule -ErrorAction SilentlyContinue
  New-NetFirewallRule -DisplayName $rule.Name -Direction Inbound -Action Allow `
    -Protocol TCP -LocalPort $rule.Port -RemoteAddress $RemoteCidr -Profile Any | Out-Null
}

Write-Host "MetaHub LAN trial ingress enabled for $RemoteCidr on TCP 5173, 8001, 8000, and 8002."
