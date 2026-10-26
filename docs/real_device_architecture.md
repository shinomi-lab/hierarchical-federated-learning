# Real-Device HFL Architecture and Data Flow

**Date:** 2026-04-27
**Overview:** This document outlines the architectural structure and data flow focusing exclusively on the real-device environment (physical Android smartphones communicating with the real Server hierarchy). Simulated environments and Docker-based logical clients are omitted.

## System Architecture (Real Devices)

```mermaid
flowchart TD
    %% Style Definitions
    classDef central fill:#f9f,stroke:#333,stroke-width:2px,color:#000;
    classDef edge fill:#bbf,stroke:#333,stroke-width:2px,color:#000;
    classDef terminal fill:#dfd,stroke:#333,stroke-width:2px,color:#000;

    %% ---------------------------------------------
    %% Real Server Infrastructure
    %% ---------------------------------------------
    subgraph Serverside_HFL ["☁️ Serverside_HFL (FastAPI / Cloud & Edge)"]
        direction TB
        CentralServer["Central Server<br>(Layer N: Global Aggregation)"]:::central
        EdgeServer1["Edge Server 00<br>(Layer 0: Local Aggregation)"]:::edge
        EdgeServer2["Edge Server 01<br>(Layer 0: Local Aggregation)"]:::edge
        
        CentralServer -- "1. グローバルモデル配布<br>4. 重み集約 (FedAvg)" <--> EdgeServer1
        CentralServer -- "1. グローバルモデル配布<br>4. 重み集約 (FedAvg)" <--> EdgeServer2
    end

    %% ---------------------------------------------
    %% Real Physical Terminals
    %% ---------------------------------------------
    subgraph Physical_Terminals ["📱 Physical Android Devices (HFL_terminals)"]
        direction TB
        AndroidDevice1["Android App 1<br>(Kotlin)"]:::terminal
        AndroidDevice2["Android App 2<br>(Kotlin)"]:::terminal
        AndroidDevice3["Android App 3<br>(Kotlin)"]:::terminal
        
        AndroidDevice1 -- "2. モデルDL<br>3. ローカル学習重みUP" <--> EdgeServer1
        AndroidDevice2 -- "2. モデルDL<br>3. ローカル学習重みUP" <--> EdgeServer1
        AndroidDevice3 -- "2. モデルDL<br>3. ローカル学習重みUP" <--> EdgeServer2
    end

    %% Flow Explanation Note
    note1>"🔄 データフロー (1ラウンドの流れ):<br>① Centralが最新のグローバルモデルを各Edgeへ配布<br>② Android端末が担当のEdgeからモデルをダウンロード<br>③ Android実機上でローカル学習(MLP)を実行し、重みをEdgeへアップロード<br>④ Edgeが配下のAndroid端末の重みを集約し、Centralへアップロード<br>⑤ Centralが全Edgeの重みからグローバルモデルを更新"]
    CentralServer -.- note1
```

## Summary
1. **クラウド・エッジインフラ（`Serverside_HFL`）:** 実環境にデプロイされたCentralサーバーとEdgeサーバー群です。Edgeが各Android端末からの通信を捌き、Centralが全体の学習（FedAvg）を統括します。
2. **実機Android端末（`HFL_terminals`）:** 物理的なスマートフォン（Android）上で動くアプリです。ユーザーの実際の通信環境やアプリ使用状況をリアルタイムで観測しながら、エッジサーバーとモデルの重みをやり取りします。
3. **データ通信の独立性:** 各Android端末が外部のAPIと通信するのは直近のEdgeサーバーのみであり、Centralサーバーとは直接通信しない階層的なプライバシー保護（HFL）アーキテクチャを実現しています。