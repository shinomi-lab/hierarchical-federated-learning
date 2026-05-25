#!/usr/bin/env text
# curl / PowerShell examples for uploads

下記は PowerShell で使える `curl`（Windows の `curl`/`Invoke-WebRequest` 互換）例です。`{{NG}}` はエッジの公開ホスト（ngrok の public_url）に置き換えてください。

例: torch_state_dict を送る
```powershell
$NG = "tuan-peaceful-nontechnologically.ngrok-free.dev"
$TID = "terminal-123"
$TOKEN = "<EDGE_UPLOAD_TOKEN>"

# curl (PowerShell の curl を使う / 外部 curl を使う場合はパスを調整してください)
curl -v -X POST "https://$NG/receive_terminal_weights/$TID" `
  -H "Authorization: Bearer $TOKEN" `
  -F "round=1" `
  -F "model_id=demo-mlp-v1" `
  -F "base_hash=sha256:bootstrap" `
  -F "n_samples=100" `
  -F "dtype=torch_state_dict" `
  -F "weights=@./local_weights.pt;type=application/octet-stream" `
  -F "contentSha256=sha256:<hex>"
```

例: f32_flat を送る
```powershell
curl -v -X POST "https://$NG/receive_terminal_weights/$TID" `
  -H "Authorization: Bearer $TOKEN" `
  -F "round=1" `
  -F "model_id=demo-mlp-v1" `
  -F "base_hash=sha256:bootstrap" `
  -F "n_samples=50" `
  -F "dtype=f32_flat" `
  -F "payload_kind=full" `
  -F "input_size=128" -F "hidden_size=64" -F "output_size=10" `
  -F "weights=@./flat_weights.bin;type=application/octet-stream" `
  -F "contentSha256=sha256:<hex>"
```

大容量ファイル用の補足:
- 途中で切断されやすい環境では、ファイルを分割してサーバ側で結合する API を用意するか、`tus` のような再開対応のプロトコルを検討してください。
