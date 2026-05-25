# 以前は device_config.deviceA.json などを device_config.json にコピーしていた。
# 現在は assets に device_config.json を 1 本置き、端末ごとの terminal_id / experiment_group は
# アプリ初回の「端末 ID」画面または設定から入力する（同一 APK を複数端末に配布可能）。

Write-Host "select-device-config.ps1 は廃止されました。同一 APK を入れ、初回または設定で端末 ID を入力してください。"

$dst = "app/src/main/assets/device_config.json"
if (-not (Test-Path $dst)) {
  Write-Error "見つかりません: $dst"
  exit 1
}
Write-Host "OK: $dst が存在します。"
exit 0
