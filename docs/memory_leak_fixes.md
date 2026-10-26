# HFL Android端末 メモリリーク対策ガイド

調査日: 2026-05-06
対象: `HFL_terminals/app/src/main/java/com/example/hfl_experiment/`

---

## CRITICAL（即修正）

### 1. TrainingViewModel — onCleared() 未実装

**ファイル**: `training/TrainingViewModel.kt`
**問題**: ViewModel破棄時にDecisionEngine・ModelLoader等のリソースが解放されない。

**修正**:
```kotlin
override fun onCleared() {
    decisionEngine = null
    // modelLoader等の大きなオブジェクトも解放
    super.onCleared()
}
```

---

### 2. DecisionEngine — Context保持

**ファイル**: `experiment/DecisionEngine.kt` (L21)
**問題**: コンストラクタで `private val context: Context` を保持。TrainingViewModelが解放しないとContext（=Application）がリークする。

**修正**: TrainingViewModelの`onCleared()`で`decisionEngine = null`を設定（上記#1で対応）。
加えて、DecisionEngine内でContextが不要な箇所ではApplicationContextを使い、Activityリファレンスを避ける。

---

### 3. DeviceAckClient — Scheduler/OkHttpClient未クローズ

**ファイル**: `device/DeviceAckClient.kt` (L43, L56, L87)
**問題**:
- `ScheduledExecutorService` が `shutdownNow()` だけで `awaitTermination()` なし
- `OkHttpClient` の `dispatcher.executorService` が未シャットダウン
- `stop()` が呼ばれる保証がない

**修正**:
```kotlin
fun stop() {
    webSocket?.close(1000, "client_stop")
    webSocket = null
    scheduler.shutdownNow()
    try { scheduler.awaitTermination(5, TimeUnit.SECONDS) } catch (_: Exception) {}
    client.dispatcher.executorService.shutdown()
    client.connectionPool.evictAll()
}
```

呼び出し側（MainActivity等）の`onDestroy()`で確実に`stop()`を呼ぶこと。

---

### 4. NetworkClient — ストリーム未クローズ & 一時クライアント乱造

**ファイル**: `network/NetworkClient.kt`

#### 4a. downloadToTemp() (L222-231)
**問題**: `BufferedSink` が例外時にクローズされない可能性。

**修正**:
```kotlin
val sink: BufferedSink = tmp.sink().buffer()
try {
    resp.body?.source()?.let { sink.writeAll(it) }
} finally {
    sink.close()
}
```

#### 4b. 一時OkHttpClient (L341-350, L1191-1196)
**問題**: `fetchSendToDeviceShort()` や `measureRtt()` で毎回 `client.newBuilder().build()` を呼ぶ。各インスタンスが内部スレッドプールを持つ。

**修正**: タイムアウト違いのクライアントをキャッシュする、または共通クライアントの`newCall()`でリクエスト単位のタイムアウトを設定する。
```kotlin
// newBuilder() は親のコネクションプールを共有するので
// スレッドプールのリークは実際には軽微。
// ただし大量生成を避けるため、short用クライアントは1つだけ保持：
private val shortClient: OkHttpClient by lazy {
    client.newBuilder()
        .callTimeout(5, TimeUnit.SECONDS)
        .build()
}
```

---

### 5. ModelLoader — FloatArray未解放

**ファイル**: `experiment/ModelLoader.kt` (L28-41)
**問題**: `weights1`, `biases1` 等のFloatArrayがインスタンス生存中ずっと保持される。推論後も解放されない。

**修正**:
```kotlin
fun release() {
    weights1 = null; biases1 = null
    weights2 = null; biases2 = null
    weights3 = null; biases3 = null
}
```

推論完了後に `modelLoader.release()` を呼ぶ。
TrainingViewModelの`onCleared()`でも呼ぶ。

---

### 6. MainActivity — WebSocket/BroadcastReceiver未解除

**ファイル**: `ui/MainActivity.kt` (L108)
**問題**: WebSocketリファレンスとBroadcastReceiverがonDestroy()で適切に解除されていない可能性。

**修正**:
```kotlin
override fun onDestroy() {
    try { unregisterReceiver(telemetryReceiver) } catch (_: Exception) {}
    deviceAckClient?.stop()
    super.onDestroy()
}
```

---

## MODERATE（重要だが緊急度は中）

### 7. AndroidManifest.xml — largeHeap未設定

**ファイル**: `app/src/main/AndroidManifest.xml` (L20-31)
**問題**: ML学習で大きなテンソルを扱うが、`largeHeap` が未設定（デフォルト ~512MB）。

**修正**:
```xml
<application
    android:name=".HflApplication"
    android:largeHeap="true"
    ...>
```

---

### 8. DeviceMetricsCollector — リスト無制限成長

**ファイル**: `telemetry/DeviceMetricsCollector.kt` (L40, L47, L62)
**問題**: `handshakeLatencies`, `handoverOfflineDurations`, `threadWaitDurations` がセッション中に無制限に成長。

**修正**: リストサイズに上限を設ける。
```kotlin
fun recordHandshakeLatency(ms: Long) {
    if (ms > 0 && handshakeLatencies.size < MAX_SAMPLES) {
        handshakeLatencies.add(ms)
    }
}

companion object {
    private const val MAX_SAMPLES = 500
}
```

