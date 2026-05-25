# test_agg_run.py
import asyncio
from edge_server.endpoints.aggregation import aggregate_and_send_to_central_server

if __name__ == "__main__":
    # edge_id と round_id はテスト用に適切な値を入れてください
    edge_id = "edge-test"
    round_id = 1
    # auto_send False, interactive False で保存まで動かす
    asyncio.run(aggregate_and_send_to_central_server(edge_id, round_id, auto_send=False, interactive=False))
    print("done")
