#!/usr/bin/env python3
"""ベースラインモードとサイクル数を中央で設定する（反映は各端末の次セッションから）。

例: set_mode.py fixed / set_mode.py fixed --terminal terminal-02 / set_mode.py --clear [--terminal T] / set_mode.py --show
    set_mode.py --num-cycles 20 [--terminal T] / set_mode.py --clear-num-cycles [--terminal T]
    set_mode.py fixed --num-cycles 3   （モードとサイクル数を同時に指定）
"""
import argparse
import json
import sys
import urllib.request

MODES = ["none", "fixed", "random", "greedy", "central", "hfl_rec", "fixed_ap1"]
NUM_CYCLES_MIN, NUM_CYCLES_MAX = 1, 100


def _cycles(v: str) -> int:
    try:
        n = int(v)
    except ValueError:
        raise argparse.ArgumentTypeError(f"整数を指定してください: {v!r}")
    if not NUM_CYCLES_MIN <= n <= NUM_CYCLES_MAX:
        raise argparse.ArgumentTypeError(f"{NUM_CYCLES_MIN}〜{NUM_CYCLES_MAX} を指定してください: {n}")
    return n


def _parser():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("mode", nargs="?", choices=MODES)
    p.add_argument("--terminal")
    p.add_argument("--clear", action="store_true", help="ベースラインモードの指定を解除")
    p.add_argument("--num-cycles", type=_cycles, help="1 セッションのサイクル数（1〜100）")
    p.add_argument("--clear-num-cycles", action="store_true", help="サイクル数の指定を解除")
    p.add_argument("--show", action="store_true")
    p.add_argument("--central", default="http://127.0.0.1:8000")
    return p


def build_request(argv):
    a = _parser().parse_args(argv)
    if a.show:
        return "GET", "/api/v1/experiment_control", None
    body = {}
    if a.clear:
        body["clear"] = True
    elif a.mode:
        body["baseline_mode"] = a.mode
    if a.clear_num_cycles:
        body["clear_num_cycles"] = True
    elif a.num_cycles is not None:
        body["num_cycles"] = a.num_cycles
    if not body:
        _parser().error("mode か --clear か --num-cycles か --clear-num-cycles か --show を指定してください")
    if a.terminal:
        body["terminal_id"] = a.terminal
    return "PUT", "/api/v1/experiment_control", body


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    method, path, body = build_request(argv)
    central = _parser().parse_args(argv).central.rstrip("/")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(central + path, data=data, method=method, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        print(json.dumps(json.loads(r.read()), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
