from fastapi import APIRouter, Request
from typing import Optional
from starlette.responses import JSONResponse
from .. import config

router = APIRouter(prefix="/api/v1")

# Router parameters tuned for small-scale experiments (4 terminals sensitive)
# Router A (fast but easy to congest)
ROUTER_PARAMS = {
    "A": {"initial_rtt_ms": 20.0, "tp_init_mbps": 50.0, "mu": 2.0, "n": 2},
    "B": {"initial_rtt_ms": 80.0, "tp_init_mbps": 20.0, "mu": 5.0, "n": 10},
}


def erlang_b(n: int, A: float) -> float:
    """Compute Erlang B blocking probability B(n, A) using stable recurrence.

    n: number of channels
    A: offered load (Erlangs)
    Returns blocking probability in [0,1].
    """
    if n <= 0:
        return 1.0
    if A <= 0.0:
        return 0.0
    B = 1.0
    for k in range(1, n + 1):
        B = (A * B) / (k + A * B)
    return float(B)


@router.get("/virtual_congestion/{terminal_id}")
async def virtual_congestion(request: Request, terminal_id: str, ap: Optional[str] = "A", measured_ping_ms: Optional[float] = None, sim_users: Optional[int] = None):
    """Return simulated RTT and TP using Murata-like formulas with hybrid physical+model approach.

    Query params:
    - `ap`: "A" or "B" (router selection)
    - `measured_ping_ms`: optional physical ping measured by the terminal (ms); if provided, we add computed congestion delta to it
    - `sim_users`: optional override for number of connected users at that AP (for testing)
    """

    app = request.app

    params = ROUTER_PARAMS.get(str(ap).upper(), ROUTER_PARAMS["A"])
    initial_rtt_ms = float(params["initial_rtt_ms"])
    tp_init = float(params["tp_init_mbps"])
    mu = float(params["mu"])
    n = int(params["n"])

    # Determine current connected users for that AP. The app may keep per-AP counts
    # in app.client_states as a mapping; fall back to total count if per-AP not tracked.
    try:
        # expect app.client_states to be dict mapping client_id -> {"ap": "A"}
        ap_count = 0
        for v in getattr(app, "client_states", {}).values():
            try:
                if isinstance(v, dict) and v.get("ap") == ap:
                    ap_count += 1
            except Exception:
                continue
        current_users = ap_count
    except Exception:
        current_users = 0

    if sim_users is not None:
        try:
            current_users = int(sim_users)
        except Exception:
            pass

    # Lambda per the user's spec: use number of connected terminals
    lambda_conn = float(current_users)

    # Compute RTT_link using M/M/1 model: RTT_link = 1 / (mu - lambda)
    if lambda_conn >= mu:
        virtual_rtt_ms = 10000.0
    else:
        virtual_rtt_ms = (1.0 / (mu - lambda_conn)) * 1000.0

    # Hybrid: use measured physical ping as baseline, add the model-computed increase
    # delta = max(0, virtual_rtt_ms - initial_rtt_ms)
    delta = max(0.0, virtual_rtt_ms - initial_rtt_ms)
    if measured_ping_ms is not None:
        try:
            baseline = float(measured_ping_ms)
        except Exception:
            baseline = initial_rtt_ms
    else:
        baseline = initial_rtt_ms

    rtt_report_ms = min(10000.0, baseline + delta)

    # Compute TP via Erlang-B: offered load A = lambda * mean_service_time (1/mu)
    # A = lambda / mu
    if mu <= 0.0:
        offered_A = lambda_conn
    else:
        offered_A = lambda_conn / mu

    p_loss = erlang_b(n, offered_A)
    tp_actual_mbps = max(0.0, tp_init * (1.0 - p_loss))

    body = {
        "terminal_id": terminal_id,
        "ap": ap,
        "current_users": current_users,
        "mu": mu,
        "n": n,
        "offered_load_A": round(offered_A, 4),
        "virtual_rtt_ms": round(virtual_rtt_ms, 2),
        "measured_ping_ms": None if measured_ping_ms is None else round(float(measured_ping_ms), 2),
        "rtt_report_ms": round(rtt_report_ms, 2),
        "tp_init_mbps": tp_init,
        "tp_actual_mbps": round(tp_actual_mbps, 3),
        "packet_loss": round(float(p_loss), 6),
        "note": "Hybrid: physical ping + model delta. If lambda>=mu virtual_rtt saturates at 10000ms."
    }

    return JSONResponse(status_code=200, content=body)
