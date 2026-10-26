# エッジサーバ推論アルゴリズム仕様（実装用プロンプト）

- 目的：統合済みの `global_model` を用いて各端末の接続先 AP インデックスを決定する。
- 前提入力（端末ごと）
  - `tpNeed`：端末が要求するスループット（学習時の単位に合わせる）
  - `rttNeed`：端末が要求する RTT（学習時の単位に合わせる）
  - `appNum`：アプリ種別（整数）→ `apNumMax` 長のワンホットに変換
- 前処理
  1. `tpNeed` と `rttNeed` を学習時に用いた `tp_mean,tp_std,rtt_mean,rtt_std` で正規化：`(x-mean)/(std+1e-8)`
  2. `appNum = appNum % apNumMax`（教師ラベルと AP 数が合わない可能性に備える）
  3. ワンホット化して `x = concat([tp_norm, rtt_norm, one_hot(appNum)])` を作る
  4. バッチ化して `tensor`（shape: `[N, input_dim]`）を作成
- モデル推論
  - 実行条件：`global_model.eval()`、`with torch.no_grad():`、`to(device)`（CPU/GPU）
  - 出力：`outputs = global_model(x)`（logits）
  - 予測ラベル：`pred = torch.argmax(outputs, dim=1).cpu().numpy().astype(int)`
  - 追加（任意）：`probs = torch.softmax(outputs, dim=1)` → `conf = probs.max(dim=1)` を計算し信頼度を評価
- 後処理・安全化
  1. 範囲検査：各 `pred[i]` が `0 <= pred < apNumMax` でなければ `mapped = pred % apNumMax` としてフォールバックし、ログで警告を出す
  2. 低信頼対応（任意）：`conf[i] < threshold` の場合は（a）直近の割当を維持、または（b）負荷の最も低い AP を選ぶ、といったルールにフォールバックする
  3. 最終割当を `TERMS[i].setSwitchAp(ap_index)` に反映する
- 運用の注意
  - 推論はバッチ処理して効率化する（端末数が多い場合は適切な `batch_size` を選ぶ）
  - 推論時は勾配を無効化してリソース節約する（`torch.no_grad()`）
  - サービング安全性：学習中に `global_model` を直接差し替えると推論中に不整合が起きるため、公開用コピー `serving_model = copy.deepcopy(global_model)` を作り、完了時に原子的に差し替える（またはロック／バージョン管理）
  - 学習ラベルと物理 `apNumMax` が不一致なケースは警告を出して運用者に通知する
- 擬似コード（実装例）

```python
# 前処理（N terminals）
tp_norm = (tpNeed_array - tp_mean) / (tp_std + 1e-8)
rtt_norm = (rttNeed_array - rtt_mean) / (rtt_std + 1e-8)
app_idx = (appNum_array % apNumMax)
app_onehot = one_hot(torch.tensor(app_idx), num_classes=apNumMax)
x = torch.cat([torch.tensor(tp_norm).unsqueeze(1), torch.tensor(rtt_norm).unsqueeze(1), app_onehot.float()], dim=1).to(device)

# 推論
serving_model.eval()
with torch.no_grad():
    outputs = serving_model(x)          # logits
    probs = torch.softmax(outputs, dim=1)
    pred = torch.argmax(outputs, dim=1).cpu().numpy()
    conf = probs.max(dim=1).cpu().numpy()

# 後処理
final_assign = []
for i, p in enumerate(pred):
    if p < 0 or p >= apNumMax:
        mapped = int(p % apNumMax)
        logger.warn(f"pred {p} out-of-range -> mapped {mapped}")
        p = mapped
    if conf[i] < CONF_THRESHOLD:
        p = fallback_rule(i)  # 例: 直近割当 or 最も空いているAP
    final_assign.append(int(p))

# 反映
for i, ap_idx in enumerate(final_assign):
    TERMS[i].setSwitchAp(ap_idx)
```

これがシミュレーションでの推論の内容だ。
