# HFL — Hierarchical Federated Learning for Wi-Fi AP Selection

A master's research project that evaluates **hierarchical federated learning (HFL)** for choosing a Wi-Fi access point (AP) on real Android phones. The system has three tiers: phones that train on-device, edge servers that aggregate them, and a central server that builds the global model. The same algorithms also run in a Python simulator and in Docker containers, so results can be compared across simulation, emulation and real hardware.

Thesis theme (Japanese): 「実環境における階層型連合学習を用いた動的アクセスポイント選択の有効性と限界」 — *effectiveness and limits of dynamic AP selection with hierarchical federated learning in a real environment*.

## Architecture

```
                  ┌──────────────────┐
                  │  Central Server  │   global aggregation (sync: threshold / async: K_CENTRAL)
                  └────────┬─────────┘
              ┌────────────┴────────────┐
              ▼                         ▼
     ┌────────────────┐       ┌────────────────┐
     │  Edge Server 1 │       │  Edge Server 2 │   edge FedAvg
     └───────┬────────┘       └───────┬────────┘
        ┌────┴────┐              ┌────┴────┐
        ▼         ▼              ▼         ▼
   Android 1  Android 2      Android 3  Android 4   on-device training, AP decision, QoS measurement
```

- **Phones** download the global model, train locally on their own data partition, upload weights and metrics, and measure throughput/RTT to compute a per-app *terminal satisfaction* score.
- **Edge servers** aggregate their phones and forward the result. **The central server** aggregates the edges in **synchronous** (round threshold) or **asynchronous** (staleness-bounded, K_CENTRAL) mode.
- AP selection model: a small MLP whose input follows the prior work (app QoS requirement z-scores + app one-hot) and whose output is one score per AP.

## What is in this repository

| Directory | Contents |
|---|---|
| `HFL_terminals/` | Android app (Kotlin, Jetpack Compose, minSdk 26). On-device training, model exchange with SHA-256 checks, AP decision engine and baselines, satisfaction measurement, an append-only metrics ledger with resend queue, adb-driven unattended sessions |
| `Serverside_HFL/` | Central and edge servers (Python 3.9, FastAPI). Sync/async aggregation, experiment control (`scripts/set_mode.py`), AP-state aggregation from real measurements, centralized-inference and HFL-recommendation endpoints, metrics ingestion and reconciliation, an **unattended trial runner** (`scripts/run_trials.py`) |
| `Sim_HFL/` | Pure-Python simulator (Python 3.12, PyTorch) with M/M/1 + Erlang-B congestion models, baselines, sync/async coordinators and factor-ablation configs |
| `docker/`, `terminal_client/`, `compose.yaml` | Docker virtual terminals for scale experiments (N = 4–14) |
| `docs/` | Architecture notes, runbooks and investigation reports from the earlier phase of the project |

## Engineering highlights

- **End-to-end correctness work**: an audit found that the phones were not really doing federated learning (random initialization every cycle, wrong gradients after epoch 1, LayerNorm backward errors, servers not converting received models). These were fixed and checked against golden tests.
- **Reproducible experiment operations**: each trial runs from an isolated run directory built from a git commit. A runner executes a condition table overnight on four phones, validates each trial (missing metrics, model provenance), and writes a manifest per trial.
- **Measurement integrity**: phones keep an append-only ledger and the server DB is reconciled against it (target: 0 missing rows). Throughput units, data partitioning, connected-AP recording and the "after" satisfaction window (settle time, repeated samples) were all audited and fixed, with each change recorded as a condition difference.
- **Spec-driven development**: features are specified with OpenSpec (proposal → spec → design → tasks) and implemented test-first. About 325 server tests and 290 Android JVM tests run on each change, plus Android lint for API-level compatibility.

## Results so far (October 2026)

- **Sync vs. async (real phones, symmetric delay, n = 5 each)**: the time for all four phones to finish five training cycles was 134.1 s (sync) and 117.4 s (async), a 1.14× ratio.
- **Sync vs. async (Docker, N = 4–14)**: async was faster at every N (Welch's t-test). The ratio depends strongly on the sync server's polling interval: 1.53–2.61× with 5 s polling and 1.10–1.17× with 1 s polling. Async slowed down as N grew, because the containers compete for the host CPU.
- **Limits found**: with the prior-work input design the model can only learn a per-app lookup table (accuracy ceiling about 86%), and on the lab network the satisfaction score often saturates at 1.0. Both are documented as limitations, and they motivate future extensions (measured AP state as input, non-IID data, congestion scenarios).
- Measuring satisfaction at the AP that HFL recommends is in progress.

## Quick start

```bash
# Simulator
cd Sim_HFL && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python3 run_sim.py --config config/default.yaml

# Servers (central :8000, edges :8001/:8002)
cd Serverside_HFL && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
HFL_MODE=sync .venv/bin/python3 start.py --run-mode production --env localhost --edges 2 --edge-thresholds 2,2 --central-threshold 2

# Android app
cd HFL_terminals && ./gradlew :app:assembleDebug :app:testDebugUnitTest
```

Wi-Fi credentials are intentionally left blank (`HFL_terminals/app/src/main/assets/ap_config.json`, `AppConfig.ROUTER_PASS_*`). Set them locally and never commit them. Teacher data and experiment results are not included.

## Tech stack

Kotlin · Jetpack Compose · PyTorch / pytorch_android · Python · FastAPI · SQLite · Docker · pytest · OpenSpec
