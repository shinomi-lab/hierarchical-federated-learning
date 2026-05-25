"""
central_server/ai_advisor.py
============================
HFL 実験中に AI（Claude / Gemini）がリアルタイムで分析・警告・レポートを行うモジュール。

設定方法:
  環境変数 AI_ADVISOR_PROVIDER に "claude" または "gemini" を設定し、
  対応する API キーをセットするだけで有効になります。

  # Claude を使う場合
  export AI_ADVISOR_PROVIDER=claude
  export ANTHROPIC_API_KEY=sk-ant-...

  # Gemini を使う場合
  export AI_ADVISOR_PROVIDER=gemini
  export GEMINI_API_KEY=AIza...

  # 無効化（何も設定しなければデフォルトで無効）
  unset AI_ADVISOR_PROVIDER
"""

import asyncio
import logging
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger("ai_advisor")

# ------------------------------------------------------------------ #
# カラー出力ヘルパー
# ------------------------------------------------------------------ #
_USE_COLOR = os.name != "nt" or os.environ.get("WT_SESSION")

def _tag(label: str, color: str, text: str) -> str:
    if _USE_COLOR:
        return f"\033[{color}m{label}\033[0m {text}"
    return f"{label} {text}"

def _info(text: str)  -> str: return _tag("[AI ✦]", "35", text)   # マゼンタ
def _warn(text: str)  -> str: return _tag("[AI ⚠]", "33", text)   # 黄
def _report(text: str)-> str: return _tag("[AI 📊]", "36", text)  # シアン