同様に `handoverOfflineDurations`, `threadWaitDurations` にも適用。

---

### 9. UploadMetricsWorker — NetworkClient再生成

**ファイル**: `worker/UploadMetricsWorker.kt` (L32)
**問題**: `doWork()` 毎に `NetworkClient` を新規作成。OkHttpClient + Retrofitインスタンスが蓄積。

**修正**: アプリレベルのシングルトンNetworkClientを使うか、Worker終了時にクリーンアップする。
```kotlin
override fun doWork(): Result {
    val client = NetworkClient(applicationContext, baseUrl, token)
    try {
        // ... 処理 ...
    } finally {
        client.shutdown()  // NetworkClientにshutdown()メソッドを追加
    }
}
```

NetworkClientに追加：
```kotlin
fun shutdown() {
    client.dispatcher.executorService.shutdown()
    client.connectionPool.evictAll()
}
```

---

### 10. WeightBinLoader — 一時ByteArray蓄積

**ファイル**: `training/WeightBinLoader.kt` (L62-70)
**問題**: テンソルごとに `ByteArray(lengthBytes)` + `FloatArray(fb.remaining())` を生成。GCが間に合わないとOOM。

**修正**: バッファを再利用する。
```kotlin
// ループ外で最大サイズのバッファを1つ確保
val maxSize = layerDefs.maxOf { it.shape.reduce(Int::times) * 4 }
val reusableBuf = ByteArray(maxSize)

for (layer in layerDefs) {
    val needed = layer.shape.reduce(Int::times) * 4
    stream.readFully(reusableBuf, 0, needed)
    val fb = ByteBuffer.wrap(reusableBuf, 0, needed)
        .order(ByteOrder.LITTLE_ENDIAN).asFloatBuffer()
    val floats = FloatArray(fb.remaining())
    fb.get(floats)
    // ...
}
```

---

### 11. LocalTrainer — バッチごとのFloatArray再確保

**ファイル**: `training/core/LocalTrainer.kt` (L81-93)
**問題**: `forward()` で毎回 `List(batchSize) { FloatArray(outputDim) }` を新規作成。25エポック×多数バッチで大量の一時配列。

**修正**: バッファをクラスフィールドとして保持し再利用する。
```kotlin
// LinearLayer内にバッファをキャッシュ
private var outputBuf: Array<FloatArray>? = null

fun forward(input: List<FloatArray>): List<FloatArray> {
    val buf = outputBuf?.takeIf { it.size == input.size && it[0].size == outputDim }
        ?: Array(input.size) { FloatArray(outputDim) }.also { outputBuf = it }
    // buf の中身をゼロクリアして再利用
    for (arr in buf) arr.fill(0f)
    // ... 計算 ...
    return buf.toList()
}
```

---

### 12. TrainingMetricsStore — ファイル全体をメモリに読み込み

**ファイル**: `training/TrainingMetricsStore.kt` (L97, L106)
**問題**: `readLines().toMutableList()` で全行をメモリに読み、`joinToString()` で再結合。大きなファイルでメモリスパイク。

**修正**: ストリーミング書き込みに変更するか、ファイルサイズが大きい場合は古いエントリを切り捨てる。
```kotlin
fun markUploaded(dataIds: Set<String>, runId: String?) {
    val f = File(baseDir, FILE_NAME)
    if (!f.exists()) return
    val tmpFile = File(baseDir, "${FILE_NAME}.tmp")
    f.bufferedReader().use { reader ->
        tmpFile.bufferedWriter().use { writer ->
            reader.lineSequence().forEach { line ->
                // dataIdを含む行にuploaded=trueを付与して書き出す
                // ...
                writer.appendLine(line)
            }
        }
    }
    tmpFile.renameTo(f)
}
```

---

## 設定変更チェックリスト

| 項目 | ファイル | 変更 |
|------|---------|------|
| largeHeap有効化 | AndroidManifest.xml | `android:largeHeap="true"` 追加 |
| JVM heap (ビルド時) | gradle.properties | 現状 2048m で十分 |

---

## 検証方法

1. **Android Studio Profiler** でヒープ推移を確認
   - 学習5ラウンド実行後、ヒープが初期比+20%以内に収まること
   - GC後にヒープが初期値付近に戻ること

2. **`adb shell dumpsys meminfo com.example.hfl_experiment`** で確認
   - Native Heap, Dalvik Heap の推移を3ラウンドごとに記録

3. **DeviceMetricsCollector の `memory_low_flag_count`** がゼロであること

---

## 修正の優先順

1. `AndroidManifest.xml` — largeHeap（1行追加、即効果）
2. `TrainingViewModel.kt` — onCleared() 追加（CRITICAL全体のルート原因）
3. `DeviceAckClient.kt` — stop() 強化 + 呼び出し保証
4. `MainActivity.kt` — onDestroy() でリソース解放
5. `NetworkClient.kt` — ストリームclose保証
6. `ModelLoader.kt` — release() メソッド追加
7. 残りのMODERATE項目
