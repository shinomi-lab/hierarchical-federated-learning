import os
import re

# Refactor data_gen.py
with open("sim/data_gen.py", "r", encoding="utf-8") as f:
    data_gen = f.read()

data_gen_new = data_gen.replace(
"""            # 仮想的な AP_A (Wi-Fi) と AP_B (Cellular) の状況を生成して最適APを決定する
            # AP_A: 普段は高速だが、時々RTTが跳ね上がる (混雑)
            ap_a_tp = rng.uniform(20.0, 60.0)
            ap_a_rtt = rng.uniform(10.0, 500.0) if rng.random() > 0.3 else rng.uniform(500.0, 2000.0)
            
            # AP_B: 速度は遅めだが、RTTは安定
            ap_b_tp = rng.uniform(5.0, 25.0)
            ap_b_rtt = rng.uniform(30.0, 150.0)

            sat_a = terminal_satisfaction(app_idx, ap_a_tp, ap_a_rtt)
            sat_b = terminal_satisfaction(app_idx, ap_b_tp, ap_b_rtt)

            best_ap = 0 if sat_a >= sat_b else 1""",
"""            # 動的 AP の状況を生成して最適APを決定する (現状は2台だがN台にも対応可能とする)
            # (ここではランダム生成のため、AP数が変わった場合の対応は別途必要ですが、とりあえず 2AP以上に対応するランダム生成)
            # AP_0: 普段は高速だが、時々RTTが跳ね上がる
            ap_0_tp = rng.uniform(20.0, 60.0)
            ap_0_rtt = rng.uniform(10.0, 500.0) if rng.random() > 0.3 else rng.uniform(500.0, 2000.0)
            
            # AP_1: 速度は遅めだが、RTTは安定
            ap_1_tp = rng.uniform(5.0, 25.0)
            ap_1_rtt = rng.uniform(30.0, 150.0)

            sat_0 = terminal_satisfaction(app_idx, ap_0_tp, ap_0_rtt)
            sat_1 = terminal_satisfaction(app_idx, ap_1_tp, ap_1_rtt)

            best_ap = 0 if sat_0 >= sat_1 else 1""")

with open("sim/data_gen.py", "w", encoding="utf-8") as f:
    f.write(data_gen_new)

# Refactor terminal.py
with open("sim/terminal.py", "r", encoding="utf-8") as f:
    term = f.read()

term_new = term.replace(
"""    def run_inference(
        self,
        run_id:   str,
        round_id: int,
        ap_tp_a:  float = 0.0,
        ap_rtt_a: float = 0.0,
        ap_tp_b:  float = 0.0,
        ap_rtt_b: float = 0.0,
        app_idx:  int   = 0,
        best_ap:  int   = 0,
    ) -> InferenceResult:""",
"""    def run_inference(
        self,
        run_id:   str,
        round_id: int,
        ap_conditions: list[tuple[float, float]] = None,
        app_idx:  int   = 0,
        best_ap:  int   = 0,
    ) -> InferenceResult:
        if ap_conditions is None:
            ap_conditions = [(0.0, 0.0)] * 2
""")

term_new = term_new.replace(
"""        # ── 現在接続APのTP/RTT を選択 ────────────────────────────────
        cur_ap  = self._current_ap
        ap_tp   = ap_tp_a  if cur_ap == 0 else ap_tp_b
        ap_rtt  = ap_rtt_a if cur_ap == 0 else ap_rtt_b""",
"""        # ── 現在接続APのTP/RTT を選択 ────────────────────────────────
        cur_ap  = self._current_ap
        if cur_ap >= len(ap_conditions):
            cur_ap = 0 # fallback
            self._current_ap = cur_ap
        ap_tp, ap_rtt = ap_conditions[cur_ap]""")

term_new = term_new.replace(
"""        satisfaction_before = terminal_satisfaction(app_idx, ap_tp, ap_rtt)

        new_tp  = ap_tp_a  if predicted_ap == 0 else ap_tp_b
        new_rtt = ap_rtt_a if predicted_ap == 0 else ap_rtt_b
        satisfaction = terminal_satisfaction(app_idx, new_tp, new_rtt)""",
"""        satisfaction_before = terminal_satisfaction(app_idx, ap_tp, ap_rtt)

        if predicted_ap >= len(ap_conditions):
            predicted_ap = 0 # fallback
            
        new_tp, new_rtt = ap_conditions[predicted_ap]
        satisfaction = terminal_satisfaction(app_idx, new_tp, new_rtt)""")

