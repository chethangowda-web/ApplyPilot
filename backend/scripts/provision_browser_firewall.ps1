#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Provisions Windows Defender Firewall outbound egress rules for Playwright Chromium.

.DESCRIPTION
    Restricts the isolated Playwright Chromium executable so it may open outbound TCP
    connections ONLY to 127.0.0.1 (the ApplyPilot LocalSSRFEgressProxy) while blocking
    direct external IPv4/IPv6 TCP and all direct UDP (blocking QUIC, WebRTC STUN, and
    direct external egress). Because Windows Firewall evaluates Block rules ahead of Allow
    rules, non-loopback IPv4 ranges (0.0.0.0-126.255.255.255 and 128.0.0.0-255.255.255.255)
    and all IPv6 (::/0) are explicitly blocked rather than using 0.0.0.0/0.
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [Parameter(Mandatory = $false)]
    [string]$ChromiumPath = "",

    [Parameter(Mandatory = $false)]
    [string]$RulePrefix = "ApplyPilot-Browser-Egress"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($ChromiumPath)) {
    $msPlaywrightRoot = Join-Path $env:LOCALAPPDATA "ms-playwright"
    $candidates = Get-ChildItem -Path $msPlaywrightRoot -Filter "chrome.exe" -Recurse -ErrorAction SilentlyContinue |
        Where-Object { $_.FullName -like "*\chrome-win64\chrome.exe" } |
        Sort-Object LastWriteTime -Descending
    if (-not $candidates) {
        throw "Could not locate Playwright chrome.exe under $msPlaywrightRoot. Pass -ChromiumPath explicitly."
    }
    $ChromiumPath = $candidates[0].FullName
}

$resolvedChrome = (Resolve-Path -LiteralPath $ChromiumPath).Path
$loopbackAllowRule = "$RulePrefix-Allow-Loopback-TCP"
$blockExternalV4Rule = "$RulePrefix-Block-External-IPv4-TCP"
$blockExternalV6Rule = "$RulePrefix-Block-External-IPv6-TCP"
$blockUdpRule = "$RulePrefix-Block-All-UDP"

$allRuleNames = @($loopbackAllowRule, $blockExternalV4Rule, $blockExternalV6Rule, $blockUdpRule)

if ($PSCmdlet.ShouldProcess($resolvedChrome, "Create Windows Defender Firewall browser egress rules")) {
    foreach ($name in $allRuleNames) {
        Get-NetFirewallRule -DisplayName $name -ErrorAction SilentlyContinue |
            Remove-NetFirewallRule -ErrorAction Stop
    }

    New-NetFirewallRule `
        -DisplayName $loopbackAllowRule `
        -Direction Outbound `
        -Action Allow `
        -Enabled True `
        -Profile Any `
        -Program $resolvedChrome `
        -Protocol TCP `
        -RemoteAddress "127.0.0.1" | Out-Null

    New-NetFirewallRule `
        -DisplayName $blockExternalV4Rule `
        -Direction Outbound `
        -Action Block `
        -Enabled True `
        -Profile Any `
        -Program $resolvedChrome `
        -Protocol TCP `
        -RemoteAddress @("0.0.0.0-127.0.0.0", "127.0.0.2-255.255.255.255") | Out-Null

    New-NetFirewallRule `
        -DisplayName $blockExternalV6Rule `
        -Direction Outbound `
        -Action Block `
        -Enabled True `
        -Profile Any `
        -Program $resolvedChrome `
        -Protocol TCP `
        -RemoteAddress "::/0" | Out-Null

    New-NetFirewallRule `
        -DisplayName $blockUdpRule `
        -Direction Outbound `
        -Action Block `
        -Enabled True `
        -Profile Any `
        -Program $resolvedChrome `
        -Protocol UDP `
        -RemoteAddress Any | Out-Null

    Write-Output "Provisioned Windows Firewall egress rules for $resolvedChrome with prefix $RulePrefix"
}
