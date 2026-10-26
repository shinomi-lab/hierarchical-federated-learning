import json
import sqlite3

from scripts import summarize_baseline as sb


def _db(tmp_path, sessions):
    p = tmp_path / "m.db"
    con = sqlite3.connect(p)
    con.execute("create table metrics (id integer primary key autoincrement, terminal_id text, raw_json text)")
    for term, j in sessions:
        j = {"row_kind": "baseline_session", "app_type": "動画", **j}
        con.execute("insert into metrics (terminal_id, raw_json) values (?, ?)", (term, json.dumps(j)))
    con.commit()
    con.close()
    return str(p)


def S(mode, before, after, **extra):
    return {"baseline_mode": mode, "satisfaction_baseline_mean": before, "satisfaction_after": after,
            "after_switch_confirmed": True, **extra}


def test_hfl_rec_rows_use_same_columns_and_carry_followed_and_initial(tmp_path):
    db = _db(tmp_path, [
        ("terminal-01", S("greedy", 0.5, 0.6)),
        ("terminal-01", S("hfl_rec", 0.5, 0.7, hfl_rec_ap=1, hfl_rec_followed=True, hfl_rec_is_initial=False, hfl_rec_round=4)),
    ])
    rs = sb.rows(db, 0)
    assert [r["mode"] for r in rs] == ["greedy", "hfl_rec"]
    h = rs[1]
    assert abs(h["delta"] - 0.2) < 1e-9 and h["rec_ap"] == 1 and h["followed"] is True and h["initial"] is False


def test_initial_model_sessions_excluded_by_default_and_included_on_request(tmp_path):
    db = _db(tmp_path, [
        ("terminal-01", S("hfl_rec", 0.5, 0.6, hfl_rec_ap=0, hfl_rec_followed=True, hfl_rec_is_initial=True)),
        ("terminal-01", S("hfl_rec", 0.5, 0.7, hfl_rec_ap=0, hfl_rec_followed=False, hfl_rec_is_initial=False)),
    ])
    rs = sb.rows(db, 0)
    kept, n_init = sb.split_initial(rs, include_initial=False)
    assert len(kept) == 1 and n_init == 1 and kept[0]["followed"] is False
    kept2, n_init2 = sb.split_initial(rs, include_initial=True)
    assert len(kept2) == 2 and n_init2 == 1


def test_mode_summary_counts_followed_and_initial(tmp_path):
    db = _db(tmp_path, [
        ("terminal-01", S("hfl_rec", 0.5, 0.7, hfl_rec_ap=1, hfl_rec_followed=True, hfl_rec_is_initial=False)),
        ("terminal-02", S("hfl_rec", 0.5, 0.4, hfl_rec_ap=1, hfl_rec_followed=False, hfl_rec_is_initial=False)),
        ("terminal-03", S("hfl_rec", 0.5, 0.5, hfl_rec_ap=1, hfl_rec_followed=None, hfl_rec_is_initial=False)),
        ("terminal-01", S("greedy", 0.5, 0.6)),
    ])
    kept, n_init = sb.split_initial(sb.rows(db, 0), include_initial=False)
    m = {r["mode"]: r for r in sb.mode_summary(kept, {"hfl_rec": n_init})}
    assert m["hfl_rec"]["n"] == 3 and m["hfl_rec"]["followed_n"] == 1 and m["hfl_rec"]["followed_total"] == 2   # null は分母に入れない
    assert m["hfl_rec"]["initial_n"] == 0 and m["greedy"]["followed_n"] is None
    assert abs(m["hfl_rec"]["delta"] - (0.2 - 0.1 + 0.0) / 3) < 1e-9


def test_rec_agreement_between_hfl_rec_and_central(tmp_path):
    db = _db(tmp_path, [
        ("terminal-01", S("hfl_rec", 0.5, 0.6, hfl_rec_ap=1, hfl_rec_is_initial=False)),
        ("terminal-02", S("hfl_rec", 0.5, 0.6, hfl_rec_ap=0, hfl_rec_is_initial=False)),
        ("terminal-01", S("central", 0.5, 0.6, central_ap=1)),
        ("terminal-02", S("central", 0.5, 0.6, central_ap=1)),
    ])
    kept, _ = sb.split_initial(sb.rows(db, 0), include_initial=False)
    assert sb.rec_agreement(kept) == (1, 2)   # t01 は一致、t02 は不一致


def test_existing_modes_unchanged_in_main_output(tmp_path, capsys):
    db = _db(tmp_path, [("terminal-01", S("fixed", 0.5, 0.55))])
    assert sb.main(["--db", db]) == 0
    out = capsys.readouterr().out
    assert "fixed" in out and "followed" in out


def test_fixed_ap1_and_new_columns(tmp_path, capsys):
    db = _db(tmp_path, [
        ("terminal-01", S("fixed_ap1", 0.9, 0.8, after_settle_ms=20000)),
        ("terminal-02", S("random", 0.9, 0.9, random_choice_ap=0, after_settle_ms=20000)),
        ("terminal-03", S("random", 0.9, 0.7)),   # 以前のデータ（項目なし）
    ])
    rs = sb.rows(db, 0)
    assert rs[0]["mode"] == "fixed_ap1" and rs[0]["settle_ms"] == 20000 and rs[0]["random_ap"] is None
    assert rs[1]["random_ap"] == 0 and rs[2]["random_ap"] is None and rs[2]["settle_ms"] is None
    assert sb.main(["--db", db]) == 0
    out = capsys.readouterr().out
    assert "fixed_ap1" in out and "random_ap" in out and "settle_ms" in out
