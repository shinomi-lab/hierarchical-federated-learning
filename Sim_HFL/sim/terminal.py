"""
sim/terminal.py
---------------
仮想端末クラス。実機 (LocalTrainer.kt) と同一の学習設定で PyTorch ローカル学習を行う。

実機 LocalTrainer.kt との対応・修正済み項目:
  [修正A] LR スケジューラー:
    LambdaLR は last_epoch=0 が lr=0 になるオフバイワン問題があった。
    Kotlin の LinearWarmupScheduler は「optimizer.step() → scheduler.step()」で
    currentStep をインクリメントしてから LR を更新する。
    → LambdaLR を廃止し、バッチごとに手動で LR を計算・適用する方式に変更。
    これにより 1バッチ目=base_lr、2バッチ目以降=スケジュール値 となり Kotlin と一致。

  [修正B] バッチ順序:
    Kotlin の generateBatches(shuffle=true) は「訓練開始時に1度だけ」シャッフルし、
    全エポックで同じ固定順を繰り返す。
    以前の DataLoader(shuffle=True) はエポックごとにシャッフルしており逆だった。
    → 訓練開始時に randperm で1度だけシャッフルした TensorDataset を作成し、
       shuffle=False の DataLoader で全エポックを回す方式に変更。

  [修正C] LayerNorm の weight_decay:
    Kotlin の AdamW は isNormParam=true のとき weight_decay=0 にする。
    以前は全パラメータに同一の weight_decay を適用していた。
    → LayerNorm の weight/bias を別 param_group (weight_decay=0) に分離。

  [正常] AdamW の β1, β2, ε      : Kotlin と一致 (0.9, 0.999, 1e-8)
  [正常] 勾配クリッピング            : clip_grad_norm_ で実機と同等
  [正常] 損失関数 CrossEntropyLoss  : nn.CrossEntropyLoss で同等
  [正常] データ分割 80/10/10        : _split_dataset で同等
"""
from __future__ import annotations

import copy
import time
from dataclasses import dataclass, field

import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.data import DataLoader, Dataset, Subset, TensorDataset

from .model import APSelectionMLP


@dataclass
class InferenceResult:
    """
    推論フェーズの結果。
    run_inference() が返す。
    実験フローにおける「推論 → AP選択 → 満足度計測」に対応。
    """
    terminal_id:    str
    round_id:       int
    run_id:         str

    inference_acc:  float = 0.0   # テストデータでの分類精度（モデル品質追跡用）
    inference_loss: float = 0.0   # テストデータでの損失

    # ── 実機 TrainingViewModel の satisfaction_before / satisfaction_after に対応 ──
    satisfaction_before: float = 0.0  # 切替前APの満足度 (TerminalSatisfaction 式)
    satisfaction:        float = 0.0  # 切替後APの満足度 (= satisfaction_after)

    current_ap:     int   = 0     # 推論前の接続AP（0=AP_A, 1=AP_B）
    predicted_ap:   int   = 0     # モデルが推奨するAP（AP TP/RTT 特徴量から判断）
    switched:       bool  = False # APを変更したか

    predicted_tier: int   = 0     # 現在AP向けにモデルが予測した満足度ティア (0〜3)
    ap_confidence:  float = 0.0   # 現在APへのモデル確信度 (softmax最大値)
    avg_confidence: float = 0.0   # テストデータ全体での予測確信度平均
    n_test_samples: int   = 0


@dataclass
class TrainingResult:
    terminal_id: str
    round_id:    int
    run_id:      str

    train_loss:  float = 0.0
    train_acc:   float = 0.0
    val_loss:    float = 0.0
    val_acc:     float = 0.0
    test_loss:   float = 0.0
    test_acc:    float = 0.0
    n_samples:   int   = 0
    epochs_done: int   = 0

    weight_norm:       float = 0.0
    weights_f32:       bytes = field(default_factory=bytes, repr=False)
    train_duration_ms: float = 0.0


