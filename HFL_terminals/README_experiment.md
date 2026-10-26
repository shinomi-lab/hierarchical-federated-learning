# 実験用 Decision Engine（日本語ドキュメント）

このドキュメントは、実験目的で作成した DecisionEngine モジュール一式の簡単な使い方、テスト方法、実機での注意点を日本語でまとめたものです。

概要

- 提供物: `DecisionEngine`, `ModelLoader` (TFLite 想定のスタブ実装), `NetworkManager` (スタブ/モック向け), `Config` (ap_config.json 読み書き), `Uploader`（ローカル CSV ログ）
- 目的: ネットワーク品質（スループット/RTT 等）とアプリ種類を基にアクセスポイント（AP）切替を判定する実験用のロジック検証
- 注意: 多くの箇所は実機依存であり、スタブやモックでの検証が前提です。実機での自動接続は OS/端末差が大きいので慎重に扱ってください。

クイックスタート

1. `ap_config.json` を `app/src/main/assets/` に置いてください（サンプルが同梱されています）。
2. アプリ内やテスト内で `Config` を読み込み `DecisionEngine` を生成します:

```kotlin
val cfg = Config.loadConfigFromAssets(context)
val engine = DecisionEngine(context, cfg)
val res = engine.observe(tp = 50f, rtt = 30f, appOneHot = floatArrayOf(1f,0f,0f,0f))
```

出力: `DecisionResult`（選択した AP、スコア、EMA、理由）が返ります。

ログと出力先

- 決定ログ: `logs/decision_log.csv`（アプリの filesDir 配下に保存されます）
  - カラム: timestamp, deviceIdHash, currentAP, chosenAP, rawScores, emaScores, tp_mbps, rtt_ms, appType, actionResult, errorMsg
- 学習メトリクス / イベント等は別モジュールで filesDir / external filesDir に書き出されます（実装参照）。

テストとビルド

- 必要環境:
  - JDK 11 以上
  - Gradle Wrapper（プロジェクトに同梱: `gradlew` / `gradlew.bat`）
  - （Android モジュールで実機テストを行う場合は）Android SDK、接続済みデバイスまたはエミュレータ

- 重要なテストコマンド（PowerShell）:

```powershell
# 単体テストを実行（JUnit / Mockito 等が build.gradle に追加されていること）
.\gradlew.bat :app:testDebugUnitTest

# アプリをビルド
.\gradlew.bat assembleDebug

# 実機向けのインストルメント化テスト（接続されたデバイスが必要）
.\gradlew.bat :app:connectedAndroidTest
```

- テスト依存: `junit`, `org.mockito:mockito-core`, `org.mockito.kotlin:mockito-kotlin`, `kotlinx-coroutines-test` 等が必要です。`app/build.gradle.kts` に追加してください。

実機での Wi‑Fi 切替に関する注意と推奨

- Android の自動接続は OS バージョン（特に Android 10+）やメーカーの実装に大きく依存します。次の方法を推奨します。

1. 実装方法（推奨）:
   - Android 10 以上: `WifiNetworkSpecifier` / `NetworkRequest` と `ConnectivityManager` を使用します（ユーザ操作や許可の制約に注意）。
   - それ未満: `WifiManager` を使う。ただし Android 10 以降では動作しないケースあり。

2. 自動化の現実的アプローチ:
   - 実験ラボでは ADB スクリプトでネットワーク切替を行う方が安定します。Accessibility による自動化は端末依存性が高いので、実験環境限定でのみ検討してください。

3. AP 情報の保存場所（推奨）:
   - 実行時に変更可能 & セキュア: `EncryptedSharedPreferences`（AndroidX Security）を利用して SSID / PSK / priority を保存する。PSK は暗号化して端末内に保管すること。
   - テスト用のデフォルト: `app/src/main/assets/ap_config.json`（パスワードは公開リポジトリに置かない）
   - CI/ビルド時デフォルト: `buildConfigField` 経由（ただし PSK はシークレットで管理）

4. セキュリティ:
   - PSK や機密情報はソース管理に入れない。Android Keystore / EncryptedSharedPreferences / 外部シークレットマネージャで管理する。

運用上のワークフロー（例）

- 研究者が実験端末に接続し、Settings 画面（または専用 UI）で SSID/PSK を入力して保存（EncryptedSharedPreferences）→ `DecisionEngine` は実行時にその設定を参照して判断を行う。
- テスト自動化はまずモックでロジックを検証し、必要に応じて ADB スクリプトで実機上の切替を行う。

実装上の既知の制約（短く）

- `ModelLoader` は現時点では TFLite を直接呼ぶ実装ではなくスタブです。実際のモデル推論を行うには TFLiteInterpreter の導入が必要です。
- `NetworkManager` は実機実装ではなくスタブ／モック前提です。実機切替は動作保証がありません。
- `DecisionEngine` の時間依存（debounce）はテストしやすくするために FakeClock を使って DI することを推奨します。

次のステップ（あなたにおすすめする順）

1. `app/build.gradle.kts` にテスト依存（Mockito 等）を追加して単体テストを実行してください。私が自動で追加・テスト実行することも可能です。  
2. FakeClock を導入して `DecisionEngine` の minSwitchSeconds を高速にテスト可能にする。  
3. 実機でのテストは必ず `FakeNetworkManager` を使ってロジック検証後、ADB スクリプトで接続先切替を行ってください。

問い合わせ・変更依頼

- この README（日本語版）をさらに詳細化してほしい箇所（コード例追加、Settings UI の実装手順、Gradle 依存の自動追加など）があれば教えてください。必要なら私が自動で変更を加えます。
