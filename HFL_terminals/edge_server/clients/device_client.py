"""
Device-side client simulator for sending training metrics and sample files to Edge server.

Usage (example):
    python device_client.py --base-url http://192.168.11.6:8001 --device-id device-001 --token secret-token

Features:
- Simulates collecting training metrics and binary "samples" each round.
- Stores metrics and samples under state/training_data/ to survive restarts.
- After configured rounds_per_upload (default 5), uploads metrics (single aggregated object) to
  POST /api/training-data and uploads each sample to POST /api/training-data/upload-sample (multipart).
- Retries HTTP requests with exponential backoff on transient failures.
- Configurable via CLI flags.

This is a lightweight, self-contained script suitable for Android/device-side porting later (Kotlin/Retrofit).
"""

import argparse
import json
import logging
import os
import random
import time
from pathlib import Path
from typing import Dict, Any, List

import requests

# Defaults
DEFAULT_BASE = "http://192.168.11.6:8001"
DEFAULT_TOKEN = "secret-token"
STATE_DIR = Path(__file__).resolve().parent.parent / "state" / "training_data"

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger("device_client")


def ensure_dirs():
    STATE_DIR.mkdir(parents=True, exist_ok=True)


def save_jsonl(path: Path, obj: Dict[str, Any]):
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def persist_sample(device_id: str, round_number: int, sample_bytes: bytes, sample_idx: int) -> Path:
    ensure_dirs()
    fn = STATE_DIR / f"sample_{device_id}_r{round_number}_{sample_idx}.bin"
    with fn.open("wb") as f:
        f.write(sample_bytes)
    return fn


def persist_metric(device_id: str, round_number: int, metrics: Dict[str, Any]) -> Path:
    ensure_dirs()
    fn = STATE_DIR / f"metrics_{device_id}_r{round_number}.jsonl"
    save_jsonl(fn, metrics)
    return fn


def load_pending_samples() -> List[Path]:
    ensure_dirs()
    return sorted(list(STATE_DIR.glob("sample_*.bin")))


def load_pending_metrics() -> List[Path]:
    ensure_dirs()
    return sorted(list(STATE_DIR.glob("metrics_*.jsonl")))


def http_post(url: str, headers: Dict[str, str], json_body: Any = None, files=None, data=None, max_retries=5) -> requests.Response:
    backoff = 1.0
    for attempt in range(1, max_retries + 1):
        try:
            if files is not None:
                # send multipart/form-data (files + form fields)
                r = requests.post(url, headers=headers, files=files, data=data, timeout=30)
            else:
                r = requests.post(url, headers=headers, json=json_body, timeout=30)
            if r.status_code >= 500:
                logger.warning("server error %s on %s (attempt %d)", r.status_code, url, attempt)
                raise requests.RequestException("server error")
            return r
        except (requests.RequestException, requests.Timeout) as e:
            logger.warning("request failed to %s: %s (attempt %d)", url, e, attempt)
            if attempt == max_retries:
                raise
            time.sleep(backoff)
            backoff *= 2


def send_metrics(base_url: str, token: str, aggregated: Dict[str, Any]) -> bool:
    url = base_url.rstrip("/") + "/api/training-data"
    headers = {"Authorization": f"Bearer {token}"}
    try:
        r = http_post(url, headers, json_body=aggregated)
        logger.info("metrics upload status=%s body=%s", r.status_code, r.text)
        return r.status_code == 200
    except Exception as e:
        logger.error("failed to upload metrics: %s", e)
        return False


def upload_sample_file(base_url: str, token: str, file_path: Path, device_id: str, round_number: int) -> bool:
    url = base_url.rstrip("/") + "/api/training-data/upload-sample"
    headers = {"Authorization": f"Bearer {token}"}
    # files: only the binary file; form fields go into data
    data = {"device_id": device_id, "round_number": str(round_number)}
    try:
        with file_path.open("rb") as fh:
            files = {"file": (file_path.name, fh, "application/octet-stream")}
            r = http_post(url, headers, files=files, data=data)
        logger.info("sample upload %s -> status=%s body=%s", file_path.name, r.status_code, r.text)
        return r.status_code == 200
    except Exception as e:
        logger.error("failed to upload sample %s: %s", file_path, e)
        return False