# ------------------------------------------------------------------ #
# HFLAdvisor
# ------------------------------------------------------------------ #
class HFLAdvisor:
    """
    中央サーバーのイベントにフックして AI 分析を提供するクラス。
    API キーが未設定の場合は完全に無効化され、サーバー動作に影響しない。
    """

    def __init__(self, db_path: Path, log_dir: Path):
        self.db_path  = db_path
        self.log_dir  = log_dir
        self.log_dir.mkdir(parents=True, exist_ok=True)

        self._client   = None
        self._provider: Optional[str] = None
        self._enabled  = False
        self._last_round_reported = -1

        self._setup()

    # ---- 初期化 ----

    def _setup(self):
        provider = os.getenv("AI_ADVISOR_PROVIDER", "").lower()
        if not provider:
            return  # 環境変数未設定 → 無効

        if provider == "claude":
            self._setup_claude()
        elif provider == "gemini":
            self._setup_gemini()
        else:
            logger.warning(f"AI_ADVISOR_PROVIDER の値が不正です: {provider!r}（claude または gemini を指定）")

    def _setup_claude(self):
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            logger.info("ANTHROPIC_API_KEY が未設定のため AI アドバイザーを無効化します")
            return
        try:
            import anthropic
            self._client   = anthropic.Anthropic(api_key=api_key)
            self._provider = "claude"
            self._enabled  = True
            print(_info("Claude AI アドバイザーが有効です（claude-haiku-4-5）"), flush=True)
        except ImportError:
            logger.warning("anthropic パッケージが未インストールです: pip install anthropic")

    def _setup_gemini(self):
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            logger.info("GEMINI_API_KEY が未設定のため AI アドバイザーを無効化します")
            return
        try:
            import google.generativeai as genai
            genai.configure(api_key=api_key)
            self._client   = genai.GenerativeModel("gemini-1.5-flash")
            self._provider = "gemini"
            self._enabled  = True
            print(_info("Gemini AI アドバイザーが有効です（gemini-1.5-flash）"), flush=True)
        except ImportError:
            logger.warning("google-generativeai パッケージが未インストールです: pip install google-generativeai")

    # ---- LLM 呼び出し ----

    async def _call_llm(self, prompt: str, max_tokens: int = 300) -> str:
        """LLM を非同期（別スレッド）で呼び出す。失敗時は空文字を返す。"""
        if not self._enabled:
            return ""

        def _sync():
            try:
                if self._provider == "claude":
                    msg = self._client.messages.create(
                        model="claude-haiku-4-5-20251001",
                        max_tokens=max_tokens,
                        messages=[{"role": "user", "content": prompt}],
                    )
                    return msg.content[0].text.strip()
                elif self._provider == "gemini":
                    resp = self._client.generate_content(prompt)
                    return resp.text.strip()
            except Exception as e:
                logger.warning(f"AI API 呼び出し失敗: {e}")
            return ""

        return await asyncio.to_thread(_sync)

    # ---- DB アクセス ----

    def _query(self, sql: str, params: tuple = ()) -> list[dict]:
        if not self.db_path.exists():
            return []
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.row_factory = sqlite3.Row
            return [dict(r) for r in conn.execute(sql, params).fetchall()]

    def _round_metrics(self, round_id: int) -> list[dict]:
        return self._query("SELECT * FROM metrics WHERE round=? ORDER BY id", (round_id,))

    def _all_metrics(self) -> list[dict]:
        return self._query("SELECT * FROM metrics ORDER BY round, id")

    # ---- ログ保存 ----

    def _save_log(self, content: str, label: str):
        ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = self.log_dir / f"{ts}_{label}.md"
        try:
            path.write_text(content, encoding="utf-8")
        except Exception as e:
            logger.warning(f"AI ログ保存失敗: {e}")

    # ================================================================
    # 公開メソッド（サーバーから呼ばれる）
    # ================================================================

    async def on_metrics_received(self, metrics: dict):
        """
        [A] リアルタイム実況 ＋ [B] 異常検知
        メトリクス保存後に非同期で呼ばれる。
        satisfaction の急落など明確な異常がある場合のみ出力する。
        """
        if not self._enabled:
            return

        before = metrics.get("satisfaction_before")
        after  = metrics.get("satisfaction_after")
        acc    = metrics.get("accuracy")
        loss   = metrics.get("loss")

        # 異常がなければ何もしない（ノイズを減らす）
        anomalies = []
        if before is not None and after is not None and (after - before) < -0.2:
            anomalies.append(f"satisfaction 急落 ({before:.3f}→{after:.3f}, Δ={after-before:+.3f})")
        if acc is not None and acc < 0.3:
            anomalies.append(f"accuracy 低値 ({acc:.4f})")

        if not anomalies:
            return

        prompt = (
            "以下のHFL実験データで異常が検出されました。研究者向けに1文（日本語）で"
            "問題と推奨アクションを簡潔に報告してください。前置き不要。\n\n"
            f"ラウンド: {metrics.get('round')}\n"
            f"エッジ: {metrics.get('edge_id')}\n"
            f"app_type: {metrics.get('app_type')}\n"
            f"異常項目: {', '.join(anomalies)}\n"
            f"accuracy: {acc}, loss: {loss}"
        )
        comment = await self._call_llm(prompt, max_tokens=120)
        if comment:
            print(f"\n{_warn(comment)}\n", flush=True)
            self._save_log(
                f"# 異常検知 (ラウンド{metrics.get('round')})\n\n{comment}\n\n"
                f"## 検出項目\n" + "\n".join(f"- {a}" for a in anomalies),
                f"anomaly_r{metrics.get('round')}",
            )

    async def on_round_complete(self, round_id: int):
        """
        [C] ラウンド完了レポート
        各ラウンドの集約完了後に呼ばれる。
        """
        if not self._enabled:
            return
        if round_id <= self._last_round_reported:
            return
        self._last_round_reported = round_id

        rows = self._round_metrics(round_id)
        if not rows:
            return

        # 統計計算
        n          = len(rows)
        app_types  = [r["app_type"] for r in rows if r.get("app_type")]
        accuracies = [r["accuracy"] for r in rows if r.get("accuracy") is not None]
        sat_deltas = [
            r["satisfaction_after"] - r["satisfaction_before"]
            for r in rows
            if r.get("satisfaction_before") is not None and r.get("satisfaction_after") is not None
        ]

        def _fmt(vals): return f"{sum(vals)/len(vals):.4f}" if vals else "N/A"

        stats_text = (
            f"ラウンド{round_id} / 受信端末数: {n}\n"
            f"app_type 分布: {', '.join(sorted(set(app_types))) if app_types else 'なし'}\n"
            f"accuracy 平均: {_fmt(accuracies)}\n"
            f"満足度改善 平均: {_fmt(sat_deltas)} "
            f"(最大: {max(sat_deltas):.4f if sat_deltas else 'N/A'}, "
            f"最小: {min(sat_deltas):.4f if sat_deltas else 'N/A'})"
        )

        prompt = (
            "以下のHFL実験のラウンド結果を2〜3文（日本語）で分析してください。"
            "注目点や次ラウンドへの示唆も含めてください。前置き不要。\n\n"
            + stats_text
        )
        report = await self._call_llm(prompt, max_tokens=250)
        if report:
            print(f"\n{_report(f'ラウンド {round_id} レポート')}", flush=True)
            for line in report.splitlines():
                print(f"  {line}", flush=True)
            print(flush=True)
            self._save_log(
                f"# ラウンド {round_id} レポート\n\n{report}\n\n## 統計\n```\n{stats_text}\n```",
                f"round_{round_id:03d}",
            )

    async def generate_experiment_summary(self) -> str:
        """
        [D] 実験全体サマリ
        `python3 start.py report` から呼ばれる。
        """
        rows = self._all_metrics()
        if not rows:
            return "メトリクスデータがありません。実験を実行してからお試しください。"

        rounds    = sorted(set(r["round"] for r in rows))
        app_types = sorted(set(r["app_type"] for r in rows if r.get("app_type")))

        # app_type 別集計
        app_lines = []
        for at in app_types:
            sub    = [r for r in rows if r.get("app_type") == at]
            deltas = [
                r["satisfaction_after"] - r["satisfaction_before"]
                for r in sub
                if r.get("satisfaction_before") is not None and r.get("satisfaction_after") is not None
            ]
            avg_d = f"{sum(deltas)/len(deltas):.4f}" if deltas else "N/A"
            app_lines.append(f"  {at}: n={len(sub)}, 平均満足度改善={avg_d}")

        # ラウンド別 accuracy 推移
        round_lines = []
        for rnd in rounds:
            sub = [r for r in rows if r["round"] == rnd and r.get("accuracy") is not None]
            if sub:
                avg = sum(r["accuracy"] for r in sub) / len(sub)
                round_lines.append(f"R{rnd}: {avg:.4f}")

        context = (
            f"【実験概要】全{len(rows)}件 / {len(rounds)}ラウンド\n\n"
            f"【app_type 別 満足度改善】\n" + "\n".join(app_lines) + "\n\n"
            f"【ラウンド別 accuracy 推移】\n  " + ", ".join(round_lines)
        )

        prompt = (
            "以下のHFL（階層型連合学習）実験結果を研究者向けに分析してください（日本語、6〜8文）。\n"
            "含めてほしい観点：app_type別の傾向、学習の収束状況、外れ値・課題、今後の実験への示唆。\n"
            "前置き不要。\n\n" + context
        )

        print(_report("実験サマリを生成中..."), flush=True)
        summary = await self._call_llm(prompt, max_tokens=700)
        if not summary:
            return "AI サマリの生成に失敗しました。API キーを確認してください。"

        self._save_log(
            f"# 実験サマリ\n\n{summary}\n\n## データ概要\n```\n{context}\n```",
            "experiment_summary",
        )
        return summary


# ------------------------------------------------------------------ #
# グローバルシングルトン
# ------------------------------------------------------------------ #
_advisor: Optional[HFLAdvisor] = None


def get_advisor() -> Optional[HFLAdvisor]:
    """中央サーバー起動後に使えるグローバルアドバイザーを返す。未初期化なら None。"""
    return _advisor


def init_advisor(db_path: Path, log_dir: Path) -> HFLAdvisor:
    """中央サーバーの startup イベントから一度だけ呼ぶ。"""
    global _advisor
    _advisor = HFLAdvisor(db_path=db_path, log_dir=log_dir)
    return _advisor
