"""
運用向け: 登録エッジごとの端末の見え方（許可リスト・レジャー・メモリ上の更新）を一覧する。

- GET /ops/edge-terminal-topology … HTML（Ctrl+L でオーバーレイを開閉＋再取得）
- GET /ops/edge-terminal-topology/data … 集約 JSON（スクリプト・監視用）
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
from typing import Any, Dict, List

import httpx
from fastapi import APIRouter
from fastapi.responses import HTMLResponse, JSONResponse

from central_server.state import edge_registry, global_model_state

logger = logging.getLogger("central_server.ops_topology")

router = APIRouter(prefix="/ops", tags=["ops"])


async def _fetch_one_edge(edge_url: str) -> Dict[str, Any]:
    base = edge_url.rstrip("/")
    url = f"{base}/admin/topology_snapshot"
    try:
        async with httpx.AsyncClient(timeout=4.0) as client:
            r = await client.get(url)
            if r.status_code == 200:
                try:
                    data = r.json()
                except Exception:
                    data = {"_parse_error": True, "raw": r.text[:500]}
                return {"edge_url": edge_url, "ok": True, "data": data}
            return {
                "edge_url": edge_url,
                "ok": False,
                "error": f"HTTP {r.status_code}",
                "detail": r.text[:500],
            }
    except Exception as e:
        logger.warning("topology fetch failed for %s: %s", edge_url, e)
        return {"edge_url": edge_url, "ok": False, "error": str(e)}


async def build_topology_payload() -> Dict[str, Any]:
    edges_sorted = sorted(edge_registry)
    tasks = [_fetch_one_edge(u) for u in edges_sorted]
    edge_results: List[Dict[str, Any]] = list(await asyncio.gather(*tasks)) if tasks else []
    return {
        "central": {
            "registered_edge_urls": edges_sorted,
            "global_model_round": global_model_state.get("round"),
        },
        "edges": edge_results,
    }


@router.get("/edge-terminal-topology/data")
async def edge_terminal_topology_data():
    payload = await build_topology_payload()
    return JSONResponse(content=payload)


@router.get("/edge-terminal-topology", response_class=HTMLResponse)
async def edge_terminal_topology_page():
    """ブラウザで開き、Ctrl+L で表オーバーレイを表示（同一ページ内フォーカス時）。"""
    payload = await build_topology_payload()
    json_text = json.dumps(payload, ensure_ascii=False, indent=2)
    safe_json = html.escape(json_text)

    # インライン HTML（外部静的ファイル依存なし）
    body = f"""<!DOCTYPE html>
