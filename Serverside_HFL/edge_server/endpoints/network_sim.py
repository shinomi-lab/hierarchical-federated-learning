from fastapi import APIRouter, Request
from typing import Optional
from starlette.responses import JSONResponse
from .. import config

router = APIRouter(prefix="/api/v1")

# Simple Murata-inspired network probe simulator
# - ap_id: select base station parameters (mu, TP_init)
# - uses current connected client count (app.client_states) as load

# per-AP service rates (mu) and initial throughput (Mbps)
MU_MAP = {"1": 50.0, "2": 50.0, "3": 20.0}
TP_INIT_MAP = {"1": 100.0, "2": 100.0, "3": 50.0}


@router.get("/network_probe/{terminal_id}")
async def network_probe(request: Request, terminal_id: str, ap_id: Optional[str] = "1", sim_users: Optional[int] = None):
    """Return simulated RTT (ms) and TP (Mbps) based on a simple Murata-like model.

    Query params:
    - ap_id: base station id ("1","2","3") to pick mu/TP base values.
    - sim_users: optional override for number of connected users (for testing).
    """
    app = request.app

    # determine current connected users; fall back to 0
    try:
        current_users = len(app.client_states)
    except Exception:
        current_users = 0

    if sim_users is not None:
        try:
            current_users = int(sim_users)
        except Exception:
            pass

    capacity = getattr(config, "MAX_CONCURRENT_CLIENTS", 100)

    # select mu and TP_init for ap_id
    mu = MU_MAP.get(str(ap_id), MU_MAP["1"])
    tp_init_mbps = TP_INIT_MAP.get(str(ap_id), TP_INIT_MAP["1"])

    # arrival rate lambda proportional to connected users
    # (units chosen compatible with mu)
    lambda_rate = float(current_users)

    # utilization rho (clipped to avoid rho >= 1 pathological results)
    rho = min(0.999, float(current_users) / float(max(1, capacity)))

    # If overloaded (mu <= lambda) produce very large RTT
    if lambda_rate >= mu:
        rtt_ms = 10000.0
    else:
        # RTT_link = 1 / (mu - lambda)  (seconds) -> convert to ms
        rtt_seconds = 1.0 / (mu - lambda_rate)
        rtt_ms = rtt_seconds * 1000.0

    # Simple packet-loss model growing with utilization. This is a pragmatic
    # mapping approximating section behavior: no loss for low rho, then
    # linear increase above 0.5 up to near-1.
    if rho <= 0.5:
        p_loss = 0.0
    else:
        p_loss = min(0.99, (rho - 0.5) / 0.5)

    tp_actual_mbps = tp_init_mbps * (1.0 - p_loss)

    body = {
        "terminal_id": terminal_id,
        "ap_id": ap_id,
        "current_users": current_users,
        "capacity": capacity,
        "mu": mu,
        "lambda": lambda_rate,
        "rho": round(rho, 4),
        "rtt_ms": round(rtt_ms, 2),
        "tp_init_mbps": tp_init_mbps,
        "tp_actual_mbps": round(tp_actual_mbps, 3),
        "packet_loss": round(p_loss, 4),
        "note": "Simulated values (Murata-inspired). If mu<=lambda, RTT is saturated."
    }

    return JSONResponse(status_code=200, content=body)
