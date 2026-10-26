from scripts.set_mode import build_request


def test_build_requests():
    assert build_request(["fixed"]) == ("PUT", "/api/v1/experiment_control", {"baseline_mode": "fixed"})
    assert build_request(["fixed", "--terminal", "terminal-02"]) == ("PUT", "/api/v1/experiment_control", {"baseline_mode": "fixed", "terminal_id": "terminal-02"})
    assert build_request(["--clear"]) == ("PUT", "/api/v1/experiment_control", {"clear": True})
    assert build_request(["--clear", "--terminal", "t2"]) == ("PUT", "/api/v1/experiment_control", {"clear": True, "terminal_id": "t2"})
    assert build_request(["--show"]) == ("GET", "/api/v1/experiment_control", None)


def test_build_num_cycles_requests():
    assert build_request(["--num-cycles", "20"]) == ("PUT", "/api/v1/experiment_control", {"num_cycles": 20})
    assert build_request(["--num-cycles", "3", "--terminal", "terminal-02"]) == (
        "PUT", "/api/v1/experiment_control", {"num_cycles": 3, "terminal_id": "terminal-02"})
    assert build_request(["--clear-num-cycles"]) == ("PUT", "/api/v1/experiment_control", {"clear_num_cycles": True})
    assert build_request(["--clear-num-cycles", "--terminal", "t2"]) == (
        "PUT", "/api/v1/experiment_control", {"clear_num_cycles": True, "terminal_id": "t2"})
    assert build_request(["fixed", "--num-cycles", "20"]) == (
        "PUT", "/api/v1/experiment_control", {"baseline_mode": "fixed", "num_cycles": 20})


def test_num_cycles_out_of_range_rejected_locally():
    import pytest
    for bad in ["0", "101", "abc", "2.5"]:
        with pytest.raises(SystemExit):
            build_request(["--num-cycles", bad])
    with pytest.raises(SystemExit):
        build_request([])


def test_central_mode_is_accepted():
    assert build_request(["central"]) == ("PUT", "/api/v1/experiment_control", {"baseline_mode": "central"})
    assert build_request(["central", "--terminal", "terminal-02"]) == (
        "PUT", "/api/v1/experiment_control", {"baseline_mode": "central", "terminal_id": "terminal-02"})


def test_hfl_rec_mode_is_accepted():
    assert build_request(["hfl_rec"]) == ("PUT", "/api/v1/experiment_control", {"baseline_mode": "hfl_rec"})
    assert build_request(["hfl_rec", "--terminal", "terminal-02"]) == (
        "PUT", "/api/v1/experiment_control", {"baseline_mode": "hfl_rec", "terminal_id": "terminal-02"})


def test_fixed_ap1_is_accepted_and_near_miss_rejected():
    import pytest
    assert build_request(["fixed_ap1"]) == ("PUT", "/api/v1/experiment_control", {"baseline_mode": "fixed_ap1"})
    with pytest.raises(SystemExit):
        build_request(["fixed_ap2"])
