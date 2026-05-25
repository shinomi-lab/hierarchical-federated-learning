# Fire 向け APK 用ファイル束（参照コピー）

Amazon Fire タブレット用 APK を切るときに、**ビルド設定・マニフェスト・端末別アセット**を一か所に集めて差分レビューしやすくするためのフォルダです。

## 正のソース

**常に `HFL_terminals/` 直下の `app/`・`gradle/` が正**です。この下の `copied/` はコピーなので、編集は元ファイルで行い、必要なら再度同期してください。

## 同期のしかた

`HFL_terminals/` で:

```bash
bash fire_apk_bundle/sync_fire_bundle.sh
```

`fire_apk_bundle/copied/` にミラー構造でファイルが入ります。

## Fire 向けに見るポイント（チェックリスト）

- `app/build.gradle.kts`: GMS / Firebase の **runtime `implementation`** がないか、`pytorch_android` の **ABI（armeabi-v7a / arm64-v8a）**。
- `gradle/libs.versions.toml`: 依存バージョン。
- `app/src/main/AndroidManifest.xml`: 権限・exported。
- `assets/device_config.json`: 共通テンプレート（`terminal_id` / `experiment_group` は空で可）。端末ごとの ID はアプリ初回または設定で入力。
- `assets/ap_config.json`: AP 実験設定。

## ビルドそのもの

APK のビルドは従来どおり **`HFL_terminals` をルートにした Gradle** で行います（このフォルダだけではプロジェクトは完結しません）。

```bash
cd /path/to/HFL_terminals
./gradlew :app:assembleDebug
```
