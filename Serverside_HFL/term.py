from typing import List
import httpx
import numpy as np
import json

class Term:
    # クラスをインスタンス化して使えるように __init__ を定義
    def __init__(self):
        self.id: int = 0 # identifier of the term
        self.apBssid: int = 0 # access point BSSID
        self.lines: List = [] # list of lines
        self.app: object = None # application name
        self.appNum: int = 0 # application number


if __name__ == "__main__":
    
    # Termクラスをインスタンス化
    each_term = Term()
    # インスタンスの属性として値を設定
    each_term.appNum = np.random.randint(0,4)
    
    edge_server_base_url = "http://127.0.0.1:8001"
    
    try:
        # 1. エッジサーバーからダウンロード情報を取得
        info_url = f"{edge_server_base_url}/send_to_device"
        print(f"ダウンロード情報を取得中: {info_url}")
        with httpx.Client() as client:
            info_response = client.get(info_url, timeout=10)
            info_response.raise_for_status()  # ステータスコードが200番台でなければ例外を発生

            paths = info_response.json()
            model_relative_path = paths.get("model_path")
            data_relative_path = paths.get("data_path")

            # 2. 取得したパスを使い、モデル本体をダウンロード
            if model_relative_path:
                model_download_url = f"{edge_server_base_url}{model_relative_path}"
                print(f"モデルをダウンロード中: {model_download_url}")
                model_response = client.get(model_download_url, timeout=30)
                model_response.raise_for_status()

                with open("downloaded_model_mobile.pt", "wb") as f:
                    f.write(model_response.content)
                print("モデルのダウンロードに成功しました！")

            # 3. 取得したパスを使い、教師データをダウンロード
            if data_relative_path:
                data_download_url = f"{edge_server_base_url}{data_relative_path}"
                print(f"教師データをダウンロード中: {data_download_url}")
                data_response = client.get(data_download_url, timeout=60)
                data_response.raise_for_status()

                with open("downloaded_training_data.csv", "wb") as f:
                    f.write(data_response.content)
                print("教師データのダウンロードに成功しました！")

    except httpx.RequestError as e:
        print(f"ファイルのダウンロードに失敗しました: {e}")

    with open('sim.json', 'a') as f:
        f.write(f"{each_term.appNum}\n")