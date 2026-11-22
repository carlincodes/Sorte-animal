Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" | Where-Object {
    $_.CommandLine -match "middleware.broker|manage.py runserver"
} | ForEach-Object {
    Stop-Process -Id $_.ProcessId -Force
    Write-Output ("encerrou " + $_.ProcessId)
}
