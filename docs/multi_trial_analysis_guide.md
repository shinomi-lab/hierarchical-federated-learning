# Multi-Trial Analysis Guide

HFL の実機試行を複数まとめて横断分析するためのガイド。

## Scripts

| Script | Purpose |
|--------|---------|
| `scripts/analyze_latest_trial.py` | 最新1試行の詳細分析 (20種プロット) |
| `scripts/analyze_multi_trial.py` | N試行の横断比較分析 (8種プロット) |

## Quick Start

```bash
cd Serverside_HFL

# 直近5試行を自動選択して分析
python3 scripts/analyze_multi_trial.py -n 5

# 直近10試行 (短すぎるログは自動除外)
python3 scripts/analyze_multi_trial.py -n 10 --min-lines 500

# 全試行
python3 scripts/analyze_multi_trial.py --all

# インタラクティブに選択
python3 scripts/analyze_multi_trial.py --interactive

# 特定の試行を指定
python3 scripts/analyze_multi_trial.py --select \
    logs/time_records/central_server_20260429_171040.log \
    logs/time_records/central_server_20260429_143011.log

# JSON サマリーも出力
python3 scripts/analyze_multi_trial.py -n 5 --json-summary
```

## Output

`analysis_output/multi_trial_<timestamp>_<N>trials/` に以下が生成される:

```
multi_trial_20260430_111824_5trials/
  multi_trial_analysis.md          # Markdown レポート
  multi_trial_summary.json         # JSON サマリー (--json-summary 時)
  cross_final_accuracy_loss.png    # 試行間: 最終Acc/Loss 比較
  cross_accuracy_curves.png        # 試行間: Accuracy学習曲線 重ね描き
  cross_loss_curves.png            # 試行間: Loss学習曲線 重ね描き
  cross_satisfaction.png           # 試行間: 満足度 Before/After 比較
  cross_training_time.png          # 試行間: 平均学習時間
  cross_trial_overview.png         # 全試行サマリーテーブル
  cross_rounds_terminals.png       # 試行間: ラウンド数/端末数
  cross_upload_latency.png         # 試行間: アップロードレイテンシ
```

## Options

| Option | Default | Description |
|--------|---------|-------------|
| `-n N` / `--last N` | 5 | 直近N試行を分析 |
| `--all` | - | 全試行を分析 |
| `--select PATH...` | - | 特定のログファイルを指定 |
| `--interactive` | - | 一覧から番号で選択 |
| `--min-lines N` | 100 | N行未満のログは除外 (短い試行をスキップ) |
| `--out-dir DIR` | `analysis_output` | 出力先ベースディレクトリ |
| `--json-summary` | - | JSON サマリーも出力 |
| `--no-plots` | - | プロットをスキップ (レポートのみ) |

## Interactive Mode

`--interactive` を指定すると、利用可能な試行一覧が表示される:

```
Available trials:
--------------------------------------------------------------------------------
  [ 1] central_server_20260429_171040  (2026-04-29 17:10, 3min, 2680 lines)
  [ 2] central_server_20260429_164821  (2026-04-29 16:48, 4min, 2236 lines)
  [ 3] central_server_20260429_143011  (2026-04-29 14:30, 2min, 1821 lines)
  ...
--------------------------------------------------------------------------------
Enter numbers separated by spaces (e.g. '1 3 5'), range (e.g. '1-5'), or 'all':
> 1 3 5
```

## Single Trial Analysis (参考)

1試行の詳細分析は従来通り:

```bash
# 最新1試行
python3 scripts/analyze_latest_trial.py

# 特定のログを指定
python3 scripts/analyze_latest_trial.py --central logs/time_records/central_server_20260429_171040.log
```

出力される20種のプロット:

| Category | Plots |
|----------|-------|
| Learning | `plot_accuracy_loss`, `plot_accuracy_delta`, `plot_convergence_overview` |
| Training | `plot_local_ms_by_round`, `plot_mean_epoch_ms`, `plot_training_distribution` |
| Satisfaction | `plot_satisfaction_combined`, `plot_satisfaction_by_app` |
| Network/QoS | `plot_wifi_rssi`, `plot_network_rtt_bandwidth`, `plot_upload_latency` |
| Resources | `plot_battery`, `plot_memory_usage` |
| App Analysis | `plot_accuracy_by_app`, `plot_samples_by_app`, `plot_data_samples` |
| Infrastructure | `plot_edge_aggregation`, `plot_central_timeline`, `plot_weight_norm_latency` |
| Summary | `plot_device_summary`, `plot_ap_distribution` |

## Data Sources

両スクリプトが活用するデータソース:

| Source | Path | Data |
|--------|------|------|
| Central log | `logs/time_records/central_server_*.log` | Aggregation, edge updates, timing |
| Edge JSONL | `logs/time_records/edge_server_*.log`, `logs/rounds/r*/` | Telemetry, weights received |
| Index telemetry | `received_files/terminal_telemetry/*/index.jsonl` | Device info, WiFi RSSI, RTT, battery, memory, accuracy, loss, satisfaction |
| Upload events | `logs/events/edge_terminal_update/*.jsonl` | Upload timing (start -> saved) |
| Edge agg meta | `received_edges/r*/edge-server-*/agg_*.meta.json` | Client count, sample count |
| Manifests | `received_files/terminal_updates/**/manifest_*.json` | SHA256, round, terminal |