with open("sim/terminal.py", "w", encoding="utf-8") as f:
    f.write(term_new)

# Refactor runner.py
with open("sim/runner.py", "r", encoding="utf-8") as f:
    runner = f.read()

runner_new = runner.replace(
"""        # ── APネットワーク特性（config/default.yaml ap_a, ap_b）────
        ap_a_cfg = data_cfg.get("ap_a", {})
        ap_b_cfg = data_cfg.get("ap_b", {})

        # Gaussian fallback 用（待ち行列モデル未設定時のみ使用）
        self.ap_tp_mean  = [ap_a_cfg.get("tp_mean",  15.0), ap_b_cfg.get("tp_mean",   5.0)]
        self.ap_tp_std   = [ap_a_cfg.get("tp_std",    3.0), ap_b_cfg.get("tp_std",    1.0)]
        self.ap_rtt_mean = [ap_a_cfg.get("rtt_mean",150.0), ap_b_cfg.get("rtt_mean", 30.0)]
        self.ap_rtt_std  = [ap_a_cfg.get("rtt_std",  20.0), ap_b_cfg.get("rtt_std",   8.0)]

        # ── 待ち行列モデルパラメータ（M/M/1 + Erlang-B）────────────
        # mu_rtt が設定されていれば待ち行列モデルを使用、なければ Gaussian にフォールバック
        self.ap_queue_params: list[dict] = []
        for ap_cfg in [ap_a_cfg, ap_b_cfg]:
            self.ap_queue_params.append({
                "mu_rtt":        ap_cfg.get("mu_rtt"),           # None = Gaussian fallback
                "base_rtt_ms":   ap_cfg.get("rtt_mean", 100.0),  # initial_rtt_ms
                "n_channels":    ap_cfg.get("n_channels",    2),
                "tp_init_mbps":  ap_cfg.get("tp_mean",   50.0),  # tp_init_mbps
                "rtt_noise_std": ap_cfg.get("rtt_noise_std", 2.0),
                "tp_noise_std":  ap_cfg.get("tp_noise_std",  1.0),
            })
        self.use_queue_model: bool = (
            self.ap_queue_params[0]["mu_rtt"] is not None
        )""",
"""        # ── APネットワーク特性（config/default.yaml aps）────
        self.aps_cfg = data_cfg.get("aps", [])
        if not self.aps_cfg:
            # 互換性フォールバック
            self.aps_cfg = [
                data_cfg.get("ap_a", {"tp_mean": 15.0, "tp_std": 3.0, "rtt_mean": 150.0, "rtt_std": 20.0}),
                data_cfg.get("ap_b", {"tp_mean": 5.0,  "tp_std": 1.0, "rtt_mean": 30.0,  "rtt_std": 8.0})
            ]
        
        self.num_aps = len(self.aps_cfg)

        # Gaussian fallback 用
        self.ap_tp_mean  = [ap.get("tp_mean",  15.0) for ap in self.aps_cfg]
        self.ap_tp_std   = [ap.get("tp_std",    3.0) for ap in self.aps_cfg]
        self.ap_rtt_mean = [ap.get("rtt_mean",150.0) for ap in self.aps_cfg]
        self.ap_rtt_std  = [ap.get("rtt_std",  20.0) for ap in self.aps_cfg]

        # 待ち行列モデルパラメータ
        self.ap_queue_params: list[dict] = []
        for ap_cfg in self.aps_cfg:
            self.ap_queue_params.append({
                "mu_rtt":        ap_cfg.get("mu_rtt"),
                "base_rtt_ms":   ap_cfg.get("rtt_mean", 100.0),
                "n_channels":    ap_cfg.get("n_channels",    2),
                "tp_init_mbps":  ap_cfg.get("tp_mean",   50.0),
                "rtt_noise_std": ap_cfg.get("rtt_noise_std", 2.0),
                "tp_noise_std":  ap_cfg.get("tp_noise_std",  1.0),
            })
        self.use_queue_model: bool = (
            len(self.ap_queue_params) > 0 and self.ap_queue_params[0]["mu_rtt"] is not None
        )""")