<html lang="ja">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>HFL エッジ–端末トポロジ</title>
  <style>
    body {{ font-family: system-ui, sans-serif; margin: 1rem; background: #111; color: #e8e8e8; }}
    h1 {{ font-size: 1.1rem; }}
    p.hint {{ color: #9ab; font-size: 0.85rem; max-width: 48rem; }}
    kbd {{ background: #333; padding: 0.1rem 0.35rem; border-radius: 4px; }}
    #overlay {{
      display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.88);
      z-index: 1000; padding: 1rem; overflow: auto;
    }}
    #overlay.visible {{ display: block; }}
    table {{ border-collapse: collapse; width: 100%; max-width: 72rem; font-size: 0.8rem; }}
    th, td {{ border: 1px solid #444; padding: 0.35rem 0.5rem; text-align: left; vertical-align: top; }}
    th {{ background: #252525; }}
    tr:nth-child(even) {{ background: #1a1a1a; }}
    .ok {{ color: #8d8; }}
    .err {{ color: #e88; }}
    button {{ margin-top: 0.5rem; padding: 0.4rem 0.8rem; cursor: pointer; }}
    pre.raw {{ font-size: 0.7rem; max-height: 12rem; overflow: auto; background: #0a0a0a; padding: 0.5rem; }}
  </style>
</head>
<body>
  <h1>エッジ–端末トポロジ（中央集約ビュー）</h1>
  <p class="hint">
    このページにフォーカスがある状態で <kbd>Ctrl</kbd>+<kbd>L</kbd> を押すと、下の JSON に加えて
    <strong>表形式のオーバーレイ</strong>が開きます（再押下で閉じる）。ブラウザによってはアドレスバー移動が優先される場合があるため、そのときは画面内を一度クリックしてから試してください。
    生データ: <a href="/ops/edge-terminal-topology/data" style="color:#8cf">/ops/edge-terminal-topology/data</a>
  </p>
  <button type="button" id="btnToggle">オーバーレイを開く / 閉じる（Ctrl+L 相当）</button>
  <pre class="raw" id="rawJson">{safe_json}</pre>

  <div id="overlay" aria-hidden="true">
    <div style="max-width:90rem;margin:0 auto;">
      <h2 style="margin-top:0">接続トポロジ（スナップショット）</h2>
      <p style="color:#9ab;font-size:0.85rem">中央の登録エッジから <code>/admin/topology_snapshot</code> を取得した結果です。</p>
      <div id="tables"></div>
      <button type="button" id="btnClose">閉じる (Esc)</button>
    </div>
  </div>

  <script>
const overlay = document.getElementById('overlay');
const tables = document.getElementById('tables');
const rawJson = document.getElementById('rawJson');

function esc(s) {{
  if (s === null || s === undefined) return '';
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}}

function renderTables(payload) {{
  const c = payload.central || {{}};
  let html = '<p><strong>中央</strong> global_round=' + esc(c.global_model_round)
    + ' / 登録エッジ数=' + (c.registered_edge_urls || []).length + '</p>';
  html += '<table><thead><tr><th>edge_url</th><th>状態</th><th>edge_id</th><th>allow</th><th>集約閾値</th><th>エッジround</th><th>端末（レジャー）</th><th>メモリ上の更新（抜粋）</th></tr></thead><tbody>';
  for (const row of (payload.edges || [])) {{
    if (!row.ok) {{
      html += '<tr><td>' + esc(row.edge_url) + '</td><td class="err" colspan="7">' + esc(row.error || 'error') + '</td></tr>';
      continue;
    }}
    const d = row.data || {{}};
    const allow = (d.allow_mode === 'all') ? '全端末' : ('制限: ' + (d.allowlist_terminal_ids || []).join(', '));
    const led = d.terminal_ledgers || {{}};
    const ledgerLines = Object.keys(led).sort().map(tid => {{
      const o = led[tid] || {{}};
      return esc(tid) + ' (last_seen=' + esc(o.last_seen) + ', run_id=' + esc(o.run_id) + ')';
    }}).join('<br/>') || '—';
    const upd = (d.updates_in_memory_by_round || []).slice(0, 24).map(u =>
      esc(u.round_id) + '/' + esc(u.terminal_id) + ' n=' + esc(u.n_samples)
    ).join('<br/>') || '—';
    html += '<tr><td>' + esc(row.edge_url) + '</td><td class="ok">OK</td><td>' + esc(d.edge_id)
      + '</td><td>' + allow + '</td><td>' + esc(d.aggregation_threshold) + '</td><td>' + esc(d.current_round)
      + '</td><td>' + ledgerLines + '</td><td>' + upd + ( (d.updates_in_memory_by_round||[]).length > 24 ? '<br/>…' : '') + '</td></tr>';
  }}
  html += '</tbody></table>';
  tables.innerHTML = html;
}}

async function refresh() {{
  const r = await fetch('/ops/edge-terminal-topology/data', {{ cache: 'no-store' }});
  const payload = await r.json();
  rawJson.textContent = JSON.stringify(payload, null, 2);
  renderTables(payload);
}}

function toggleOverlay() {{
  overlay.classList.toggle('visible');
  overlay.setAttribute('aria-hidden', overlay.classList.contains('visible') ? 'false' : 'true');
  if (overlay.classList.contains('visible')) refresh();
}}

document.getElementById('btnToggle').addEventListener('click', toggleOverlay);
document.getElementById('btnClose').addEventListener('click', () => {{
  overlay.classList.remove('visible');
  overlay.setAttribute('aria-hidden', 'true');
}});

window.addEventListener('keydown', (e) => {{
  if (e.ctrlKey && (e.key === 'l' || e.key === 'L')) {{
    e.preventDefault();
    toggleOverlay();
  }}
  if (e.key === 'Escape') {{
    overlay.classList.remove('visible');
    overlay.setAttribute('aria-hidden', 'true');
  }}
}}, true);

fetch('/ops/edge-terminal-topology/data', {{ cache: 'no-store' }}).then(r => r.json()).then(p => {{
  rawJson.textContent = JSON.stringify(p, null, 2);
  renderTables(p);
}});
  </script>
</body>
</html>"""
    return HTMLResponse(content=body)