import requests, hashlib, sys, json
BASE='http://127.0.0.1:8001'
TS='20251218_013000'

print('1) POST /admin/set_package')
r = requests.post(f'{BASE}/admin/set_package', json={'ts':TS}, timeout=5)
print(r.status_code)
print(r.text)
if r.status_code == 422:
    print('\nadmin_set_package returned 422, retrying with raw text body (compat)')
    r = requests.post(f'{BASE}/admin/set_package', data=TS, headers={'Content-Type':'text/plain'}, timeout=5)
    print(r.status_code)
    print(r.text)

print('\n2) GET /send_to_device')
r2 = requests.get(f'{BASE}/send_to_device', timeout=5)
print(r2.status_code)
try:
    j=r2.json()
except Exception:
    print('non-json resp:', r2.text)
    sys.exit(1)
print(json.dumps(j, indent=2, ensure_ascii=False))

model_bin_link = None
try:
    model_bin_link = j.get('links', {}).get('model_bin')
except Exception:
    model_bin_link = None

if model_bin_link:
    print('\n3) Download model_bin')
    # rel_path is the query value after rel_path=
    import urllib.parse as _up
    parsed = _up.urlparse(model_bin_link)
    qs = _up.parse_qs(parsed.query)
    rel = qs.get('rel_path', [None])[0]
    print('rel_path=', rel)
    dl = requests.get(f'{BASE}/download', params={'rel_path': rel}, stream=True, timeout=10)
    print('download status', dl.status_code)
    data = dl.content
    print('download len', len(data))
    print('sha256', hashlib.sha256(data).hexdigest())
else:
    print('no model_bin link advertised')

print('\n4) POST /edge/infer')
inputs = [ [1.0,2.0,3.0,4.0,5.0,6.0], [10.0,0.0,1.0,0.0,0.0,0.0] ]
pr = requests.post(f'{BASE}/edge/infer', json={'input': inputs}, timeout=10)
print(pr.status_code)
try:
    print(json.dumps(pr.json(), indent=2, ensure_ascii=False))
except Exception:
    print('non-json:', pr.text)

print('\nE2E test complete')