runner_new = runner_new.replace(
"""            # ── 現在の接続台数を集計（端末 _current_ap から）────────────
            n_on_ap = [0, 0]
            for term in all_terminals:
                n_on_ap[term._current_ap] += 1""",
"""            # ── 現在の接続台数を集計（端末 _current_ap から）────────────
            n_on_ap = [0] * self.num_aps
            for term in all_terminals:
                if term._current_ap < self.num_aps:
                    n_on_ap[term._current_ap] += 1
                else:
                    n_on_ap[0] += 1 # fallback""")

runner_new = runner_new.replace(
"""                tp_rtt_per_ap: list[tuple[float, float]] = []
                for ap_idx in range(2):
                    p = self.ap_queue_params[ap_idx]
                    tp_val, rtt_val = ap_conditions(
                        n_connected   = n_on_ap[ap_idx],
                        mu_rtt        = p["mu_rtt"],
                        base_rtt_ms   = p["base_rtt_ms"],
                        n_channels    = p["n_channels"],
                        tp_init_mbps  = p["tp_init_mbps"],
                        rng           = rng_net,
                        rtt_noise_std = p["rtt_noise_std"],
                        tp_noise_std  = p["tp_noise_std"],
                    )
                    tp_rtt_per_ap.append((tp_val, rtt_val))
            else:
                # Gaussian フォールバック（待ち行列パラメータ未設定時）
                tp_rtt_per_ap = [
                    (
                        max(0.1, float(rng_net.normal(self.ap_tp_mean[i],  self.ap_tp_std[i]))),
                        max(1.0, float(rng_net.normal(self.ap_rtt_mean[i], self.ap_rtt_std[i]))),
                    )
                    for i in range(2)
                ]

            tp_a, rtt_a = tp_rtt_per_ap[0]
            tp_b, rtt_b = tp_rtt_per_ap[1]

            # 混雑サマリーをログに記録
            c_summary = congestion_summary(n_on_ap, self.ap_queue_params)
            self._write_congestion_log(g_rnd, n_on_ap, tp_a, rtt_a, tp_b, rtt_b, c_summary)""",
"""                tp_rtt_per_ap: list[tuple[float, float]] = []
                for ap_idx in range(self.num_aps):
                    p = self.ap_queue_params[ap_idx]
                    tp_val, rtt_val = ap_conditions(
                        n_connected   = n_on_ap[ap_idx],
                        mu_rtt        = p["mu_rtt"],
                        base_rtt_ms   = p["base_rtt_ms"],
                        n_channels    = p["n_channels"],
                        tp_init_mbps  = p["tp_init_mbps"],
                        rng           = rng_net,
                        rtt_noise_std = p["rtt_noise_std"],
                        tp_noise_std  = p["tp_noise_std"],
                    )
                    tp_rtt_per_ap.append((tp_val, rtt_val))
            else:
                # Gaussian フォールバック（待ち行列パラメータ未設定時）
                tp_rtt_per_ap = [
                    (
                        max(0.1, float(rng_net.normal(self.ap_tp_mean[i],  self.ap_tp_std[i]))),
                        max(1.0, float(rng_net.normal(self.ap_rtt_mean[i], self.ap_rtt_std[i]))),
                    )
                    for i in range(self.num_aps)
                ]

            # 混雑サマリーをログに記録
            c_summary = congestion_summary(n_on_ap, self.ap_queue_params)
            self._write_congestion_log(g_rnd, n_on_ap, tp_rtt_per_ap, c_summary)""")

runner_new = runner_new.replace(
"""            for tidx, term in enumerate(all_terminals):
                app = self._terminal_app_idx[tidx]

                # オラクル最適AP: 両APのsatisfactionを比較して決定
                s_a     = terminal_satisfaction(app, tp_a, rtt_a)
                s_b     = terminal_satisfaction(app, tp_b, rtt_b)
                best_ap = 0 if s_a >= s_b else 1

                inf = term.run_inference(
                    run_id   = self.run_id,
                    round_id = g_rnd,
                    ap_tp_a  = tp_a,
                    ap_rtt_a = rtt_a,
                    ap_tp_b  = tp_b,
                    ap_rtt_b = rtt_b,
                    app_idx  = app,
                    best_ap  = best_ap,
                )""",
"""            for tidx, term in enumerate(all_terminals):
                app = self._terminal_app_idx[tidx]

                # オラクル最適AP: 全APのsatisfactionを比較して決定
                satisfactions = [terminal_satisfaction(app, tp, rtt) for tp, rtt in tp_rtt_per_ap]
                best_ap = satisfactions.index(max(satisfactions)) if satisfactions else 0

                inf = term.run_inference(
                    run_id   = self.run_id,
                    round_id = g_rnd,
                    ap_conditions = tp_rtt_per_ap,
                    app_idx  = app,
                    best_ap  = best_ap,
                )""")

