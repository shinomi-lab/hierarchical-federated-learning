# ラウンド同期スタック問題 — 調査報告

調査日: 2026-05-07

## 症状

- エッジサーバ1の端末は2ラウンド目に移行しているのに、エッジサーバ2の端末は1ラウンド目で停滞（またはその逆）
- 学習リセットボタンを押して、アプリをタスクキルせずに次の試行に進むときに多発

---

## 原因1: 端末側 — リセット後のラウンド状態不整合（構造的問題・最大要因）

### 発見事項

端末には**2つの独立したラウンド管理**が存在し、リセット時に片方しかクリアされない：

| 管理場所 | 用途 | リセット時 |
|---------|------|-----------|
| `TrainingViewModel.lastKnownRound` + `SharedPreferences("training_prefs", "last_known_round")` | 学習ロジック、アップロード時のround_id | **クリアされる** ✓ |
| `SharedPreferences("hfl_prefs", "last_applied_round")` | WebSocket ACK送信時のround | **クリアされない** ✗ |

### リセット後のフロー（問題の再現）

```
1. ユーザが「学習リセット」を押す
2. TrainingViewModel.fullReset() が実行される
   → lastKnownRound = null
   → "training_prefs"/"last_known_round" を削除
   → "hfl_prefs"/"last_applied_round" はそのまま残る！
3. WebSocket接続は切断されない（そのまま維持）
4. サーバが新ラウンドのモデルをブロードキャスト
5. WebSocketListenerImpl が受信
   → persistLastAppliedRound(新ラウンド) で "hfl_prefs" 更新
   → viewModel.onNewRoundReceived(新ラウンド) を呼ぶ
6. しかし TrainingViewModel 内の状態はリセット直後で
   fullDataset が空、isTrainingDataReady = false
   → 学習が開始できず停滞
```

### もう一つの問題: `restoreRoundState()` が未使用

`TrainingViewModel` に `restoreRoundState()` メソッドが定義されている（L1781-1800）が、**一度も呼ばれていない**。ViewModel初期化時にラウンド状態を復元する手段がない。

---

## 原因2: エッジサーバ間のラウンド非同期（構造的問題）

### アーキテクチャ

```
Central Server (port 8000)
    ↑ /edge_update (集約結果を受信)
    ↓ /get_global_model (ポーリングで配信)
Edge Server 01 (port 8001) ←→ 端末群A
Edge Server 02 (port 8002) ←→ 端末群B
```

### 問題: エッジサーバは完全に独立

- 各エッジは**独立したプロセス**で動作し、状態を共有しない
- 各エッジが**独立してセントラルをポーリング**（5秒間隔）
- ラウンド進行の検知タイミングにズレが生じる

### レースコンディション

```
時刻0: Central が round 2 のモデルを準備完了
時刻1: Edge-01 がポーリング → round 2 を検知 → 端末に通知
時刻4: Edge-02 がポーリング → round 2 を検知 → 端末に通知
       ↑ 3秒間、Edge-02の端末はまだ round 1 にいる
```

さらに悪化するケース:
- Edge-01 の集約閾値 = 2（端末2台待ち）
- Edge-02 の集約閾値 = 1（端末1台で即集約）
- → Edge-02 が先に集約完了 → Central に先に送信 → Central が先にラウンドを進める可能性

### ポートと接続の問題

各端末は**1つのエッジサーバにのみ接続**（WebSocket + REST）。端末がどのエッジに接続するかは `BuildConfig.EDGE_BASE_URL` で固定。

- 端末A群 → 192.168.11.2:8001 (Edge-01)
- 端末B群 → 192.168.11.2:8002 (Edge-02)

ポート自体の問題はないが、**2つのエッジが異なるラウンドにいる期間**が問題。

---

## 原因3: WebSocketとポーリングの競合（通信的問題）

### 通知経路の二重性

端末がラウンド進行を検知する経路が2つある：

1. **WebSocket push**: エッジが `broadcast_model_update()` で即時通知
2. **ポーリング**: 端末が `/api/v1/meta` を定期的にGET

リセット後にWebSocket通知を受けても、端末側の学習データが未準備のため無視される。その後ポーリングも止まっているため、新ラウンドの検知手段がなくなる。

### 409 Round Mismatch の連鎖

```
1. 端末がリセット後、古いlastKnownRoundでウェイトをアップロード
2. エッジが 409 Conflict を返す（ラウンド不一致）
3. エッジのレスポンス: "Get /api/v1/meta first"
4. しかし端末側でこの409を適切にハンドリングしていない場合がある
   → リトライループに入るが、ラウンドを更新しないまま再試行
   → スタック
```

---

## 推論: なぜ「リセット後タスクキルなし」で多発するか

```
タスクキルあり:
  プロセス再起動 → WebSocket新規接続 → 全SharedPreferences読み直し
  → "hfl_prefs"に古いroundがあっても、ViewModel初期化で/metaを取得し直す
  → 問題なし

タスクキルなし（リセットのみ）:
  プロセス生存 → WebSocket既存接続を維持 → ViewModelのみリセット
  → WebSocket側のlast_applied_roundが残存
  → ViewModelは lastKnownRound=null で学習不能
  → 次のWebSocket通知が来ても学習データ未準備で無視
  → メタポーリングも停止中 → 永久に新ラウンドに追いつけない
```

---

## 修正案

### A. 端末側（即効性あり・確実）

| # | 修正内容 | ファイル |
|---|---------|---------|
| A1 | `fullReset()` で `"hfl_prefs"/"last_applied_round"` もクリア | TrainingViewModel.kt |
| A2 | `fullReset()` 完了後に `startMetaPolling()` を再開 | TrainingViewModel.kt |
| A3 | ViewModel `init {}` で `restoreRoundState()` を呼ぶ | TrainingViewModel.kt |
| A4 | リセット時にWebSocketを一度切断→再接続 | MainActivity.kt |

### B. サーバ側（根本対策）

| # | 修正内容 | ファイル |
|---|---------|---------|
| B1 | Central がラウンド進行時に全エッジに**push通知**（ポーリング依存を排除） | edge_update.py |
| B2 | エッジ間の集約閾値を統一（現在 [2, 1] → [2, 2]） | start.py |
| B3 | Central が全エッジのラウンド一致を確認してから次ラウンド公開 | edge_update.py |

### C. 通信ロバスト化

| # | 修正内容 | ファイル |
|---|---------|---------|
| C1 | 409応答時に自動で `/api/v1/meta` を取得してラウンド更新 | TrainingViewModel.kt |
| C2 | WebSocket再接続時に必ず `/api/v1/meta` で最新ラウンドを同期 | MainActivity.kt |

---

## 優先順位

1. **A1 + A2** — リセット時の状態不整合を根絶（最小変更で最大効果）
2. **A4** — WebSocketのゾンビ状態を防止
3. **B2** — エッジ間タイミング差を最小化
4. **C1** — 409時の自動リカバリで、万一の不整合でも自己修復

---

## 検証方法

1. 2台のエッジサーバ + 4端末で5ラウンド実行
2. ラウンド3完了後に全端末で「学習リセット」→ タスクキルせず次の試行を開始
3. 全端末が同一ラウンドに揃って学習を再開できることを確認
4. エッジサーバのログで `round_mismatch` 409 が0件であることを確認
