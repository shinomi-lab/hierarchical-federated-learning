# Serverside_HFL/tests/test_train_central_model.py
import hashlib
import json
import random

import torch

from tools import train_central_model as tcm


def _write_csv(path, n=240, seed=0):
    """tpNeed, rttNeed, appNum, label（ヘッダ無し）。label はアプリで決まる単純な規則。"""
    rnd = random.Random(seed)
    tp_rtt = {0: (5, 0), 1: (10, 0), 2: (0, 200), 3: (0, 100)}
    with open(path, "w", encoding="utf-8") as f:
        for _ in range(n):
            app = rnd.randrange(4)
            tp, rtt = tp_rtt[app]
            f.write(f"{tp},{rtt},{app},{0 if app < 2 else 1}\n")


def _sha_of_state(sd):
    h = hashlib.sha256()
    for k in sorted(sd):
        h.update(sd[k].detach().cpu().numpy().astype("<f4").tobytes())
    return h.hexdigest()


def test_outputs_model_and_meta(tmp_path):
    csv_path = tmp_path / "t.csv"
    _write_csv(csv_path)
    out = tmp_path / "out"
    meta = tcm.train(csv_path, out, epochs=3, seed=7)

    sd = torch.load(out / "central_model.pt", map_location="cpu")
    assert tuple(sd["layer1.weight"].shape) == (32, 6)
    assert tuple(sd["layer3.weight"].shape) == (2, 32)
    assert set(sd) == {f"{n}.{p}" for n in ("layer1", "norm1", "layer2", "norm2", "layer3") for p in ("weight", "bias")}

    saved = json.loads((out / "central_model.meta.json").read_text(encoding="utf-8"))
    assert saved == meta
    assert meta["rows"] == 240 and meta["epochs"] == 3 and meta["seed"] == 7
    assert 0.0 <= meta["val_accuracy"] <= 1.0 and 0.0 <= meta["test_accuracy"] <= 1.0
    assert meta["train_rows"] + meta["val_rows"] + meta["test_rows"] == 240
    assert meta["train_seconds"] >= 0.0
    # SHA256 は重みの float32 バイト列（キー名ソート順）に対して取る
    assert meta["model_sha256"] == _sha_of_state(sd)


def test_same_input_same_sha(tmp_path):
    csv_path = tmp_path / "t.csv"
    _write_csv(csv_path)
    a = tcm.train(csv_path, tmp_path / "a", epochs=2, seed=1)
    b = tcm.train(csv_path, tmp_path / "b", epochs=2, seed=1)
    assert a["model_sha256"] == b["model_sha256"]


def test_different_seed_different_sha(tmp_path):
    csv_path = tmp_path / "t.csv"
    _write_csv(csv_path)
    a = tcm.train(csv_path, tmp_path / "a", epochs=2, seed=1)
    b = tcm.train(csv_path, tmp_path / "b", epochs=2, seed=2)
    assert a["model_sha256"] != b["model_sha256"]


def test_features_match_terminal_loader(tmp_path):
    """端末 SimpleDataLoader.loadFlexible と同じ: tp・rtt は z-score、appNum は one-hot（4）。"""
    csv_path = tmp_path / "t.csv"
    csv_path.write_text("10,0,1,0\n0,200,2,1\n", encoding="utf-8")
    x, y, norm = tcm.load_dataset(csv_path)
    assert x.shape == (2, 6) and y.tolist() == [0, 1]
    assert abs(norm["tp_mean"] - 5.0) < 1e-6 and abs(norm["tp_std"] - 5.0) < 1e-6
    assert abs(x[0, 0].item() - 1.0) < 1e-4 and abs(x[1, 0].item() + 1.0) < 1e-4
    assert x[0, 2:].tolist() == [0.0, 1.0, 0.0, 0.0] and x[1, 2:].tolist() == [0.0, 0.0, 1.0, 0.0]


def test_runs_as_a_script_from_another_directory(tmp_path):
    """ランブックの使い方（python tools/train_central_model.py …）。pytest 経由では見えない import の不具合を防ぐ。"""
    import subprocess
    import sys
    from pathlib import Path
    csv_path = tmp_path / "t.csv"
    _write_csv(csv_path)
    root = Path(__file__).resolve().parent.parent
    r = subprocess.run([sys.executable, str(root / "tools" / "train_central_model.py"), str(csv_path),
                        "--out-dir", str(tmp_path / "o"), "--epochs", "1"], cwd=tmp_path, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert (tmp_path / "o" / "central_model.pt").is_file()
