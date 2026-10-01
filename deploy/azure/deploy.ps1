<#
.SYNOPSIS
  Deploy Minecraft Skill Builder to Azure App Service (Linux, Python), with Microsoft Entra ID
  sign-in for the teacher pages and a secret join code for Minecraft.

.DESCRIPTION
  Creates (or updates): a resource group, a Linux App Service plan, a Python 3.12 web app with
  WebSockets and Always On, an Entra ID app registration for sign-in, and the app settings.
  Then uploads the code (tracked and new files, never .env or other ignored files).
  Your Azure AI Foundry endpoint, key and model are read from the local .env.

  Run from the project folder in PowerShell, signed in with `az login`:
    ./deploy/azure/deploy.ps1                 # shows the plan and asks before creating anything
    ./deploy/azure/deploy.ps1 -Yes            # no prompt
    ./deploy/azure/deploy.ps1 -CodeOnly -Yes  # redeploy code to the existing app

.NOTES
  Cost: one B1 Linux App Service plan, about US$13 a month, billed while it exists.
  Remove everything with:  az group delete -n <resource group>
#>
param(
    [string]$ResourceGroup = "rg-minecraft-skill-builder",
    [string]$Location = "uksouth",
    [string]$AppName = "",
    [string]$Sku = "B1",
    [switch]$CodeOnly,
    [switch]$Yes
)
$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "../..")).Path
Set-Location $root

function Invoke-Az {
    # Run az and fail loudly; returns its output.
    $output = & az @args
    if ($LASTEXITCODE -ne 0) { throw "az $($args -join ' ') failed" }
    return $output
}

function Read-DotEnv([string]$path) {
    $values = @{}
    if (Test-Path $path) {
        foreach ($line in Get-Content $path) {
            if ($line -match '^\s*([A-Z_][A-Z0-9_]*)\s*=\s*(.*)\s*$') {
                $values[$Matches[1]] = $Matches[2].Trim().Trim("'").Trim('"')
            }
        }
    }
    return $values
}

$account = Invoke-Az account show --query "{name:name, id:id, tenant:tenantId, user:user.name}" -o json | ConvertFrom-Json
$stateFile = Join-Path $root "deploy/azure/.deploy-state.json"   # remembers the app name and join code (git-ignored)
$state = if (Test-Path $stateFile) { Get-Content $stateFile | ConvertFrom-Json } else { [pscustomobject]@{} }
if (-not $AppName) { $AppName = if ($state.appName) { $state.appName } else { "msb-" + (-join ((97..122) + (48..57) | Get-Random -Count 8 | ForEach-Object { [char]$_ })) } }
$joinCode = if ($state.joinCode) { $state.joinCode } else { -join ((97..122) + (48..57) | Get-Random -Count 12 | ForEach-Object { [char]$_ }) }
$hostName = "$AppName.azurewebsites.net"
$plan = "plan-$AppName"

Write-Host ""
Write-Host "Minecraft Skill Builder -> Azure" -ForegroundColor Cyan
Write-Host "  Subscription : $($account.name) ($($account.id))"
Write-Host "  Signed in as : $($account.user)"
if ($CodeOnly) {
    Write-Host "  Action       : upload code to $hostName"
} else {
    Write-Host "  Resources    : resource group $ResourceGroup ($Location)"
    Write-Host "                 App Service plan $plan (Linux $Sku, about US`$13/month)"
    Write-Host "                 web app $hostName (Python 3.12, WebSockets, Always On)"
    Write-Host "                 Entra ID app registration 'Minecraft Skill Builder ($AppName)' for sign-in"
}
Write-Host "  Minecraft    : /connect wss://$hostName/mc/$joinCode"
Write-Host ""
if (-not $Yes) {
    $answer = Read-Host "Type yes to continue"
    if ($answer -ne "yes") { Write-Host "Cancelled."; exit 1 }
}

