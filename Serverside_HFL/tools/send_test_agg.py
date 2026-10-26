import torch
import requests

# create a tiny state_dict
sd = {"w": torch.randn(2,2)}
file_path = "tmp_test_agg.pt"
torch.save(sd, file_path)

# テスト用のURL（必要に応じて環境に合わせて変更してください）
url = "http://localhost:8000/edge_update"
files = {"weights": open(file_path, "rb")}
# required form fields
data = {
    "edge_id": "test-edge-1",
    "round": "1",
    "num_clients": "1",
    "sum_n_samples": "10",
}
print('Posting to', url)
resp = requests.post(url, data=data, files={"weights": (file_path, open(file_path, 'rb'), 'application/octet-stream')})
print('status', resp.status_code)
try:
    print(resp.json())
except Exception:
    print(resp.text)
