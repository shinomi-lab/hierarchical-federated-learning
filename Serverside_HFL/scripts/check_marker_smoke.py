"""Simple smoke test for marker backend.

Usage: python scripts/check_marker_smoke.py
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from central_server.utils import marker_backend
import uuid

def main():
    test_sha = 'sha256:test-' + uuid.uuid4().hex
    print('Using marker backend. Redis connected?' , marker_backend._redis_client is not None if hasattr(marker_backend, '_redis_client') else False)
    print('Add received hash ->', marker_backend.add_received_hash(test_sha))
    print('Has received hash ->', marker_backend.has_received_hash(test_sha))

    r = 9999
    eid = 'smoke-edge'
    s = 'sha256:' + uuid.uuid4().hex
    print('Set edge round sha ->', marker_backend.set_edge_round_sha(r, eid, s))
    print('Get edge round sha ->', marker_backend.get_edge_round_sha(r, eid))

if __name__ == '__main__':
    main()
