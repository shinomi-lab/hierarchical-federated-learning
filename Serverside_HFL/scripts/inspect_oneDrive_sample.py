from pathlib import Path

files = [
    Path('tools/profiler_report.txt'),
    Path('logs/organized/20251120_0233/edge_server_edge-server-01_20251120_023301.log'),
    Path('received_files/terminal_logs/com.example.hfl_experiment/index.jsonl'),
    Path('scripts/setup_marker_local.ps1'),
    Path('.venv\Scripts\activate'),
    Path('.venv\Scripts\activate.bat'),
]

for p in files:
    if not p.exists():
        print('Missing:', p)
        continue
    try:
        s = p.read_text(encoding='utf-8')
    except Exception:
        s = p.read_text(encoding='cp932')
    i = s.find('OneDrive')
    print('File:', p)
    if i == -1:
        print('  No OneDrive found')
        continue
    start = max(0, i-40)
    end = min(len(s), i+120)
    snippet = s[start:end]
    print('  index', i)
    print('  snippet repr:', repr(snippet))
