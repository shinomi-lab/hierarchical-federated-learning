import requests
import sys

ts = '20251218_005125'
try:
    r = requests.post('http://127.0.0.1:8001/admin/set_package', json={'ts': ts}, timeout=5)
    print(r.status_code)
    print(r.text)
except Exception as e:
    print('ERROR', e)
    sys.exit(1)
