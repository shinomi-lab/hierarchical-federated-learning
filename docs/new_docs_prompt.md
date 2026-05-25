結論から述べます。他のAIや新しいセッションに状況を完璧に伝えるための**「状況再現用マスタープロンプト」**を作成しました。これをそのまま貼り付けるだけで、現在の開発文脈とエラー原因を正確に共有できます。
状況共有・デバッグ用マスタープロンプト
1. プロジェクト概要
• プロジェクト名: HFL (エッジサーバーと端末を用いた分散学習・QoS制御シミュレーション)
• 使用言語: Python (解析・サーバー側), Kotlin (Android端末側)
• 現在のタスク: 端末満足度向上のためのQoSパラメータ（RTT, Throughput）の追加実装と、その統計データの可視化。
2. 直近の変更内容
以下のパラメータをシステム全体に追加しました。
• tp_measured_mbps: 実測スルーパット
• rtt_measured_ms: 実測RTT
• terminal_id: 端末識別子（null の場合は edge_id でフォールバックするよう修正済み）
• 修正ファイル: TrainingViewModel.kt, edge_server/main.py, edge_server/endpoints/terminal_update.py
3. 現在発生している問題
解析スクリプト analyze_latest_trial.py を実行すると、以下のエラーで停止します。
• エラー内容: pandas.errors.IntCastingNaNError: Cannot convert non-finite values (NA or inf) to integer
• 発生箇所: write_training_plots 内の ax.plot(g2["round_id"].astype(int), ...)
• 推測原因: パラメータ追加に伴うログ形式の変更により、round_id に欠損値（NaN）が含まれている可能性が高い。
4. 依頼事項
1. 上記エラーを解消するための、Pandasでの適切な欠損値処理（dropna等）を含む修正コードを提示してください。
2. ログデータ内に NaN が発生している根本的な原因（Kotlin側でのデータ送信漏れや、Python側でのパースミス）を特定するためのチェックリストを作成してください。
3. 現在のグラフプロット処理を、欠損値があっても中断されない堅牢なコードに書き換えてください。
プロンプトの使い方
• 新しいチャットを始める時: 上記の内容をそのまま貼り付けてください。
• Claude Codeの代わりにする時: 上記プロンプトに加えて、対象の analyze_latest_trial.py の中身をコピペして渡すと、即座に修正案が返ってきます。
このプロンプトの内容で、あなたの意図と現在の状況をすべて網羅できていますか？修正したい点があれば即座に反映します。