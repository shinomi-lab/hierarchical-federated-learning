# HFL Project Architecture and Data Flow

**Date:** 2026-04-27
**Overview:** This document outlines the architectural structure and data flow of the Hierarchical Federated Learning (HFL) project, including the real-device/Docker setup and the standalone Python simulator.

## System Architecture

```mermaid
flowchart TD
    %% Style Definitions
    classDef central fill:#f9f,stroke:#333,stroke-width:2px;
    classDef edge fill:#bbf,stroke:#333,stroke-width:2px;
    classDef terminal fill:#dfd,stroke:#333,stroke-width:2px;
    classDef sim fill:#ffe,stroke:#333,stroke-width:2px;

    %% ---------------------------------------------
    %% 1. Real-Device / Docker System (Production / Experiment)
    %% ---------------------------------------------
    subgraph Serverside_HFL ["☁️ Serverside_HFL (FastAPI)"]
        direction TB
        CentralServer["Central Server<br>(Layer N)"]:::central
        EdgeServer1["Edge Server 00<br>(Layer 0)"]:::edge
        EdgeServer2["Edge Server 01<br>(Layer 0)"]:::edge
        
        CentralServer -- "1. Global Model Distribution<br>4. Weight Aggregation (FedAvg)" <--> EdgeServer1
        CentralServer -- "1. Global Model Distribution<br>4. Weight Aggregation (FedAvg)" <--> EdgeServer2
    end

    subgraph Clients ["📱 Terminals (Android / Docker)"]
        direction TB
        AndroidApp["HFL_terminals<br>(Android Kotlin)"]:::terminal
        LogicalClient1["terminal_client<br>(Python Logical)"]:::terminal
        LogicalClient2["terminal_client<br>(Python Logical)"]:::terminal
        
        AndroidApp -- "2. Model Download<br>3. Upload Local Weights" <--> EdgeServer1
        LogicalClient1 -- "2. Model Download<br>3. Upload Local Weights" <--> EdgeServer1
        LogicalClient2 -- "2. Model Download<br>3. Upload Local Weights" <--> EdgeServer2
    end

    %% Data Flow Notes
    note1>"Flow:<br>① Central sends latest model to Edge<br>② Terminals fetch model from Edge<br>③ Terminals perform local training (MLP) & upload weights<br>④ Edge aggregates terminal weights & sends to Central<br>⑤ Central updates global model (proceed to next round)"]
    CentralServer -.- note1

    %% ---------------------------------------------
    %% 2. Standalone Simulator (Theoretical Simulation)
    %% ---------------------------------------------
    subgraph Sim_HFL ["💻 Sim_HFL (Python Simulator)"]
        direction TB
        Runner["runner.py<br>(HFLRunner)"]:::sim
        SimData["data_gen.py<br>(Synthetic Data)"]:::sim
        SimTerm["terminal.py<br>(Virtual Terminal)"]:::sim
        SimAgg["aggregator.py<br>(Virtual Server)"]:::sim
        
        Runner -->|Generate Data| SimData
        Runner -->|Model Distribute/Aggregate| SimAgg
        Runner -->|Local Train/Infer| SimTerm
        SimTerm <-->|Virtual Communication| SimAgg
    end

    %% System Relationship Note
    note2>"* Sim_HFL is a standalone environment for theoretical validation.<br>It rapidly runs the learning cycle to verify algorithms,<br>and successful settings/models are then ported to the real system."]
    Sim_HFL -.- note2
```

## Summary
1. **Server Hierarchy (`Serverside_HFL`):** Uses a two-tier structure (FastAPI) with "Edge" servers handling terminals and a "Central" server orchestrating the global model. This distributes network load while performing FedAvg weight aggregation and distribution.
2. **Clients (`HFL_terminals` / `terminal_client`):** Android smartphones or Python-based logical clients in Docker. They download the model, perform training on local data, and upload only the resulting weights to the Edge server.
3. **Simulator (`Sim_HFL`):** An independent experimental platform that runs the "Data Generation -> Training -> Aggregation -> Evaluation" cycle rapidly on a single machine without actual network communication, used for theoretical validation.