runner_new = runner_new.replace(
"""                tp_rtt_ap    = [(tp_a, rtt_a), (tp_b, rtt_b)],""",
"""                tp_rtt_ap    = tp_rtt_per_ap,""")

runner_new = runner_new.replace(
"""    def _write_congestion_log(
        self,
        g_rnd:     int,
        n_on_ap:   list[int],
        tp_a:      float,
        rtt_a:     float,
        tp_b:      float,
        rtt_b:     float,
        summary:   list[dict],
    ) -> None:
        \"\"\"AP の混雑状態と決定された TP/RTT をログに記録する。\"\"\"
        event = {
            "schema_version": 1,
            "ts_ms":          int(time.time() * 1000),
            "event":          "ap_congestion",
            "run_id":         self.run_id,
            "global_round":   g_rnd,
            "n_on_ap_a":      n_on_ap[0],
            "n_on_ap_b":      n_on_ap[1],
            "tp_a_mbps":      round(tp_a,  3),
            "rtt_a_ms":       round(rtt_a, 2),
            "tp_b_mbps":      round(tp_b,  3),
            "rtt_b_ms":       round(rtt_b, 2),
            "queue_model":    self.use_queue_model,
            "ap_details":     summary,
        }
        self._append_jsonl(event)""",
"""    def _write_congestion_log(
        self,
        g_rnd:         int,
        n_on_ap:       list[int],
        tp_rtt_per_ap: list[tuple[float, float]],
        summary:       list[dict],
    ) -> None:
        \"\"\"AP の混雑状態と決定された TP/RTT をログに記録する。\"\"\"
        event = {
            "schema_version": 1,
            "ts_ms":          int(time.time() * 1000),
            "event":          "ap_congestion",
            "run_id":         self.run_id,
            "global_round":   g_rnd,
            "n_on_ap":        n_on_ap,
            "tp_rtt_per_ap":  [{"tp_mbps": round(tp, 3), "rtt_ms": round(rtt, 2)} for tp, rtt in tp_rtt_per_ap],
            "queue_model":    self.use_queue_model,
            "ap_details":     summary,
        }
        self._append_jsonl(event)""")

runner_new = runner_new.replace(
"""            "n_on_ap_a":         n_on_ap[0] if n_on_ap else None,
            "n_on_ap_b":         n_on_ap[1] if n_on_ap else None,""",
"""            "n_on_ap":           n_on_ap,""")

with open("sim/runner.py", "w", encoding="utf-8") as f:
    f.write(runner_new)

# Refactor display.py
with open("sim/display.py", "r", encoding="utf-8") as f:
    disp = f.read()

