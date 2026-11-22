param(
    [string]$Sdk = "$env:LOCALAPPDATA\Android\Sdk",
    [string]$BuildTools = "36.0.0",
    [string]$Output = (Join-Path $PSScriptRoot "VkSmsUSB.apk"),
    [string]$Keystore = (Join-Path $PSScriptRoot ".local-build\relay.jks")
)
$ErrorActionPreference = "Stop"
$tools = Join-Path $Sdk "build-tools\$BuildTools"
$jar = Join-Path $Sdk "platforms\android-36\android.jar"
$src = Join-Path $PSScriptRoot "android"
$tmp = Join-Path $PSScriptRoot ".local-build"
New-Item -ItemType Directory -Force "$tmp\classes", "$tmp\dex" | Out-Null

function Run-Checked([string]$Program, [string[]]$Arguments) {
    & $Program @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Program exited with $LASTEXITCODE" }
}

Run-Checked "javac" (@("--release", "8", "-encoding", "UTF-8", "-classpath", $jar, "-d", "$tmp\classes") + @(Get-ChildItem "$src\*.java" | ForEach-Object FullName))
$classes = @(Get-ChildItem "$tmp\classes" -Recurse -Filter "*.class" | ForEach-Object FullName)
Run-Checked "$tools\d8.bat" (@("--min-api", "24", "--lib", $jar, "--output", "$tmp\dex") + $classes)
Run-Checked "$tools\aapt2.exe" @("link", "-I", $jar, "--manifest", "$src\AndroidManifest.xml", "-o", "$tmp\base.apk")
Run-Checked "python" @("-c", "import sys, zipfile; apk = zipfile.ZipFile(sys.argv[1], 'a'); apk.write(sys.argv[2], 'classes.dex', compress_type=zipfile.ZIP_DEFLATED); apk.close()", "$tmp\base.apk", "$tmp\dex\classes.dex")
Run-Checked "$tools\zipalign.exe" @("-f", "4", "$tmp\base.apk", "$tmp\aligned.apk")
if (!(Test-Path -LiteralPath $Keystore)) {
    Run-Checked "keytool" @("-genkeypair", "-keystore", $Keystore, "-storepass", "android", "-keypass", "android", "-alias", "vksms", "-keyalg", "RSA", "-keysize", "2048", "-validity", "3650", "-dname", "CN=VkSms USB")
}
Run-Checked "$tools\apksigner.bat" @("sign", "--ks", $Keystore, "--ks-pass", "pass:android", "--key-pass", "pass:android", "--out", $Output, "$tmp\aligned.apk")
Run-Checked "$tools\apksigner.bat" @("verify", "--verbose", $Output)
