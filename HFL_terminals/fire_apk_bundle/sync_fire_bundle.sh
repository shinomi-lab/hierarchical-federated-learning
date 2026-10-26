#!/usr/bin/env bash
# Fire 向け APK 準備用に、端末アプリの主要ファイルを fire_apk_bundle/copied/ へコピーする。
# 実行場所: HFL_terminals/ から
#   bash fire_apk_bundle/sync_fire_bundle.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$ROOT/fire_apk_bundle/copied"

echo "ROOT=$ROOT"
echo "OUT=$OUT"
rm -rf "$OUT"
mkdir -p "$OUT"

copy_one() {
  local rel="$1"
  local src="$ROOT/$rel"
  local dst="$OUT/$rel"
  if [[ ! -f "$src" ]]; then
    echo "skip (missing): $rel" >&2
    return 0
  fi
  mkdir -p "$(dirname "$dst")"
  cp -p "$src" "$dst"
  echo "ok: $rel"
}

# --- Gradle / プロジェクト ---
copy_one "settings.gradle.kts"
copy_one "build.gradle.kts"
copy_one "gradle.properties"
copy_one "gradle/libs.versions.toml"
copy_one "gradle/wrapper/gradle-wrapper.properties"

# --- app モジュール ---
copy_one "app/build.gradle.kts"
copy_one "app/proguard-rules.pro"
copy_one "app/src/main/AndroidManifest.xml"

# --- assets（端末・AP 設定）---
for f in "$ROOT/app/src/main/assets"/*; do
  [[ -f "$f" ]] || continue
  base="app/src/main/assets/$(basename "$f")"
  copy_one "$base"
done

echo ""
echo "Done. Review files under: $OUT"
