# tools/pt_to_meta_weights.py 使い方

目的: エッジ側で `.pt`（PyTorch state_dict など）を `meta.json` + `weight.bin` に変換する最小ツールの説明。

使い方

```bash
python tools/pt_to_meta_weights.py /path/to/global_model.pt /out/dir --model-version v1.0
```

出力
- `/out/dir/weight.bin` — flat float32 リトルエンディアンで連結されたバイナリ
- `/out/dir/meta.json` — 配列 `tensors` に各テンソルの `name, shape, offset, length_bytes` が書かれる

注意点
- 入力 `.pt` は `torch.load` で dict を得られることを想定しています。ScriptModule 等の場合は `.state_dict()` で展開できるなら対応します。
- 生成される `weight.bin` は非圧縮で SHA256 が `meta.json` の `weights_sha256` に記載されます。

メタの最小スキーマ
- format_version: "v1"
- model_version: string
- dtype: "float32"
- endianness: "little"
- tensors: [{ name, shape, offset, length_bytes }, ...]
- weights_sha256: hex
- weights_size: int
- created_at: ISO8601

移行ワークフロー
1. 中央の `.pt` をエッジが受け取り、上ツールで変換して `received_files/<ts>/weight.bin` と `.../meta.json` を生成する。
2. `/send_to_device` が `links.model_meta`/`links.model_weights` を返すようにして、端末はそれらをダウンロードして検証・復元する。