if (-not $CodeOnly) {
    $dotenv = Read-DotEnv (Join-Path $root ".env")
    $endpoint = if ($dotenv.AZURE_AI_ENDPOINT) { $dotenv.AZURE_AI_ENDPOINT } else { $dotenv.AZURE_OPENAI_ENDPOINT }
    $apiKey = if ($dotenv.AZURE_AI_API_KEY) { $dotenv.AZURE_AI_API_KEY } else { $dotenv.AZURE_OPENAI_API_KEY }
    $model = if ($dotenv.AZURE_AI_MODEL) { $dotenv.AZURE_AI_MODEL } else { $dotenv.AZURE_OPENAI_DEPLOYMENT_NAME }

    Write-Host "Creating the resource group, plan and web app..." -ForegroundColor Cyan
    Invoke-Az group create -n $ResourceGroup -l $Location -o none
    Invoke-Az appservice plan create -g $ResourceGroup -n $plan --is-linux --sku $Sku -o none
    $exists = az webapp show -g $ResourceGroup -n $AppName --query name -o tsv 2>$null
    if (-not $exists) { Invoke-Az webapp create -g $ResourceGroup -p $plan -n $AppName --runtime "PYTHON:3.12" -o none }
    Invoke-Az webapp config set -g $ResourceGroup -n $AppName --web-sockets-enabled true --always-on true `
        --startup-file "PYTHONPATH=src python -m mcchat serve --data-dir /home/data" -o none

    Write-Host "Setting up Microsoft Entra ID sign-in..." -ForegroundColor Cyan
    $redirect = "https://$hostName/.auth/login/aad/callback"
    $appId = if ($state.clientId) { $state.clientId } else {
        Invoke-Az ad app create --display-name "Minecraft Skill Builder ($AppName)" --sign-in-audience AzureADMyOrg `
            --web-redirect-uris $redirect --enable-id-token-issuance true --query appId -o tsv
    }
    $secret = Invoke-Az ad app credential reset --id $appId --display-name "app-service-sign-in" --years 1 --append --query password -o tsv

    $settings = @(
        "HOSTED=true", "JOIN_CODE=$joinCode", "PUBLIC_URL=https://$hostName",
        "WEB_HOST=0.0.0.0", "WEB_PORT=8000", "WEBSITES_PORT=8000", "SCM_DO_BUILD_DURING_DEPLOYMENT=true",
        "MICROSOFT_PROVIDER_AUTHENTICATION_SECRET=$secret"
    )
    if ($endpoint) { $settings += "AZURE_AI_ENDPOINT=$endpoint" }
    if ($apiKey) { $settings += "AZURE_AI_API_KEY=$apiKey" }
    if ($model) { $settings += "AZURE_AI_MODEL=$model" }
    Invoke-Az webapp config appsettings set -g $ResourceGroup -n $AppName --settings @settings -o none

    # Sign-in for everything except Minecraft's address (which is protected by the join code).
    $auth = @{
        properties = @{
            platform = @{ enabled = $true }
            globalValidation = @{
                requireAuthentication = $true
                unauthenticatedClientAction = "RedirectToLoginPage"
                redirectToProvider = "azureactivedirectory"
                excludedPaths = @("/mc", "/mc/$joinCode")
            }
            identityProviders = @{
                azureActiveDirectory = @{
                    enabled = $true
                    registration = @{
                        openIdIssuer = "https://login.microsoftonline.com/$($account.tenant)/v2.0"
                        clientId = $appId
                        clientSecretSettingName = "MICROSOFT_PROVIDER_AUTHENTICATION_SECRET"
                    }
                    validation = @{ allowedAudiences = @("api://$appId", $appId) }
                }
            }
            login = @{ tokenStore = @{ enabled = $true } }
            httpSettings = @{ requireHttps = $false }   # allow ws:// as a fallback for Minecraft
        }
    } | ConvertTo-Json -Depth 10
    $authFile = New-TemporaryFile
    Set-Content -Path $authFile -Value $auth -Encoding utf8
    $uri = "https://management.azure.com/subscriptions/$($account.id)/resourceGroups/$ResourceGroup/providers/Microsoft.Web/sites/$AppName/config/authsettingsV2?api-version=2022-03-01"
    Invoke-Az rest --method put --uri $uri --body "@$authFile" -o none
    Remove-Item $authFile

    [pscustomobject]@{ appName = $AppName; resourceGroup = $ResourceGroup; joinCode = $joinCode; clientId = $appId } |
        ConvertTo-Json | Set-Content $stateFile
}

Write-Host "Packaging and uploading the code..." -ForegroundColor Cyan
$files = git ls-files --cached --others --exclude-standard | Where-Object { $_ -and (Test-Path (Join-Path $root $_) -PathType Leaf) }
$zipPath = Join-Path ([System.IO.Path]::GetTempPath()) "minecraft-skill-builder.zip"
if (Test-Path $zipPath) { Remove-Item $zipPath }
Add-Type -AssemblyName System.IO.Compression.FileSystem
$zip = [System.IO.Compression.ZipFile]::Open($zipPath, "Create")
foreach ($file in $files) {
    [System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile($zip, (Join-Path $root $file), $file.Replace("\", "/")) | Out-Null
}
$zip.Dispose()
Invoke-Az webapp deploy -g $ResourceGroup -n $AppName --src-path $zipPath --type zip -o none
Remove-Item $zipPath

Write-Host ""
Write-Host "Done." -ForegroundColor Green
Write-Host "  Teacher page : https://$hostName  (sign in with your Microsoft account)"
Write-Host "  Minecraft    : /connect wss://$hostName/mc/$joinCode"
Write-Host "  Logs         : az webapp log tail -g $ResourceGroup -n $AppName"
Write-Host "  Remove all   : az group delete -n $ResourceGroup"
