"""エッジ・中央の全エンドポイントのモジュールが、実行中の Python で import できること
（OpenSpec fix-ap-states-py39, edge-router-load-visibility）。

背景: 2026-10-21 に、ap_states.py の `str | None`（3.10 以降の構文）がサーバの Python 3.9 で読み込めないことが分かった。
GitHub Actions は 3.11 で動いていたので検出できなかった。この試験を配備先と同じ Python（3.9）で実行する。
`ast.parse(feature_version=(3, 9))` の静的検査では検出できない（`str | None` は構文としては正しく、実行時の型の評価で失敗する）。

central_server.main は追跡ファイル(current_meta.json・ログ)を書き換えるので import しない。
中央のエンドポイントの保存先は tests/conftest.py が一時ディレクトリに向ける。
"""
import importlib
import pkgutil

import pytest

PACKAGES = ["edge_server.endpoints", "central_server.endpoints"]

# どこからも参照されず、どの Python でも import できない既知のモジュール（spec の例外）。
# strict な xfail なので、直って import できるようになったら XPASS で失敗し、この一覧から外すことを促す。
KNOWN_BROKEN_UNUSED = {
    "central_server.endpoints.log_management":
        "`from logging.handlers import FileHandler` が ImportError（全バージョン共通）。未参照の死んだモジュール",
}


def _module_names():
    names = []
    for pkg_name in PACKAGES:
        pkg = importlib.import_module(pkg_name)
        for m in pkgutil.iter_modules(pkg.__path__):
            names.append(f"{pkg_name}.{m.name}")
    return sorted(names)


def test_module_list_is_not_empty():
    names = _module_names()
    assert len(names) >= 20, names  # 一覧の取得が壊れて 0 件になっても、試験が素通りしないようにする


def _param(name):
    if name in KNOWN_BROKEN_UNUSED:
        return pytest.param(name, marks=pytest.mark.xfail(
            raises=ImportError, strict=True, reason=KNOWN_BROKEN_UNUSED[name]))
    return name


@pytest.mark.parametrize("module_name", [_param(n) for n in _module_names()])
def test_endpoint_module_imports_on_this_python(module_name):
    importlib.import_module(module_name)


def test_known_broken_modules_still_exist():
    # 一覧の項目が消えたモジュールを指したまま残らないようにする
    assert set(KNOWN_BROKEN_UNUSED) <= set(_module_names())


def test_known_broken_modules_are_unreferenced():
    # 「未参照」が前提。どこかから使われ始めたら、例外ではなく修正の対象にする
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[1]
    for name in KNOWN_BROKEN_UNUSED:
        short = name.rsplit(".", 1)[-1]
        for f in root.rglob("*.py"):
            if ".venv" in f.parts or "tests" in f.parts or f.stem == short:
                continue
            assert f"endpoints.{short}" not in f.read_text(errors="ignore") and f"endpoints import {short}" not in f.read_text(errors="ignore"), f"{short} が {f} から参照されている"
