"""ランナーの副作用（adb・サーバ・遅延・通知）。テストではフェイクに差し替える。計画書 Task 6・設計書 §6。

コマンドの組み立ては純関数にしてテストする。実際の adb・サーバ起動・sudo はテストしない。
"""
import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional, Tuple

REPO = Path(__file__).resolve().parents[3]
PKG = "com.example.hfl_experiment"
CENTRAL_PORT = 8000
EDGE_PORTS = {"edge-00": 8001, "edge-01": 8002}  # start.py の EDGE_BASE_PORT から順に
HOST = "127.0.0.1"


# ── 純関数 ──────────────────────────────────────────────────────────────

def adb_auto_start_cmd(serial: str, run_tag: str, num_cycles: Optional[int]) -> List[str]:
    return ["adb", "-s", serial, "shell", "am", "start", "--activity-single-top", "-n", f"{PKG}/.ui.MainActivity",
            "--ez", "auto_start", "true", "--es", "run_tag", run_tag,
            *(["--ei", "num_cycles", str(num_cycles)] if num_cycles else [])]


def server_start_cmd(run_dir: Path, mode: str, server: dict) -> Tuple[Dict[str, str], List[str]]:
    """実行ディレクトリの hfl ラッパで start.py を起動する (環境変数, argv)。設定は過去の実機（6/18）準拠が既定。"""
    argv = [str(Path(run_dir) / "hfl"), "start.py", "--run-mode", "production", "--env", str(server["env"]),
            "--edges", "2", "--hfl-mode", mode,
            "--edge-thresholds", ",".join(str(x) for x in server["edge_thresholds"])]
    if mode == "async":
        argv += ["--k-central", str(server["k_central"])]  # async の中央は K_CENTRAL だけを見る
    else:
        argv += ["--central-threshold", str(server["central_threshold"])]
    return {"HFL_MODE": mode}, argv


def parse_run_dir(prepare_stdout: str) -> Path:
    m = re.search(r"^作成: (.+?)（コミット", prepare_stdout, re.M)
    if not m:
        raise RuntimeError(f"prepare_server_run.sh の出力から実行ディレクトリが読めない: {prepare_stdout[:300]!r}")
    return Path(m.group(1))


def delay_apply_cmd(delay: dict, targets: dict, probes: dict, run_tag: str) -> List[str]:
    argv = [sys.executable, str(REPO / "scripts" / "net_delay.py"), "apply", "--sudo-noninteractive"]
    # 範囲が重なり遅延が違う --to（旧: B=0 と A の /24=100）は net_delay.py が拒否する。2026-10-22 の実機で、quick を付けても
    # 広い方の遅延が B にも掛かると判明したため。A の範囲に遅延をかけ、B（遅延なし）は --exclude で除く。
    if delay["A"] == delay["B"]:
        argv += ["--to", f"{targets['A']}={delay['A']}"]
        import ipaddress
        try:
            b_inside_a = ipaddress.ip_network(str(targets["B"]), strict=False).subnet_of(ipaddress.ip_network(str(targets["A"]), strict=False))
        except (ValueError, TypeError):
            b_inside_a = False
        if not b_inside_a:  # B が A の範囲の外なら、B にも同じ遅延を掛けないと対称にならない
            argv += ["--to", f"{targets['B']}={delay['B']}"]
    elif delay["B"] == 0:
        argv += ["--to", f"{targets['A']}={delay['A']}", "--exclude", str(targets["B"])]
    else:
        raise ValueError(f"B にだけ遅延をかける指定は未対応: {delay}")
    for k in ("A", "B"):
        for ip in probes[k]:
            argv += ["--probe", str(ip)]
    return argv + ["--note", run_tag]


def central_k_values(log_text: str) -> set:
    """中央ログの async イベント（JSON）に記録された K_CENTRAL の値の集合（docker/run_ch4_rerun.py と同じ）。"""
    return {int(v) for v in re.findall(r'"K_CENTRAL": (\d+)', log_text)}


def edge_condition(metas: Dict[str, dict]) -> Dict[str, dict]:
    out = {}
    for eid, m in metas.items():
        th = m.get("aggregation_threshold")
        out[eid] = {"hfl_mode": m.get("hfl_mode"), "aggregation_threshold": int(th) if th is not None else None}
    return out


# ── 実装 ────────────────────────────────────────────────────────────────

def _run(argv, timeout=120, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, **kw)


def _http(method: str, url: str, body: Optional[dict] = None, timeout: float = 5.0):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode() or "null")


