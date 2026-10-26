from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import uvicorn
from typing import Optional
import connection_index as ci

app = FastAPI()


class RegisterPayload(BaseModel):
    device_id: str
    edge_id: str
    endpoint: str
    model_version: Optional[str] = None


@app.on_event("startup")
def startup():
    ci.init_db()


@app.post("/register_connection")
def register(p: RegisterPayload):
    ci.register(p.device_id, p.edge_id, p.endpoint, p.model_version)
    return {"ok": True}


@app.post("/heartbeat")
def heartbeat(device_id: str):
    ci.heartbeat(device_id)
    return {"ok": True}


@app.post("/deregister")
def deregister(device_id: str):
    ci.deregister(device_id)
    return {"ok": True}


@app.get("/whereis/{device_id}")
def whereis(device_id: str):
    info = ci.whereis(device_id)
    if not info:
        raise HTTPException(status_code=404, detail="見つかりません")
    return info


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=9002)
