[CmdletBinding()]
param(
  [string]$GatewayBaseUrl = "http://127.0.0.1:8000",
  [string]$Prompt = "Reply with the single word: ready",
  [string]$Model,
  [switch]$ExpectUnauthorized
)

$ErrorActionPreference = "Stop"

try {
  $base = [uri]$GatewayBaseUrl
} catch {
  throw "GatewayBaseUrl must be an absolute http or https URL."
}

if (
  -not $base.IsAbsoluteUri -or
  $base.Scheme -notin @("http", "https") -or
  -not $base.Host -or
  $base.UserInfo -or
  $base.Query -or
  $base.Fragment
) {
  throw "GatewayBaseUrl must contain only scheme, host, and optional port/path."
}

$endpoint = $base.GetLeftPart([System.UriPartial]::Authority).TrimEnd("/") + "/v1/chat/completions"
$secureKey = Read-Host -AsSecureString -Prompt "Paste the newly issued personal MetaHub KEY"
$keyPointer = [IntPtr]::Zero

try {
  $keyPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureKey)
  $personalKey = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($keyPointer)
  if ([string]::IsNullOrWhiteSpace($personalKey)) {
    throw "A personal MetaHub KEY is required."
  }

  $body = @{
    messages = @(@{ role = "user"; content = $Prompt })
    stream = $false
  }
  if (-not [string]::IsNullOrWhiteSpace($Model)) {
    $body.model = $Model
  }

  try {
    $response = Invoke-RestMethod -Method Post -Uri $endpoint -Headers @{
      Authorization = "Bearer $personalKey"
    } -ContentType "application/json" -Body ($body | ConvertTo-Json -Depth 5 -Compress)
  } catch {
    $status = $null
    if ($_.Exception.Response) {
      $status = [int]$_.Exception.Response.StatusCode
    }
    if ($ExpectUnauthorized -and $status -eq 401) {
      [pscustomobject]@{
        Result = "expected_unauthorized"
        Endpoint = $endpoint
        StatusCode = 401
      }
      return
    }
    if ($status) {
      throw "Gateway request returned HTTP $status."
    }
    throw
  }

  if ($ExpectUnauthorized) {
    throw "Expected HTTP 401 after revocation, but the Gateway accepted the personal KEY."
  }
  if (-not @($response.choices).Count) {
    throw "Gateway returned success without a chat completion choice."
  }

  [pscustomobject]@{
    Result = "accepted"
    Endpoint = $endpoint
    ModelSent = if ($Model) { $Model } else { "(omitted; default route)" }
    RequestId = $response.id
  }
} finally {
  if ($keyPointer -ne [IntPtr]::Zero) {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($keyPointer)
  }
}
