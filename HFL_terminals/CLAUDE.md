# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This is an Android-based **Hierarchical Federated Learning (HFL)** experiment platform. Android devices perform local model training, edge servers coordinate aggregation, and a central server manages model distribution.

## Build & Run Commands

### Android App (Gradle Wrapper)
```bash
./gradlew assembleDebug                   # Build debug APK
./gradlew :app:testDebugUnitTest          # Run unit tests
./gradlew :app:connectedAndroidTest       # Run instrumentation tests on connected device
./gradlew lint                            # Run lint checks
```

### Edge Server (FastAPI / Python)
```bash
cd edge_server
python -m venv .venv && source .venv/bin/activate
pip install fastapi uvicorn[standard] sqlalchemy aiosqlite pydantic python-multipart
uvicorn edge_server.main:app --host 0.0.0.0 --port 8000
```

## Architecture

### 3-Tier System

```
Central Server (PyTorch .pt models)
        ↓ distributes .pt files
Edge Server (FastAPI, port 8000/8001)
  - Converts .pt → meta.json + weight.bin
  - Receives terminal weights/logs via REST API
  - Persists to SQLite (state/training_data/metrics.db)
        ↓ distributes meta.json + weight.bin
Android Terminals (Kotlin/Compose)
  - Downloads model, verifies SHA256
  - Runs 5-cycle training (25 epochs)
  - Uploads trained weights + telemetry logs
  - Makes AP switching decisions via DecisionEngine
```

### Android App Package Structure (`com.example.hfl_experiment`)

| Package | Purpose |
|---|---|
| `experiment/` | `DecisionEngine` (AP selection ML inference), `Config` (reads `ap_config.json`), `ModelLoader`, `Uploader` |
| `training/` | `TrainingViewModel` (orchestrates 5-cycle rounds), `ModelWeightsLoader`, `WeightBinLoader`, `TrainingMetricsStore` |
| `network/` | `NetworkClient` (Retrofit API calls), `ModelUpdateUtil` (download/verify/apply), `NetworkSwitchHelper` (Wi-Fi AP switching) |
| `device/` | `DeviceAckClient` (WebSocket telemetry streaming) |
| `telemetry/` | `TelemetryWorker` (WorkManager background collection) |
| `ui/` | `MainActivity`, `TrainingScreen` (Compose), `SettingsActivity` |
| `util/` | `RealTimeLogger`/`ServerStyleLogger` (session-based logging), `NetworkDiagnostics`, `PingUtil` |

### Model Format

Models are distributed as two files:
- **`meta.json`** — layer definitions, shapes, SHA256 checksums; see `docs/TERMINAL_META_README.md` and `meta.schema.json`
- **`weight.bin`** — raw binary weights; parsed by `WeightBinLoader` according to meta.json spec

### Key Configuration

- **`BuildConfig.EDGE_BASE_URL`**: defaults to `http://192.168.11.2:8001`; overridable via `SettingsActivity` → SharedPreferences
- **`BuildConfig.SERVER_AUTH_TOKEN`**: Bearer token for edge server authentication
- **`app/src/main/assets/ap_config.json`**: Wi-Fi AP definitions (SSID, credentials, thresholds)
- **`app/src/main/assets/app.json`**: App category QoS requirements (throughput/RTT per category: browser/video/call/streaming)
- **`AppConfig.kt`**: Reads all runtime config from SharedPreferences; hardcoded router credentials marked for migration to Android Keystore

### Edge Server API Endpoints

| Endpoint | Purpose |
|---|---|
| `POST /send_to_device` | Distribute model meta/weights to terminals |
| `GET /download` | Stream model files |
| `POST /receive_terminal_weights/{terminal_id}` | Receive trained weights |
| `POST /upload_client_logs/{terminal_id}` | Receive telemetry logs |

## Key Technical Notes

- **PyTorch version**: `pytorch_android:1.13.1` (older version; model format compatibility matters)
- **Cleartext traffic**: Enabled (`android:usesCleartextTraffic="true"`) for local LAN testing
- **Wi-Fi switching**: Requires `ACCESS_FINE_LOCATION` and `CHANGE_WIFI_STATE` permissions; full automation not supported — ADB or manual switching used in experiments
- **Logging**: Dual system — `RealTimeLogger` writes session files to `sessions/session_XXXX.log`; `ServerStyleLogger` provides consistent timestamps for server-correlated logs
- **JVM heap**: Set to 2048m in `gradle.properties` (`org.gradle.jvmargs=-Xmx2048m`)
- **Android API**: minSdk=28 (Android 9), targetSdk=35; Java 11 source/target compatibility

## Documentation

Detailed specifications are in `docs/`:
- `TERMINAL_EDGE_PROJECT_SPEC.md` — Full 3-tier architecture spec (Japanese)
- `TERMINAL_META_README.md` — Weight binary format specification
- `CLIENT_METRICS_API.md` — Metrics upload API
- `DEVICE_ACK_SPEC.md` — Device telemetry ACK protocol
- `NETWORK_SWITCH_SPEC.md` — AP switching logic
