import requests, json, numpy as np
url = 'http://127.0.0.1:8001/edge/infer'
# prepare three sample inputs (each length 6 for this model)
inputs = [
    [1.0, 2.0, 3.0, 4.0, 5.0, 6.0],
    [10.0, 0.0, 1.0, 0.0, 0.0, 0.0],
    [0.0, 100.0, 3.0, 2.0, 0.0, 0.0],
]
payload = {"input": inputs}
print('POST', url)
print('payload:', inputs)
try:
    r = requests.post(url, json=payload, timeout=10)
    r.raise_for_status()
    j = r.json()
except Exception as e:
    print('REQUEST FAILED:', e)
    try:
        print(r.text)
    except Exception:
        pass
    raise

results = j.get('result')
lat = j.get('latency_ms')
print('latency_ms:', lat)
print('raw outputs:')
for i, out in enumerate(results):
    arr = np.array(out)
    arg = int(arr.argmax())
    probs = None
    try:
        exps = np.exp(arr - arr.max())
        probs = (exps / exps.sum()).tolist()
    except Exception:
        probs = None
    print(f' input[{i}] -> output={out}  argmax={arg}  probs={probs}')