def aggregate_metrics_for_upload(device_id: str, round_numbers: List[int], metrics_files: List[Path]) -> Dict[str, Any]:
    # For simplicity aggregate averages and sums where appropriate.
    agg = {
        "roundNumber": max(round_numbers) if round_numbers else 0,
        "terminalId": device_id,
        "epoch": 0,
        "batchSize": 0,
        "learningRate": 0.0,
        "trainingLoss": 0.0,
        "validationLoss": 0.0,
        "trainingAccuracy": 0.0,
        "validationAccuracy": 0.0,
        "uploadSize": 0,
        "downloadSize": 0,
        "communicationTime": 0,
        "cpuUsage": 0.0,
        "memoryUsage": 0.0,
        "batteryUsage": 0.0,
        "datasetSize": 0,
        "timestamp": int(time.time() * 1000),
        "communicationErrors": 0,
        "trainingErrors": 0,
        "dataDistribution": {},
        "preprocessingTime": 0,
    }
    count = 0
    for mf in metrics_files:
        try:
            with mf.open("r", encoding="utf-8") as f:
                for line in f:
                    obj = json.loads(line)
                    count += 1
                    # accumulate
                    agg["trainingLoss"] += obj.get("trainingLoss", 0.0)
                    agg["validationLoss"] += obj.get("validationLoss", 0.0)
                    agg["trainingAccuracy"] += obj.get("trainingAccuracy", 0.0)
                    agg["validationAccuracy"] += obj.get("validationAccuracy", 0.0)
                    agg["uploadSize"] += obj.get("uploadSize", 0)
                    agg["downloadSize"] += obj.get("downloadSize", 0)
                    agg["communicationTime"] += obj.get("communicationTime", 0)
                    agg["cpuUsage"] += obj.get("cpuUsage", 0.0)
                    agg["memoryUsage"] += obj.get("memoryUsage", 0.0)
                    agg["batteryUsage"] += obj.get("batteryUsage", 0.0)
                    agg["datasetSize"] += obj.get("datasetSize", 0)
                    agg["communicationErrors"] += obj.get("communicationErrors", 0)
                    agg["trainingErrors"] += obj.get("trainingErrors", 0)
                    agg["preprocessingTime"] += obj.get("preprocessingTime", 0)
                    dd = obj.get("dataDistribution") or {}
                    for k, v in dd.items():
                        agg["dataDistribution"][k] = agg["dataDistribution"].get(k, 0) + v
        except Exception as e:
            logger.warning("failed to read metrics file %s: %s", mf, e)
    if count > 0:
        # average where sensible
        agg["trainingLoss"] /= count
        agg["validationLoss"] /= count
        agg["trainingAccuracy"] /= count
        agg["validationAccuracy"] /= count
        agg["cpuUsage"] /= count
        agg["memoryUsage"] /= count
        agg["batteryUsage"] /= count
    # compute uploadSize by summing sample file sizes for the rounds being uploaded
    try:
        for rn in round_numbers:
            for p in STATE_DIR.glob(f"sample_*_r{rn}_*.bin"):
                try:
                    agg["uploadSize"] += p.stat().st_size
                except Exception:
                    logger.debug("could not stat %s", p)
    except Exception:
        pass
    return agg


def cleanup_files(files: List[Path]):
    for p in files:
        try:
            p.unlink()
        except Exception:
            logger.debug("could not delete %s", p)


