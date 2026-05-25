import requests

BASE = "http://127.0.0.1:8000"
TOKEN = "secret-token"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}

def test_send_metrics():
    payload = {
        "roundNumber": 1,
        "terminalId": "device-001",
        "epoch": 1,
        "batchSize": 32,
        "learningRate": 0.001,
        "trainingLoss": 0.5,
        "validationLoss": 0.6,
        "trainingAccuracy": 0.7,
        "validationAccuracy": 0.65,
        "uploadSize": 1234,
        "downloadSize": 4321,
        "communicationTime": 1000,
        "cpuUsage": 10.5,
        "memoryUsage": 128.0,
        "batteryUsage": 1.2,
        "datasetSize": 1000,
        "timestamp": 1730793600000,
        "communicationErrors": 0,
        "trainingErrors": 0,
        "dataDistribution": {"A": 500, "B": 500},
        "preprocessingTime": 100
    }
    r = requests.post(BASE+"/api/training-data", json=payload, headers=HEADERS)
    print(r.status_code, r.text)

if __name__ == '__main__':
    test_send_metrics()

