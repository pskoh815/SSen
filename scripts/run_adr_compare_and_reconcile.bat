@echo off
cd /d C:\MyClaude\ssen-dashboard
py -3.9-32 "src\ssen\collect\kiwoom\verify_adr_timing.py" compare
set PYTHONPATH=src
set PYTHONIOENCODING=utf-8
python -m ssen.update.daily_update --reconcile-adr >> logs\adr_verify.log 2>&1
