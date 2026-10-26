# edge_server/logical_client_env_export.py
"""論理クライアント（Docker）向けに EDGE_URL をファイルへ書き出す（任意）。"""
from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlparse, urlunparse


def _repo_root() -> Path:
    # .../HFL/Serverside_HFL/edge_server/this_file.py → parents[2] = HFL
    return Path(__file__).resolve().parents[2]


def _with_port(url: str, port: int) -> str:
    p = urlparse(url)
    host = p.hostname or "127.0.0.1"
    return urlunparse((p.scheme or "http", f"{host}:{port}", "", "", "", ""))


def maybe_export_logical_client_env() -> None:
    """
    HFL_EXPORT_LOGICAL_CLIENT_ENV=1 のとき、edge_server.config.EDGE_URL を元に
    docker/generated-logical-client.env を生成する。

    Docker 側は例:
      docker compose --env-file docker/generated-logical-client.env \\
        -f docker/docker-compose.logical-clients.yml up --build
    """
    if os.getenv("HFL_EXPORT_LOGICAL_CLIENT_ENV", "").strip() != "1":
        return

    from edge_server.config import EDGE_URL, EDGE_SERVER_PORT

    out = Path(os.getenv("HFL_LOGICAL_CLIENT_ENV_OUT", "")).expanduser()
    if not str(out):
        out = _repo_root() / "docker" / "generated-logical-client.env"

    port_main = urlparse(EDGE_URL).port or int(EDGE_SERVER_PORT)
    # 2 台目エッジはホスト実験では +1 ポート（8001→8002）と仮定
    port_alt = int(os.getenv("HFL_LOGICAL_CLIENT_SECOND_EDGE_PORT", str(port_main + 1)))

    lines = [
        "# 自動生成（コミットしない）— edge_server 起動時に HFL_EXPORT_LOGICAL_CLIENT_ENV=1 で出力",
        f"HFL_EDGE_URL_TERMINAL_00={EDGE_URL}",
        f"HFL_EDGE_URL_TERMINAL_01={EDGE_URL}",
        f"HFL_EDGE_URL_TERMINAL_02={_with_port(EDGE_URL, port_alt)}",
        "",
    ]
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")
