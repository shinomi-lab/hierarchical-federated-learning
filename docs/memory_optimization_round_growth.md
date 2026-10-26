# ラウンド中メモリ肥大対策 — 変更まとめ

適用日: 2026-05-07
対象: `HFL_terminals/app/src/main/java/com/example/hfl_experiment/`

---

## 背景

Android端末で連合学習ラウンドを複数回実行すると、ラウンドごとにメモリ使用量が単調増加し、最終的にOOMに至る問題。
原因はラウンド「実行中」に発生する一時オブジェクトの大量生成とGC圧の蓄積であり、ライフサイクル終了時のリソース解放（onCleared等）では対処できない。

---

## 変更一覧

### 1. LinearLayer / LayerNorm / ReLU のバッファ再利用（影響度: 最大）

**ファイル**: `training/core/LocalTrainer.kt`

**問題**: `forward()` が毎バッチ呼ばれるたびに `List(batchSize) { FloatArray(outputDim) }` 等を新規作成。
25エポック × (データ数/16)バッチ × 5サイクル = 数千回のFloatArray確保でGC圧が極めて高い。

**修正**: 各レイヤークラスに `outputBuf: Array<FloatArray>?` フィールドを追加し、バッチサイズが同じならバッファを再利用する。

```kotlin
// LinearLayer
private var outputBuf: Array<FloatArray>? = null

fun forward(inputs: List<FloatArray>): LayerOutput {
    val buf = outputBuf?.takeIf { it.size == batchSize && ... }
        ?: Array(batchSize) { FloatArray(outputDim) }.also { outputBuf = it }
    // buf に書き込んで返す
}
```

同様に `LayerNorm` は `outputBuf`, `normBuf`, `meansBuf`, `variancesBuf` の4つを再利用。
`ReLU` も `outputBuf` を保持。

**効果**: forward中の一時FloatArray確保がほぼゼロになる。

---

### 2. LocalTrainer 再生成前の旧インスタンス解放

**ファイル**: `training/TrainingViewModel.kt` (2箇所)

**問題**: `performLocalTrainingSuspend()` で新しい `LocalTrainer` を代入する際、旧インスタンスがまだ参照されているため、一瞬2つのトレーナー（モデルパラメータ + Adam状態）がメモリに共存する。

**修正**: 新規生成の直前に `localTrainer = null` を明示的に挿入。

```kotlin
// Release previous trainer to avoid two instances coexisting in memory
localTrainer = null

localTrainer = LocalTrainer(...)
```

**効果**: ピークメモリがモデルサイズ分（パラメータ + Adam m/v状態 = モデルの約3倍）削減。

---

### 3. forwardCache のバッチ間即時解放

**ファイル**: `training/core/LocalTrainer.kt` — `MLPModel.backward()`

**問題**: `forwardCache` はforward時に中間結果を保存し、backward完了まで保持する。
しかし `forward()` 先頭の `forwardCache.clear()` は次バッチのforward開始時にしか呼ばれないため、
backward完了〜次バッチforward開始の間、不要な中間データがGC不可のまま残る。

**修正**: `backward()` の最後に `forwardCache.clear()` を追加。

```kotlin
fun backward(dLogits: List<FloatArray>): Map<String, Gradients> {
    // ... backward計算 ...
    forwardCache.clear()  // ← 追加
    return mapOf(...)
}
```

**効果**: 中間結果のメモリ保持期間が最小化され、GCが早期に回収可能。

---

### 4. websocketEvents の無制限成長を制限

**ファイル**: `training/TrainingViewModel.kt`

**問題**: `websocketEvents: MutableList<Pair<Long, String>>` がラウンドをまたいで無制限に成長。
一度も `clear()` されないため、長時間実行で数MBに達する。

**修正**: `recordWebSocketEvent()` でリストサイズが `MAX_WEBSOCKET_EVENTS (200)` を超えたら古いものから削除。

```kotlin
private companion object {
    private const val MAX_WEBSOCKET_EVENTS = 200
}

fun recordWebSocketEvent(event: String) {
    websocketEvents.add(ts to event)
    while (websocketEvents.size > MAX_WEBSOCKET_EVENTS) {
        websocketEvents.removeAt(0)
    }
}
```

**効果**: メモリ使用量に上限が設定され、長時間実行でも一定。

---

### 5. WeightBinLoader のバッファ再利用

**ファイル**: `training/WeightBinLoader.kt`

**問題**: `loadAllTensors()` がテンソルごとに `ByteArray(lengthBytes)` を新規確保。
モデルロードのたびに（各ラウンド開始時）全テンソル分の一時バッファが並行して確保される。

**修正**: ループ前に全テンソルの最大サイズを事前スキャンし、1つの `reusableBuf` を確保して全テンソルで共有。

```kotlin
var maxBytes = 0
for (i in 0 until tensors.length()) {
    val lb = tensors.getJSONObject(i).optInt("length_bytes", 0)
    if (lb > maxBytes) maxBytes = lb
}
val reusableBuf = ByteArray(maxBytes)

for (i in 0 until tensors.length()) {
    raf.readFully(reusableBuf, 0, lengthBytes)
    val bb = ByteBuffer.wrap(reusableBuf, 0, lengthBytes)
    // ...
}
```

**効果**: テンソル数分のByteArray確保が1回に削減。GCスパイクが消える。

---

## 検証方法

1. **Android Studio Memory Profiler** でヒープ推移を観察
   - 5サイクル学習中にヒープがフラット（鋸歯状でOK、単調増加はNG）であること
   - 各サイクル間でヒープが前サイクル開始時と同等に戻ること

2. **adb shell dumpsys meminfo com.example.hfl_experiment**
   - ラウンド1開始時とラウンド5完了時のDalvik Heapを比較
   - 差分が10MB以内であること（以前は50MB以上増加していた想定）

3. **RealTimeLogger でのメモリサンプリング**
   - `lastHeapBeforeBytes` / `lastHeapAfterBytes` が各サイクルで安定していること

---

## 補足: 同日に適用したライフサイクル系修正（別目的）

以下はラウンド中の肥大とは無関係だが、アプリ終了時のリソースリーク対策として同日適用：

| ファイル | 修正 |
|---------|------|
| `AndroidManifest.xml` | `android:largeHeap="true"` |
| `TrainingViewModel.kt` | `onCleared()` で decisionEngine/localTrainer/networkClient 解放 |
| `DeviceAckClient.kt` | `stop()` に awaitTermination + connectionPool.evictAll |
| `NetworkClient.kt` | `shutdown()` メソッド追加 |
| `ModelLoader.kt` | `release()` メソッド追加 |
| `MainActivity.kt` | `onDestroy()` で wsClient リソース解放 |
| `UploadMetricsWorker.kt` | try/finally で NetworkClient.shutdown() |
