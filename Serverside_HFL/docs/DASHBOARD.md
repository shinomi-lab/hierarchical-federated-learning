## HFL Central Dashboard

Lightweight Streamlit dashboard and a small verification tool were added under `central_server/`.

Files added:
- `central_server/dashboard.py` — Streamlit app showing training data (from `central_server/training_data.db`) and scanning `received_files/terminal_updates` for manifest JSONs. Useful for quick visual checks and basic cross-checks.
- `central_server/verify_metadata_consistency.py` — CLI script that scans manifests and searches `logs/time_records/*.log` for manifest `sha256` and `run_id` values (heuristic E2E verification).

How to run the dashboard

1. Install dependencies (if not already present in your environment): streamlit, pandas

2. Start the dashboard:

    streamlit run central_server/dashboard.py

What it shows

- Training data table (rows read from `central_server/training_data.db`).
- Counts by day (if `created_at` column exists).
- Detected manifest files with `sha256`, `base_hash`, `event_timestamp`, `run_id`.
- Top SHA frequency and quick checks for missing values or mismatches.

Quick verification

Run the simple consistency check:

    python central_server/verify_metadata_consistency.py

This prints how many manifests have their `sha256` or `run_id` present in the logs. It's a heuristic that helps find obvious propagation gaps quickly.

Notes & next steps

- The dashboard is intentionally small; extend it to join the training DB with received-edges metadata or build a more advanced UI (Plotly, Grafana) for production use.
- If you want, I can add a small FastAPI route that serves the same aggregates as JSON for external dashboards.
