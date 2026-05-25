# HFL — Hierarchical Federated Learning for AP Selection

A research implementation of **Hierarchical Federated Learning (HFL)** that optimizes wireless Access Point (AP) selection on mobile devices. The system learns optimal AP selection strategies locally on each device, aggregates knowledge through a distributed hierarchy of edge and central servers, and delivers real-time QoS-aware recommendations — all without centralizing user data.

## Architecture

```
                  ┌──────────────────┐
                  │  Central Server  │   Global FedAvg aggregation
                  └────────┬─────────┘
                           │
              ┌────────────┴────────────┐
              ▼                         ▼
     ┌────────────────┐       ┌────────────────┐
     │  Edge Server 1 │       │  Edge Server 2 │   Local FedAvg aggregation
     └───────┬────────┘       └───────┬────────┘
             │                        │
        ┌────┴────┐              ┌────┴────┐
        ▼         ▼              ▼         ▼
   ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌─────────┐
   │Terminal 1│ │Terminal 2│ │Terminal 3│ │Terminal 4│  On-device training
   └─────────┘ └─────────┘ └─────────┘ └─────────┘
```

**One round of federated learning:**

1. Central server distributes the global model to edge servers
2. Edge servers relay the model to assigned terminals
3. Each terminal trains locally on its own data (SGD)
4. Terminals upload only weight updates (not raw data) to their edge server
5. Edge servers aggregate terminal weights via FedAvg
6. Central server aggregates edge weights into the next global model

## Project Structure

```
HFL/
├── Serverside_HFL/          # Central + Edge servers (Python / FastAPI)
│   ├── central_server/      #   Central aggregation server
│   ├── edge_server/         #   Edge aggregation server (runs N instances)
│   ├── create_torchscript_model.py
│   └── requirements.txt
├── HFL_terminals/           # Android app for real devices (Kotlin)
│   └── app/                 #   On-device training, inference, telemetry
├── Sim_HFL/                 # Pure-Python simulator for algorithm validation
│   ├── sim/                 #   Core simulation modules
│   ├── config/              #   YAML experiment configs
│   └── run_sim.py           #   Entry point
├── HFL_AP_selection-main/   # Data generation & offline training pipeline
│   ├── HFL_main.py          #   HFL training loop
│   ├── hungarian_main.py    #   Ground-truth AP assignment (Hungarian algorithm)
│   └── cal.py               #   Erlang-B / M·M·1 queueing calculations
├── terminal_client/         # Python logical terminal (Docker simulation)
├── docker/                  # Dockerfiles & compose for containerized runs
├── docs/                    # Architecture & research documentation
└── compose.yaml             # Root-level Docker Compose
```

## Technology Stack

| Layer | Technologies |
|-------|-------------|
| **Server** | Python 3.9+, FastAPI, uvicorn, PyTorch 2.x, httpx, Redis |
| **Mobile** | Kotlin, Android (minSdk 26), custom neural-network autodiff, Coroutines |
| **Simulation** | PyTorch, NumPy, Pandas, Matplotlib, PyYAML |
| **Infrastructure** | Docker, Docker Compose, multi-container orchestration |

## Key Features

- **Privacy-preserving**: Terminals share only model weights, never raw communication data
- **Three-tier hierarchy**: Central → Edge → Terminal, enabling scalable aggregation
- **Real-device deployment**: Native Android app with on-device training (custom Kotlin backprop — no dependency on mobile ML frameworks)
- **QoS-aware AP selection**: Model inputs are app requirements (throughput, latency); outputs are probability distributions over available APs
- **Network modeling**: Erlang-B formula for capacity blocking and M/M/1 queueing for latency estimation
- **Simulation environment**: Pure-Python simulator with IID / non-IID data splits and convergence-bound validation (Li et al., 2020)
- **Docker-based testing**: Full hierarchy runnable locally via Docker Compose with logical terminal clients

## Quick Start

### Simulation (no hardware required)

```bash
cd Sim_HFL
pip install -r requirements.txt
python run_sim.py --config config/default.yaml
```

### Server-side (Docker)

```bash
docker compose up --build
```

This launches the central server, two edge servers, and terminal clients in a bridged Docker network.

### Android Terminal

Open `HFL_terminals/` in Android Studio, build, and deploy to a physical device. Configure the edge server endpoint in the app's setup screen.

## Documentation

Detailed documentation is available in [`docs/`](docs/):

- [System Architecture](docs/hfl_architecture.md)
- [Real-Device Architecture](docs/real_device_architecture.md)
- [Research Overview](docs/research_overview.md)
- [Docker Deployment](docker/README.md)
- [Server Run Guide](Serverside_HFL/RUN_GUIDE.md)

## License

This project is developed as part of research at Shinomi Lab.
