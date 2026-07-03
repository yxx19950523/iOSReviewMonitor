$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)
python -m pip install -r requirements.txt
python -m PyInstaller --noconfirm --windowed --name "iOS审核状态监控" main.py