def _port_open(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex((HOST, port)) == 0


class Adb:
    def ensure_connected(self, serial: str) -> bool:
        for _ in range(2):  # 失敗したら 1 回だけ再接続
            if ":" in serial:
                _run(["adb", "connect", serial], timeout=20)
            r = _run(["adb", "-s", serial, "get-state"], timeout=20)
            if r.returncode == 0 and r.stdout.strip() == "device":
                return True
            time.sleep(2)
        return False

    def restart_app(self, serial: str) -> None:
        _run(["adb", "-s", serial, "shell", "am", "force-stop", PKG], timeout=30)

    def auto_start(self, serial: str, run_tag: str, num_cycles: Optional[int]) -> None:
        r = _run(adb_auto_start_cmd(serial, run_tag, num_cycles), timeout=30)
        if r.returncode != 0 or "Error" in r.stdout + r.stderr:
            raise RuntimeError(f"auto_start に失敗 {serial}: {(r.stdout + r.stderr).strip()[:200]}")

    def pull_ledger(self, serial: str, dest: Path) -> Path:
        r = subprocess.run(["adb", "-s", serial, "exec-out", "run-as", PKG, "cat", "files/metrics_ledger.jsonl"],
                           capture_output=True, timeout=120)
        if r.returncode != 0:
            raise RuntimeError(f"台帳の回収に失敗 {serial}: {r.stderr.decode(errors='replace')[:200]}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(r.stdout)
        return dest


class Server:
    """毎試行、prepare_server_run.sh の実行ディレクトリから start.py を起動する（graceful_reset は使わない）。"""

    def __init__(self, runs_dir: str):
        self.runs_dir = Path(os.path.expanduser(runs_dir))
        self.proc: Optional[subprocess.Popen] = None
        self._log = None
        self.run_dir: Optional[Path] = None
        self.args: List[str] = []

    def prepare(self, run_tag: str) -> Path:
        r = _run(["bash", str(REPO / "scripts" / "prepare_server_run.sh"), run_tag],
                 env={**os.environ, "HFL_RUNS_DIR": str(self.runs_dir)}, timeout=300)
        if r.returncode != 0:
            raise RuntimeError(f"prepare_server_run.sh に失敗: {r.stderr.strip()[:300]}")
        return parse_run_dir(r.stdout)

    def _wait_ports(self, want_open: bool, timeout: float) -> bool:
        ports = [CENTRAL_PORT, *EDGE_PORTS.values()]
        end = time.time() + timeout
        while time.time() < end:
            if all(_port_open(p) == want_open for p in ports):
                return True
            time.sleep(0.5)
        return False

    def start(self, run_dir: Path, mode: str, server: dict) -> None:
        if not self._wait_ports(False, 5):
            raise RuntimeError("サーバのポート（8000〜8002）が使用中。前のサーバが残っている")
        env, argv = server_start_cmd(run_dir, mode, server)
        self.run_dir, self.args = Path(run_dir), argv
        self._log = open(self.run_dir / "server_stdout.log", "w", encoding="utf-8")
        # start.py は対話の質問（AI アドバイザー [y/N]）に EOF が来ると終了するので、「N」を流し込む（docker の echo "N" | と同じ）
        self.proc = subprocess.Popen(argv, env={**os.environ, **env}, stdout=self._log, stderr=subprocess.STDOUT,
                                     stdin=subprocess.PIPE, text=True, start_new_session=True)
        try:
            self.proc.stdin.write("N\n" * 4)
            self.proc.stdin.flush()
        except OSError:
            pass

    def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            pgid = os.getpgid(self.proc.pid)
            os.killpg(pgid, signal.SIGTERM)
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                os.killpg(pgid, signal.SIGKILL)
                self.proc.wait(10)
        self.proc = None
        if self._log is not None:
            self._log.close()
            self._log = None
        if not self._wait_ports(False, 30):
            raise RuntimeError("サーバを止めたがポートが閉じない")

    def healthy(self) -> bool:
        try:
            for port in (CENTRAL_PORT, *EDGE_PORTS.values()):
                urllib.request.urlopen(f"http://{HOST}:{port}/healthz", timeout=3).read()
            return True
        except Exception:
            return False

    def condition(self) -> dict:
        metas = {eid: _http("GET", f"http://{HOST}:{port}/api/v1/meta") for eid, port in EDGE_PORTS.items()}
        log = (self.run_dir / "server_stdout.log").read_text(encoding="utf-8", errors="replace") if self.run_dir else ""
        return {"edges": edge_condition(metas), "k_central_seen": central_k_values(log)}

    def set_control(self, baseline: str, num_cycles: int) -> None:
        _http("PUT", f"http://{HOST}:{CENTRAL_PORT}/api/v1/experiment_control",
              {"baseline_mode": baseline, "num_cycles": num_cycles})

    def events(self, type_: str, run_tag: str) -> List[dict]:
        q = urllib.parse.urlencode({"type": type_, "run_tag": run_tag})
        return _http("GET", f"http://{HOST}:{CENTRAL_PORT}/api/v1/experiment_events?{q}")["events"]

    @staticmethod
    def db_path(run_dir: Path) -> Path:
        return Path(run_dir) / "storage" / "received_edges" / "training_metrics.db"


class DelayHook:
    """scripts/net_delay.py を呼ぶ（pf/dnctl を直接触らない）。無人のため sudo -n（sudoers で NOPASSWD）。"""

    def __init__(self, targets: Optional[dict], probes: Optional[dict]):
        self.targets, self.probes = targets, probes
        self.last_output = ""

    def available(self) -> bool:
        try:
            return _run(["sudo", "-n", "dnctl", "pipe", "list"], timeout=10).returncode == 0
        except Exception:
            return False

    def apply(self, delay: dict, run_tag: str) -> bool:
        r = _run(delay_apply_cmd(delay, self.targets, self.probes, run_tag), timeout=180, cwd=str(REPO))
        self.last_output = (r.stdout + r.stderr)[-4000:]
        return r.returncode == 0

    def clear(self, run_tag: str) -> None:
        _run([sys.executable, str(REPO / "scripts" / "net_delay.py"), "clear", "--sudo-noninteractive",
              "--note", run_tag], timeout=60, cwd=str(REPO))


class Notifier:
    """macOS の通知とログファイル。外部には送らない。"""

    def __init__(self, log_path: Path):
        self.log_path = Path(log_path)

    def log(self, msg: str) -> None:
        line = f"[{time.strftime('%H:%M:%S')}] {msg}"
        print(line, flush=True)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    def notify(self, title: str, message: str) -> None:
        self.log(f"{title}: {message}")
        script = f"display notification {json.dumps(message)} with title {json.dumps(title)}"
        try:
            _run(["osascript", "-e", script], timeout=10)
        except Exception:
            pass
