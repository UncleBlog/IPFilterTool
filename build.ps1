$ErrorActionPreference = "Stop"

python -m pip install -r requirements-build.txt
python -m PyInstaller `
  --noconfirm `
  --clean `
  --onefile `
  --windowed `
  --name IPFilterTool `
  --icon "src\ip_filter.ico" `
  --add-data "src\ip_filter.ico;." `
  --distpath "dist" `
  --workpath "build" `
  --specpath "build" `
  "src\ip_filter_app.py"

Write-Host "构建完成：dist\IPFilterTool.exe"
