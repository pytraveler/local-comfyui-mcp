param([string] $Subtitle)

$logo = @'
             _                       _
 _ __  _   _| |_ _ __ __ ___   _____| | ___ _ __
| '_ \| | | | __| '__/ _` \ \ / / _ \ |/ _ \ '__|
| |_) | |_| | |_| | | (_| |\ V /  __/ |  __/ |
| .__/ \__, |\__|_|  \__,_| \_/ \___|_|\___|_|
|_|    |___/
'@

Write-Host " ==================================================" -ForegroundColor Green
Write-Host ""
Write-Host $logo -ForegroundColor Yellow
if ($Subtitle) {
    Write-Host ""
    Write-Host "  $Subtitle" -ForegroundColor Yellow
}
Write-Host " ==================================================" -ForegroundColor Green
Write-Host ""
