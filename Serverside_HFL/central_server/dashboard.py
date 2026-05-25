"""Streamlit dashboard for quick visualization and metadata checks.

Run with:
    streamlit run central_server/dashboard.py

This reads the central training_data SQLite DB and scans
received_files/terminal_updates for manifest JSON files and
displays simple summaries and distributions.
"""
import os
import glob
import json
import sqlite3
from datetime import datetime

import pandas as pd
import streamlit as st
import requests


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DB_PATH = os.path.join(ROOT, "central_server", "training_data.db")
METRICS_DB_PATH = os.path.join(ROOT, "central_server", "training_metrics.db")
MANIFEST_GLOB = os.path.join(ROOT, "received_files", "terminal_updates", "**", "manifest_*.json")


@st.cache_data
def load_training_data(db_path: str):
    if not os.path.exists(db_path):
        return pd.DataFrame()
    conn = sqlite3.connect(db_path)
    try:
        # training_data table does not include a created_at column; order by id (newest first)
        df = pd.read_sql_query("SELECT * FROM training_data ORDER BY id DESC", conn)
    except Exception:
        df = pd.DataFrame()
    finally:
        conn.close()
    return df


@st.cache_data
def load_metrics_data(db_path: str):
    if not os.path.exists(db_path):
        return pd.DataFrame()
    conn = sqlite3.connect(db_path)
    try:
        df = pd.read_sql_query("SELECT * FROM metrics ORDER BY id DESC", conn)
    except Exception:
        df = pd.DataFrame()
    finally:
        conn.close()
    return df


