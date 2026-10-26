# Edge Server (FastAPI) for Training Data

This is a minimal FastAPI edge server compatible with the Android/Kotlin client guide provided.

Features
- POST /api/training-data: accept JSON metrics
- POST /api/training-data/upload-sample: accept multipart file uploads
- Simple token-based auth via `Authorization: Bearer <token>`
- SQLite storage for metrics and file metadata
- Files saved under `state/training_data/` and an `index.jsonl` appended for each upload

Quick start (Windows PowerShell):

```powershell
python -m venv .venv; .\.venv\Scripts\Activate.ps1
pip install fastapi uvicorn[standard] sqlalchemy aiosqlite pydantic python-multipart
uvicorn edge_server.main:app --host 0.0.0.0 --port 8000
```

Default token: `secret-token` (change in production)

API
- POST /api/training-data
  - JSON body: see TrainingMetrics model
  - Response: {"status":"ok","id":<row_id>}

- POST /api/training-data/upload-sample
  - multipart/form-data: file, device_id, round_number, label (optional), sample_type (optional), timestamp (optional)
  - Response: {"status":"ok","sha256":"...","filename":"..."}

Storage layout
- state/training_data/metrics.db (SQLite)
- state/training_data/files/ saved binary files
- state/training_data/index.jsonl log lines

Notes
- This is a minimal example meant to match the Android guide. Extend with authentication, TLS, rate limiting, and production-grade storage as needed.

