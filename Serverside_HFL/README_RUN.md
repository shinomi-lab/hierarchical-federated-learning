# How to run the local simulation from VS Code

This project includes a simple runner and VS Code configurations so you can start the central and edge servers using the Run (green) button.

Instructions (Windows PowerShell):

1. Open this workspace in VS Code.
2. (Optional) Create/activate your Python venv.
3. From VS Code, open the Run view (左の Run アイコン) and select "Run Simulation (central + edge)".
4. Press the green Run button. This will first run the "Install requirements" task (installs dependencies from `requirements.txt`), then launch `scripts/run_simulation.py` which starts the central and edge uvicorn servers.

Notes:
- If `uvicorn` is not on PATH for your Python environment, the runner uses `python -m uvicorn` inside the script. Make sure uvicorn is installed in the same environment.
- If you encounter errors, copy the integrated terminal output and share it so I can help debug.

## Shared Storage Rollup

サーバを別ターミナルで動かしていても保存先が同一の場合、集約ツールで横断的にCSV/JSONを作れます。

- 単発集約（最新日付自動検出＆全コンポーネント結合）

```powershell
python tools/shared_storage_rollup.py --latest --outdir logs/analysis --format both
```

- 日付指定（例：20251213）

```powershell
python tools/shared_storage_rollup.py --day 20251213 --outdir logs/analysis --format both
```

- 定期集約（10分ごと、停止時に最終集約も実行）

```powershell
./tools/auto_rollup.ps1 -IntervalMinutes 10 -Latest
```

出力例（`logs/analysis`）：
- `events_edge_server_all.csv/json`（全イベント結合を有効化した場合は`events_all_*`）
- `events_central_server_*.csv/json`
- `rounds_summary_*.csv/json`
- `aggregate_terminal_updates_*.csv/json`
- `aggregate_terminal_logs_*.csv/json`
- `aggregate_edge_manifests_*.csv/json`
- `aggregate_round_summary_*.csv/json`
- `aggregate_round_aggregate_*.csv/json`
