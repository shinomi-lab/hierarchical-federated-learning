# central_server/utils/registry.py

from central_server.state import edge_registry


def register_edge_url(edge_url: str) -> None:
    """エッジサーバの URL をセットに追加"""
    edge_registry.add(edge_url)


def get_all_edge_urls() -> list[str]:
    """登録済みの全エッジURLを取得"""
    return list(edge_registry)


def is_registered(edge_url: str) -> bool:
    """URLがすでに登録済みか確認"""
    return edge_url in edge_registry