def simulate_round_and_collect(device_id: str, round_number: int, samples_per_round: int = 2) -> List[Path]:
    # Produce fake metrics and sample files, persist them and return sample paths
    sample_paths: List[Path] = []
    # simple fake metrics
    metrics = {
        "roundNumber": round_number,
        "terminalId": device_id,
        "epoch": random.randint(1, 5),
        "batchSize": 32,
        "learningRate": 0.001,
        "trainingLoss": round(random.uniform(0.1, 1.0), 4),
        "validationLoss": round(random.uniform(0.1, 1.0), 4),
        "trainingAccuracy": round(random.uniform(0.5, 0.95), 4),
        "validationAccuracy": round(random.uniform(0.4, 0.9), 4),
        "uploadSize": 0,
        "downloadSize": 0,
        "communicationTime": 0,
        "cpuUsage": round(random.uniform(5.0, 60.0), 2),
        "memoryUsage": round(random.uniform(50.0, 512.0), 1),
        "batteryUsage": round(random.uniform(0.1, 5.0), 2),
        "datasetSize": random.randint(100, 10000),
        "timestamp": int(time.time() * 1000),
        "communicationErrors": 0,
        "trainingErrors": 0,
        "dataDistribution": {"A": random.randint(0, 500), "B": random.randint(0, 500)},
        "preprocessingTime": random.randint(10, 5000),
    }
    persist_metric(device_id, round_number, metrics)
    # create sample files
    for i in range(samples_per_round):
        b = os.urandom(1024)  # 1KiB fake sample
        p = persist_sample(device_id, round_number, b, i)
        sample_paths.append(p)
    logger.info("simulated round %d: saved metrics and %d samples", round_number, len(sample_paths))
    return sample_paths


def run_device_loop(base_url: str, token: str, device_id: str, total_rounds: int = 10, rounds_per_upload: int = 5, round_interval: float = 2.0):
    """Main loop: simulate rounds, persist, and upload every `rounds_per_upload` rounds."""
    ensure_dirs()
    collected_rounds = []
    for r in range(1, total_rounds + 1):
        simulate_round_and_collect(device_id, r)
        collected_rounds.append(r)
        # attempt immediate upload if server reachable? we follow policy: upload after rounds_per_upload
        if len(collected_rounds) >= rounds_per_upload:
            # gather pending files for these rounds
            metrics_files = []
            round_numbers = []
            for mr in collected_rounds:
                pattern = f"metrics_{device_id}_r{mr}.jsonl"
                fpath = STATE_DIR / pattern
                if fpath.exists():
                    metrics_files.append(fpath)
                    round_numbers.append(mr)
            # aggregate
            agg = aggregate_metrics_for_upload(device_id, round_numbers, metrics_files)
            ok = send_metrics(base_url, token, agg)
            if ok:
                # upload samples for these rounds
                pending_samples = load_pending_samples()
                # filter to round numbers
                to_upload = [p for p in pending_samples if any(f"_r{rn}_" in p.name for rn in round_numbers)]
                for p in to_upload:
                    # parse round number from filename
                    rn = next((int(part[1:]) for part in p.name.split("_") if part.startswith("r") ), round_numbers[-1])
                    su = upload_sample_file(base_url, token, p, device_id, rn)
                    if su:
                        try:
                            p.unlink()
                        except Exception:
                            pass
                # cleanup metric files
                cleanup_files(metrics_files)
                collected_rounds = []
            else:
                logger.info("metrics upload failed; will retry after next rounds or on next run")
        time.sleep(round_interval)
    logger.info("device loop finished")


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--base-url', default=DEFAULT_BASE)
    parser.add_argument('--token', default=DEFAULT_TOKEN)
    parser.add_argument('--device-id', default='device-001')
    parser.add_argument('--total-rounds', type=int, default=10)
    parser.add_argument('--rounds-per-upload', type=int, default=5)
    parser.add_argument('--round-interval', type=float, default=2.0)
    args = parser.parse_args()

    logger.info('starting device client: device=%s base=%s', args.device_id, args.base_url)
    run_device_loop(args.base_url, args.token, args.device_id, args.total_rounds, args.rounds_per_upload, args.round_interval)