@st.cache_data
def scan_manifests(glob_pattern: str):
    files = glob.glob(glob_pattern, recursive=True)
    rows = []
    for p in sorted(files, reverse=True):
        try:
            with open(p, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception:
            continue
        # try to extract some common fields
        sha = data.get("sha256") or data.get("sha")
        base_hash = data.get("base_hash")
        evt_ts = data.get("event_timestamp") or data.get("started_at") or data.get("timestamp")
        run_id = data.get("run_id") or data.get("run")
        rows.append({
            "path": p,
            "sha256": sha,
            "base_hash": base_hash,
            "event_timestamp": evt_ts,
            "run_id": run_id,
            "raw": data,
        })
    df = pd.DataFrame(rows)
    # normalize timestamps
    if not df.empty:
        try:
            df["event_dt"] = pd.to_datetime(df["event_timestamp"]) 
        except Exception:
            df["event_dt"] = pd.NaT
    return df


def main():
    st.set_page_config(page_title="HFL Central Dashboard", layout="wide")
    st.title("HFL Central — Quick Dashboard")

    col1, col2 = st.columns([2, 1])

    with col1:
        st.header("Training data (central DB)")
        df_train = load_training_data(DB_PATH)
        if df_train.empty:
            st.info(f"No training data DB found at {DB_PATH}")
        else:
            st.write(f"Rows: {len(df_train)}")
            st.dataframe(df_train.head(200))
            # show basic time distribution if created_at exists
            if "created_at" in df_train.columns:
                ts = pd.to_datetime(df_train["created_at"], errors="coerce")
                counts = ts.dt.date.value_counts().sort_index()
                st.subheader("Training data by day")
                st.bar_chart(counts)

    with col2:
        st.header("Manifests (received_files)")
        df_man = scan_manifests(MANIFEST_GLOB)
        st.write(f"Manifests found: {len(df_man)}")
        if not df_man.empty:
            st.dataframe(df_man[["path", "sha256", "base_hash", "event_timestamp", "run_id"]].head(200))
            # show sha frequency
            st.subheader("SHA256 frequency (top 20)")
            top = df_man["sha256"].value_counts().head(20)
            st.bar_chart(top)

    st.header("Quick cross-checks")
    if not df_man.empty:
        # show manifests without sha
        missing_sha = df_man[df_man["sha256"].isna()]
        if not missing_sha.empty:
            st.warning(f"{len(missing_sha)} manifests missing sha256; showing sample")
            st.dataframe(missing_sha.head(20))

        # sample mismatch check: show manifests with base_hash different from sha prefix
        def prefix_ok(row):
            sha = row.get("sha256")
            base = row.get("base_hash")
            if not sha or not base:
                return True
            return sha.startswith(base.split(":")[-1]) or base.endswith(sha.split(":")[-1])

        mismatches = [not prefix_ok(r) for r in df_man.to_dict("records")]
        if any(mismatches):
            st.error(f"Found {sum(mismatches)} possible base_hash/sha mismatches; showing samples")
            df_bad = df_man[[not x for x in mismatches]][["path", "sha256", "base_hash"]].head(50)
            st.dataframe(df_bad)

    st.markdown("---")
    st.header("実験メトリクス（training_metrics.db）")
    df_metrics = load_metrics_data(METRICS_DB_PATH)
    if df_metrics.empty:
        st.info(f"メトリクスDBが見つかりません: {METRICS_DB_PATH}")
    else:
        st.write(f"レコード数: {len(df_metrics)}")

        # app_type 別の集計
        if "app_type" in df_metrics.columns and df_metrics["app_type"].notna().any():
            st.subheader("アプリ種別ごとの件数")
            st.bar_chart(df_metrics["app_type"].value_counts())

            # 満足度改善の集計
            if "satisfaction_before" in df_metrics.columns and "satisfaction_after" in df_metrics.columns:
                df_sat = df_metrics.dropna(subset=["satisfaction_before", "satisfaction_after", "app_type"])
                if not df_sat.empty:
                    df_sat = df_sat.copy()
                    df_sat["improvement"] = df_sat["satisfaction_after"] - df_sat["satisfaction_before"]
                    summary = df_sat.groupby("app_type")[["satisfaction_before", "satisfaction_after", "improvement"]].mean().round(3)
                    st.subheader("アプリ種別ごとの満足度（平均）")
                    st.dataframe(summary)
                    st.subheader("満足度改善量（app_type別平均）")
                    st.bar_chart(summary["improvement"])
                else:
                    st.info("満足度データがまだありません（satisfaction_before / satisfaction_after が NULL）")
        else:
            st.info("app_type データがまだありません。Android側の送信実装後に表示されます。")

        # ラウンド別 loss / accuracy
        if "round" in df_metrics.columns:
            st.subheader("ラウンド別 loss / accuracy")
            numeric_cols = [c for c in ["loss", "accuracy"] if c in df_metrics.columns]
            if numeric_cols:
                df_round = df_metrics.groupby("round")[numeric_cols].mean().round(4)
                st.line_chart(df_round)

        st.subheader("生データ（直近100件）")
        st.dataframe(df_metrics.head(100))

    st.markdown("---")
    st.caption("This dashboard is intentionally lightweight: extend it to add plots or join datasets for your use case.")

    # Edge metrics section
    st.header("Edge Server Metrics")
    edge_base = os.getenv("EDGE_BASE_URL", "http://localhost:8001")
    try:
        r = requests.get(f"{edge_base}/api/dashboard/metrics", timeout=3)
        if r.ok:
            m = r.json()
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Round", m.get("round"))
            c2.metric("Finished / Threshold", f"{m.get('finished_updates')}/{m.get('aggregation_threshold')}")
            c3.metric("WS Clients", m.get("ws_clients"))
            sz = int(m.get("storage_bytes") or 0)
            c4.metric("Logs Size (MB)", f"{sz/1024/1024:.1f}")
            st.json(m.get("flags", {}))
            # Alerts
            st.subheader("Alerts")
            ack = (m.get("ack_latency_s") or {})
            p90 = ack.get("p90")
            alerts = []
            if p90 and p90 > 60:
                alerts.append(f"ACK latency P90 high: {p90:.1f}s")
            if sz/1024/1024/1024 > 10:  # >10 GB
                alerts.append("Logs storage > 10GB")
            if alerts:
                for a in alerts:
                    st.error(a)
            else:
                st.success("No alerts")
            # Operator actions
            st.subheader("Operator Actions")
            colA, colB = st.columns(2)
            if colA.button("Trigger Aggregation (current round)"):
                try:
                    rr = requests.post(f"{edge_base}/aggregation/trigger_current", timeout=5)
                    if rr.ok:
                        st.success(f"Aggregation started: {rr.json()}")
                    else:
                        st.error(f"Failed: {rr.status_code} {rr.text}")
                except Exception as e:
                    st.error(f"Request failed: {e}")
            if colB.button("Broadcast Model Update (manual)"):
                try:
                    rr = requests.post(f"{edge_base}/push_model_to_devices", timeout=5)
                    if rr.ok:
                        st.success(f"Notified: {rr.json()}")
                    else:
                        st.error(f"Failed: {rr.status_code} {rr.text}")
                except Exception as e:
                    st.error(f"Request failed: {e}")
        else:
            st.warning(f"Failed to fetch metrics from {edge_base}: {r.status_code}")
    except Exception as e:
        st.warning(f"Metrics unavailable: {e}")


if __name__ == "__main__":
    main()