# ------------------------------------------------------------------ #
# LR スケジュール計算（Kotlin LinearWarmupScheduler と完全一致）
# ------------------------------------------------------------------ #
def _kotlin_lr(
    step:         int,    # 1 始まり（Kotlin は currentStep++ してから計算）
    base_lr:      float,
    warmup_steps: int,
    total_steps:  int,
    min_lr_ratio: float,
) -> float:
    """
    Kotlin LinearWarmupScheduler.step() と同一の LR 計算。
    step は optimizer.step() 後にインクリメントされた値（1 始まり）。
    """
    if step < warmup_steps:
        return base_lr * (step / max(1, warmup_steps))
    elif step > total_steps:
        return base_lr * min_lr_ratio
    else:
        decay_ratio = (step - warmup_steps) / max(1, total_steps - warmup_steps)
        return base_lr * ((1.0 - min_lr_ratio) * (1.0 - decay_ratio) + min_lr_ratio)


def _set_lr(optimizer: optim.Optimizer, lr: float) -> None:
    """全 param_group の lr を一括更新"""
    for pg in optimizer.param_groups:
        pg["lr"] = lr


# ------------------------------------------------------------------ #
# データ分割（LocalTrainer.kt init と同一: 80/10/10 逐次分割）
# ------------------------------------------------------------------ #
def _split_dataset(
    dataset: Dataset,
    seed:    int = 42,
) -> tuple[Dataset, Dataset, Dataset]:
    """
    Kotlin LocalTrainer の init と同一ロジック:
      trainCount = (n * 0.8).toInt()
      valCount   = (n * 0.1).toInt()
      testCount  = n - trainCount - valCount
    インデックスは「先頭から順に」割り当てる（random_split ではない）。
    Kotlin の userIndices.subList(0, trainCount) に対応。
    """
    n          = len(dataset)
    train_n    = int(n * 0.8)
    val_n      = int(n * 0.1)
    test_n     = n - train_n - val_n

    # Kotlin と同一のガード処理
    if n > 0 and train_n < 1:
        train_n = 1
    if test_n <= 0 and n - train_n > 0:
        test_n  = 1
        val_n   = n - train_n - test_n
    if val_n <= 0 and n - train_n - test_n > 0:
        val_n   = 1
        train_n = n - val_n - test_n
    if train_n + val_n + test_n != n:
        train_n = n - val_n - test_n
        if train_n < 1:
            train_n = 1

    # 逐次インデックスで分割（Kotlin の subList と同一）
    all_idx  = list(range(n))
    idx_train = all_idx[:train_n]
    idx_val   = all_idx[train_n : train_n + val_n]   if val_n  > 0 else []
    idx_test  = all_idx[train_n + val_n:]             if test_n > 0 else []

    return (
        Subset(dataset, idx_train),
        Subset(dataset, idx_val),
        Subset(dataset, idx_test),
    )


# ------------------------------------------------------------------ #
# バッチ順固定のための TensorDataset 生成
# （Kotlin generateBatches(shuffle=true) に対応）
# ------------------------------------------------------------------ #
def _make_fixed_shuffled_loader(
    dataset:    Dataset,
    batch_size: int,
    seed:       int,
) -> DataLoader:
    """
    Kotlin の generateBatches(shuffle=true) と同様に「1度だけ」シャッフルし、
    全エポックで同じ順序を保つ DataLoader を返す。

    DataLoader(shuffle=True) は毎エポックシャッフルするため Kotlin と逆になる。
    → TensorDataset に変換して shuffle=False で返す。
    空データセットの場合は空の DataLoader を返す（クラッシュを防止）。
    """
    n = len(dataset)
    if n == 0:
        # 空バッチ (学習ループが 0 回回るだけで安全)
        dummy_X = torch.zeros(0, 1)
        dummy_y = torch.zeros(0, dtype=torch.long)
        return DataLoader(TensorDataset(dummy_X, dummy_y), batch_size=batch_size, shuffle=False)

    xs, ys = [], []
    for i in range(n):
        x, y = dataset[i]
        xs.append(x)
        ys.append(y)

    g    = torch.Generator().manual_seed(seed)
    perm = torch.randperm(n, generator=g).tolist()

    X = torch.stack([xs[i] for i in perm])
    y = torch.tensor([ys[i].item() if isinstance(ys[i], torch.Tensor) else ys[i] for i in perm],
                     dtype=torch.long)

    return DataLoader(TensorDataset(X, y), batch_size=batch_size, shuffle=False)


