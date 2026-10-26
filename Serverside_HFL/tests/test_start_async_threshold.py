import start


def test_effective_mode_reads_env_when_cli_is_empty(monkeypatch):
    monkeypatch.setenv("HFL_MODE", "Async")
    assert start.effective_hfl_mode("") == "async"
    assert start.effective_hfl_mode("sync") == "sync"  # CLI が優先
    monkeypatch.delenv("HFL_MODE")
    assert start.effective_hfl_mode("") == "sync"


def test_effective_k_central(monkeypatch):
    monkeypatch.delenv("K_CENTRAL", raising=False)
    assert start.effective_k_central(None) == 2
    monkeypatch.setenv("K_CENTRAL", "1")
    assert start.effective_k_central(None) == 1
    assert start.effective_k_central(2) == 2  # CLI が優先


def test_warns_when_central_threshold_given_in_async():
    msg = start.async_central_threshold_warning("async", cli_central=1, k_central=2)
    assert msg is not None
    assert "K_CENTRAL=2" in msg and "--k-central" in msg


def test_no_warning_in_sync_or_without_cli_value():
    assert start.async_central_threshold_warning("sync", cli_central=1, k_central=2) is None
    assert start.async_central_threshold_warning("async", cli_central=None, k_central=2) is None


def test_async_does_not_ask_central_threshold(monkeypatch):
    # async の中央は K_CENTRAL を使うので、--edge-thresholds だけでメニューを飛ばす（echo "N" | の入力をずらさない）
    def no_input(*a, **k):
        raise AssertionError("prompted")
    monkeypatch.setattr(start, "_ask", no_input)
    central, edges = start.select_thresholds(
        2, [4, 4], 1, cli_central=None, cli_edge_str="4,4", mode="async")
    assert central == 1 and edges == [4, 4]