disp_new = disp.replace(
"""    # 待ち行列モデル情報
    ap_a = dat.get("ap_a", {})
    ap_b = dat.get("ap_b", {})
    use_queue = ap_a.get("mu_rtt") is not None
    if use_queue:
        ap_a_str = (
            f"tp_init={ap_a.get('tp_mean',50)}Mbps "
            f"rtt_init={ap_a.get('rtt_mean',20)}ms "
            f"mu={ap_a.get('mu_rtt','?')} "
            f"n_ch={ap_a.get('n_channels','?')}"
        )
        ap_b_str = (
            f"tp_init={ap_b.get('tp_mean',20)}Mbps "
            f"rtt_init={ap_b.get('rtt_mean',80)}ms "
            f"mu={ap_b.get('mu_rtt','?')} "
            f"n_ch={ap_b.get('n_channels','?')}"
        )
        net_model = "M/M/1 + Erlang-B (virtual_congestion.py 同一)"
    else:
        ap_a_str = f"TP={ap_a.get('tp_mean',50)}±{ap_a.get('tp_std',3)} RTT={ap_a.get('rtt_mean',20)}±{ap_a.get('rtt_std',5)}"
        ap_b_str = f"TP={ap_b.get('tp_mean',20)}±{ap_b.get('tp_std',2)} RTT={ap_b.get('rtt_mean',80)}±{ap_b.get('rtt_std',10)}"
        net_model = "Gaussian"

    rows = [
        ("Edge servers",          str(fed.get("num_edge_servers", 2))),
        ("Terminals",             str(fed.get("num_terminals", 3))),
        ("Topology",              topo_str),
        ("Global rounds",         str(fed.get("num_rounds", 30))),
        ("Local rounds/G",        str(fed.get("local_rounds", 5))),
        ("Epochs/local",          str(lt.get("epochs", 5))),
        ("Learning rate",         str(lt.get("learning_rate", 0.0001))),
        ("Weight decay",          str(lt.get("weight_decay", 0.01))),
        ("Dropout",               str(mdl.get("dropout_p", 0.1))),
        ("Data dist.",            iid),
        ("Total samples",         str(dat.get("total_samples", 3000))),
        ("Model arch.",
         f"{mdl.get('input_size',6)}->{mdl.get('hidden_size',32)}"
         f"->{mdl.get('hidden_size',32)}->{mdl.get('output_size',4)}"),
        ("Network model",         net_model),
        ("AP_A (WiFi)",           ap_a_str),
        ("AP_B (Cellular)",       ap_b_str),
    ]""",
"""    aps = dat.get("aps", [])
    if not aps:
        aps = [dat.get("ap_a", {}), dat.get("ap_b", {})]
    
    use_queue = len(aps) > 0 and aps[0].get("mu_rtt") is not None
    net_model = "M/M/1 + Erlang-B" if use_queue else "Gaussian"

    rows = [
        ("Edge servers",          str(fed.get("num_edge_servers", 2))),
        ("Terminals",             str(fed.get("num_terminals", 3))),
        ("Topology",              topo_str),
        ("Global rounds",         str(fed.get("num_rounds", 30))),
        ("Local rounds/G",        str(fed.get("local_rounds", 5))),
        ("Epochs/local",          str(lt.get("epochs", 5))),
        ("Learning rate",         str(lt.get("learning_rate", 0.0001))),
        ("Weight decay",          str(lt.get("weight_decay", 0.01))),
        ("Dropout",               str(mdl.get("dropout_p", 0.1))),
        ("Data dist.",            iid),
        ("Total samples",         str(dat.get("total_samples", 3000))),
        ("Model arch.",
         f"{mdl.get('input_size',6)}->{mdl.get('hidden_size',32)}"
         f"->{mdl.get('hidden_size',32)}->{mdl.get('output_size',4)}"),
        ("Network model",         net_model),
    ]
    for i, ap in enumerate(aps):
        if use_queue:
            ap_str = f"tp={ap.get('tp_mean', 50)} rtt={ap.get('rtt_mean', 20)} mu={ap.get('mu_rtt')} n={ap.get('n_channels')}"
        else:
            ap_str = f"tp={ap.get('tp_mean', 50)}±{ap.get('tp_std', 3)} rtt={ap.get('rtt_mean', 20)}±{ap.get('rtt_std', 5)}"
        name = ap.get('name', f"AP_{i}")
        rows.append((name, ap_str))""")

disp_new = disp_new.replace(
"""        if tp_rtt_ap and len(tp_rtt_ap) == 2:
            tp_a, rtt_a = tp_rtt_ap[0]
            tp_b, rtt_b = tp_rtt_ap[1]
            ap_str = (
                f"{dim('AP_A')}:{tp_a:4.0f}M,{rtt_a:3.0f}ms  "
                f"{dim('AP_B')}:{tp_b:4.0f}M,{rtt_b:3.0f}ms"
            )""",
"""        if tp_rtt_ap:
            ap_parts = []
            for i, (tp, rtt) in enumerate(tp_rtt_ap):
                ap_parts.append(f"{dim(f'AP_{i}')}:{tp:4.0f}M,{rtt:3.0f}ms")
            ap_str = "  ".join(ap_parts)""")

disp_new = disp_new.replace(
"""        if n_on_ap and len(n_on_ap) == 2:
            n_str = f"{magenta(n_on_ap[0])} on A, {magenta(n_on_ap[1])} on B"
        else:""",
"""        if n_on_ap:
            n_str = ", ".join(f"{magenta(n)} on AP_{i}" for i, n in enumerate(n_on_ap))
        else:""")

with open("sim/display.py", "w", encoding="utf-8") as f:
    f.write(disp_new)