# ------------------------------------------------------------------ #
# 仮想端末
# ------------------------------------------------------------------ #
class SimTerminal:
    """
    仮想 Android 端末。
    LocalTrainer.kt と同一の学習設定で実際の PyTorch 学習を行い、
    FedAvg 用の重みを返す。
    """

    def __init__(
        self,
        terminal_id:   str,
        local_data:    Dataset,
        input_size:    int   = 4,
        hidden_size:   int   = 8,
        output_size:   int   = 2,
        dropout_p:     float = 0.0,
        lr:            float = 0.01,
        weight_decay:  float = 1e-4,
        clip_max_norm: float = 1.0,
        warmup_steps:  int   = 0,
        total_steps:   int   = 0,   # 0 → runner から渡された値 or epochs×batches
        min_lr_ratio:  float = 0.0,
        batch_size:    int   = 32,
        seed:          int   = 42,
    ) -> None:
        self.terminal_id   = terminal_id
        self.local_data    = local_data
        self.lr            = lr
        self.weight_decay  = weight_decay
        self.clip_max_norm = clip_max_norm
        self.warmup_steps  = warmup_steps
        self._total_steps  = total_steps
        self.min_lr_ratio  = min_lr_ratio
        self.batch_size    = batch_size
        self.seed          = seed

        # 80/10/10 分割（LocalTrainer.kt と同一）
        self.train_ds, self.val_ds, self.test_ds = _split_dataset(local_data, seed)

        self.model = APSelectionMLP(input_size, hidden_size, output_size, dropout_p)
        self._input_size  = input_size
        self._hidden_size = hidden_size
        self._output_size = output_size
        self._dropout_p   = dropout_p

        # AP接続状態（初期値はシードから決定）AP は 0 or 1 の2種類
        self._current_ap: int = seed % 2

    # ------------------------------------------------------------------ #
    # グローバルモデルから端末モデルを初期化
    # ------------------------------------------------------------------ #
    def set_global_weights(self, global_model: APSelectionMLP) -> None:
        self.model.load_state_dict(copy.deepcopy(global_model.state_dict()))

    # ------------------------------------------------------------------ #
    # ローカル学習（LocalTrainer.kt updateWeightsAndGetLoss に対応）
    # ------------------------------------------------------------------ #
    def local_train(self, epochs: int, run_id: str, round_id: int) -> TrainingResult:
        # [修正B] 1度だけシャッフルした固定順 DataLoader
        train_loader = _make_fixed_shuffled_loader(
            self.train_ds,
            batch_size = self.batch_size,
            seed       = self.seed + round_id,   # ラウンドごとに異なるシャッフル
        )
        n_batches   = max(1, len(train_loader))
        total_steps = self._total_steps if self._total_steps > 0 else epochs * n_batches

        criterion = nn.CrossEntropyLoss()

        # [修正C] LayerNorm に weight_decay=0 を適用（Kotlin isNormParam と同一）
        norm_param_ids = {
            id(p)
            for m in [self.model.norm1, self.model.norm2]
            for p in m.parameters()
        }
        other_params = [p for p in self.model.parameters() if id(p) not in norm_param_ids]
        norm_params  = [p for p in self.model.parameters() if id(p) in norm_param_ids]

        optimizer = optim.AdamW(
            [
                {"params": other_params, "weight_decay": self.weight_decay},
                {"params": norm_params,  "weight_decay": 0.0},
            ],
            lr    = self.lr,
            betas = (0.9, 0.999),
            eps   = 1e-8,
        )

        t0 = time.monotonic()
        total_train_loss, total_train_correct, total_train_samples = 0.0, 0, 0

        # [修正A] 手動 LR 更新（Kotlin LinearWarmupScheduler と完全一致）
        step = 0   # optimizer.step() 後にインクリメント（1始まりで計算）

        for _ in range(epochs):
            self.model.train()
            for X, y in train_loader:
                optimizer.zero_grad()
                logits = self.model(X)
                loss   = criterion(logits, y)
                loss.backward()

                # 勾配クリッピング（Kotlin clipMaxNorm に対応）
                torch.nn.utils.clip_grad_norm_(self.model.parameters(), self.clip_max_norm)

                optimizer.step()

                # Kotlin: optimizer.step() の後に scheduler.step() で LR を更新
                step += 1
                if self.warmup_steps > 0 or total_steps > 0:
                    new_lr = _kotlin_lr(
                        step, self.lr, self.warmup_steps, total_steps, self.min_lr_ratio
                    )
                    _set_lr(optimizer, new_lr)

                total_train_loss    += loss.item() * len(y)
                total_train_correct += (logits.argmax(1) == y).sum().item()
                total_train_samples += len(y)

        train_duration_ms = (time.monotonic() - t0) * 1000
        train_loss = total_train_loss    / max(total_train_samples, 1)
        train_acc  = total_train_correct / max(total_train_samples, 1)

        val_loss,  val_acc  = self._evaluate(self.val_ds)
        test_loss, test_acc = self._evaluate(self.test_ds)

        return TrainingResult(
            terminal_id       = self.terminal_id,
            round_id          = round_id,
            run_id            = run_id,
            train_loss        = round(train_loss,       6),
            train_acc         = round(train_acc,        6),
            val_loss          = round(val_loss,         6),
            val_acc           = round(val_acc,          6),
            test_loss         = round(test_loss,        6),
            test_acc          = round(test_acc,         6),
            n_samples         = len(self.train_ds),
            epochs_done       = epochs,
            weight_norm       = round(self.model.weight_norm(), 6),
            weights_f32       = self.model.to_f32_flat(),
            train_duration_ms = round(train_duration_ms, 2),
        )

    # ------------------------------------------------------------------ #
    # 推論・AP選択・満足度計測（実験フロー: ローカル学習後に実行）
    # ------------------------------------------------------------------ #
    def run_inference(
        self,
        run_id:   str,
        round_id: int,
        ap_conditions: list[tuple[float, float]] = None,
        app_idx:  int   = 0,
        best_ap:  int   = 0,
        n_on_ap:  list[int] = None,
    ) -> InferenceResult:
        if ap_conditions is None:
            ap_conditions = [(0.0, 0.0)] * 2
        if n_on_ap is None:
            n_on_ap = [0] * len(ap_conditions)

        from .data_gen import terminal_satisfaction, APP_COUNT, APSelectionDataset

        # ── 現在接続APのTP/RTT を選択 ────────────────────────────────
        cur_ap  = self._current_ap
        if cur_ap >= len(ap_conditions):
            cur_ap = 0 # fallback
            self._current_ap = cur_ap
        ap_tp, ap_rtt = ap_conditions[cur_ap]

        # ── ① 特徴量構築 (実測TP/RTTを入力 — 実機 start.py L1106 と同一) ──
        #    実機: inp = [tp, rtt] + one_hot[:4]
        #    tp/rtt は端末が現在APで実測した値
        TP_MAX  = APSelectionDataset.TP_MAX
        RTT_MAX = APSelectionDataset.RTT_MAX

        tp_norm  = float(min(max(ap_tp  / TP_MAX,  0.0), 1.0))
        rtt_norm = float(min(max(ap_rtt / RTT_MAX, 0.0), 1.0))

        one_hot = [0.0] * APP_COUNT
        one_hot[app_idx] = 1.0

        base_features = [tp_norm, rtt_norm] + one_hot  # 6次元

        # AP状態特徴量の追加（input_size > 6 の場合）
        if self._input_size > 6:
            ap_state_features = []
            for a in range(len(ap_conditions)):
                a_tp, a_rtt = ap_conditions[a]
                a_n = n_on_ap[a] if a < len(n_on_ap) else 0
                ap_state_features.append(float(min(max(a_tp  / APSelectionDataset.AP_TP_MAX,  0.0), 1.0)))
                ap_state_features.append(float(min(max(a_rtt / APSelectionDataset.AP_RTT_MAX, 0.0), 1.0)))
                ap_state_features.append(float(min(max(a_n   / APSelectionDataset.AP_N_MAX,   0.0), 1.0)))
            base_features = base_features + ap_state_features

        feature = torch.tensor(
            base_features, dtype=torch.float32
        ).unsqueeze(0)   # shape: [1, input_size]

        # ── ② モデル推論 (AP IDを直接予測する) ──────────────────
        self.model.eval()
        with torch.no_grad():
            ap_logits      = self.model(feature)
            ap_probs       = torch.softmax(ap_logits, dim=1)
            predicted_ap   = int(ap_probs.argmax(dim=1).item())
            ap_confidence  = float(ap_probs.max().item())

        # ── ③ AP選択 ─────────────────────────────────────────────────
        switched         = predicted_ap != cur_ap
        self._current_ap = predicted_ap

        # ── ④ 満足度 (TerminalSatisfaction.calculateSatisfaction と同一式) ──
        satisfaction_before = terminal_satisfaction(app_idx, ap_tp, ap_rtt)

        if predicted_ap >= len(ap_conditions):
            predicted_ap = 0 # fallback
            
        new_tp, new_rtt = ap_conditions[predicted_ap]
        satisfaction = terminal_satisfaction(app_idx, new_tp, new_rtt)

        # ── ⑤ テストデータで汎化精度を計測 ──────────
        inference_acc = inference_loss = avg_confidence = 0.0
        n_test_samples = 0

        if len(self.test_ds) > 0:
            loader    = DataLoader(self.test_ds, batch_size=256, shuffle=False)
            criterion = nn.CrossEntropyLoss()
            all_confs: list[float] = []
            total_loss, total_correct, total_samples = 0.0, 0, 0

            with torch.no_grad():
                for X, y in loader:
                    logits = self.model(X)
                    probs  = torch.softmax(logits, dim=1)
                    loss   = criterion(logits, y)

                    total_loss    += loss.item() * len(y)
                    total_correct += (logits.argmax(1) == y).sum().item()
                    total_samples += len(y)
                    all_confs.extend(probs.max(dim=1).values.tolist())

            inference_acc  = total_correct / max(total_samples, 1)
            inference_loss = total_loss    / max(total_samples, 1)
            avg_confidence = sum(all_confs) / max(len(all_confs), 1)
            n_test_samples = total_samples

        self.model.train()

        return InferenceResult(
            terminal_id          = self.terminal_id,
            round_id             = round_id,
            run_id               = run_id,
            inference_acc        = round(inference_acc,        6),
            inference_loss       = round(inference_loss,       6),
            satisfaction_before  = round(satisfaction_before,  6),
            satisfaction         = round(satisfaction,         6),
            current_ap           = cur_ap,
            predicted_ap         = predicted_ap,
            switched             = switched,
            predicted_tier       = predicted_ap, # ティア判定を廃止したため AP ID を格納
            ap_confidence        = round(ap_confidence,        6),
            avg_confidence       = round(avg_confidence,       6),
            n_test_samples       = n_test_samples,
        )

    # ------------------------------------------------------------------ #
    # 評価（LocalTrainer.kt inferenceOnBatches に対応）
    # ------------------------------------------------------------------ #
    def _evaluate(self, dataset: Dataset) -> tuple[float, float]:
        if dataset is None or len(dataset) == 0:
            return 0.0, 0.0

        loader    = DataLoader(dataset, batch_size=256, shuffle=False)
        criterion = nn.CrossEntropyLoss()
        self.model.eval()

        total_loss, total_correct, total_samples = 0.0, 0, 0
        with torch.no_grad():
            for X, y in loader:
                logits         = self.model(X)
                loss           = criterion(logits, y)
                total_loss    += loss.item() * len(y)
                total_correct += (logits.argmax(1) == y).sum().item()
                total_samples += len(y)

        self.model.train()
        return (
            total_loss    / max(total_samples, 1),
            total_correct / max(total_samples, 1),
        )
