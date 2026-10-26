# edge_server/utils/validation.py

from fastapi import UploadFile


def validate_weight_file(filename: str) -> bool:
    """
    学習済み重みファイルの拡張子が .pt または .pth であることを確認。
    """
    return filename.endswith(('.pt', '.pth'))


def validate_upload_file_type(file: UploadFile, allowed_extensions: tuple) -> bool:
    """
    アップロードされたファイルの拡張子が許可されたものか確認。
    """
    return file.filename.endswith(allowed_extensions)


def validate_json_content(content: bytes) -> bool:
    """
    JSONの形式としてパースできるかを確認。
    """
    import json
    try:
        json.loads(content.decode("utf-8"))
        return True
    except Exception:
        return False
