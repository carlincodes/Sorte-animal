$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$candidatos = @(
    (Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"),
    (Join-Path $env:LOCALAPPDATA "Programs\Python\Python313\python.exe")
)
$python = $candidatos | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $python) {
    $achado = Get-Command python -ErrorAction SilentlyContinue
    if ($achado) { $python = $achado.Source }
}
if (-not $python) {
    Write-Error "Python 3.12 nao encontrado. Instale em python.org e rode este script de novo."
}

& $python -m pip install -r requirements.txt
& $python manage.py migrate
Start-Process -FilePath $python -ArgumentList "-m","middleware.broker","--nome","broker-a","--porta","9001","--par","127.0.0.1:9002" -WorkingDirectory $root
Start-Process -FilePath $python -ArgumentList "-m","middleware.broker","--nome","broker-b","--porta","9002","--par","127.0.0.1:9001" -WorkingDirectory $root
Start-Sleep -Seconds 1
Start-Process "http://127.0.0.1:8000/"
& $python manage.py runserver 127.0.0.1:8000
