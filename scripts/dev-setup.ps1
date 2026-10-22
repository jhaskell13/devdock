param(
    [string]$PythonSelector,
    [switch]$Recreate
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $repoRoot

$venvPath = Join-Path $repoRoot ".venv"
$venvPython = Join-Path $venvPath "Scripts\python.exe"

if ($Recreate -and (Test-Path -LiteralPath $venvPath)) {
    $resolvedVenvPath = (Resolve-Path -LiteralPath $venvPath).Path
    if ($resolvedVenvPath -ne [System.IO.Path]::GetFullPath($venvPath)) {
        throw "Refusing to remove unexpected virtual environment path: $resolvedVenvPath"
    }
    Remove-Item -LiteralPath $resolvedVenvPath -Recurse -Force
}

if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        if ($PythonSelector) {
            & py "-$PythonSelector" -m venv $venvPath
        } else {
            & py -3 -m venv $venvPath
        }
    } elseif (Get-Command python -ErrorAction SilentlyContinue) {
        if ($PythonSelector) {
            throw "The Python launcher 'py' is required to select Python $PythonSelector."
        }
        & python -m venv $venvPath
    } else {
        throw "Python was not found. Install Python 3.12 or newer and make it available as 'py' or 'python'."
    }
    if ($LASTEXITCODE -ne 0) {
        if (Get-Command py -ErrorAction SilentlyContinue) {
            Write-Host "Python versions known to the launcher:"
            & py -0p
        }
        throw "Virtual environment creation failed. Check that the selected Python version is installed."
    }
}

if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
    throw "Virtual environment creation did not produce $venvPython"
}

& $venvPython -c "import sys; print(f'Using Python {sys.version.split()[0]}'); raise SystemExit(0 if sys.version_info >= (3, 12) else 1)"
if ($LASTEXITCODE -ne 0) {
    throw "DevDock requires Python 3.12 or newer. Recreate the environment with -Recreate after installing a supported Python."
}

& $venvPython -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) {
    throw "Upgrading pip failed."
}

& $venvPython -m pip install -e ".[dev]"
if ($LASTEXITCODE -ne 0) {
    throw "Installing DevDock development dependencies failed."
}

Write-Host "Setup complete. Run tests with: .\.venv\Scripts\python.exe -m pytest"
