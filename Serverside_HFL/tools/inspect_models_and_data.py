import torch
import json
from pathlib import Path
import csv

root = Path('received_files')
out = {'packages': []}
if not root.exists():
    print(json.dumps({'error':'no received_files dir'}))
    raise SystemExit(0)

for d in sorted([p for p in root.iterdir() if p.is_dir()], reverse=True):
    pkg = {'ts': d.name, 'model': None, 'meta': None, 'training_data': None}
    pt = d / 'global_model_mobile.pt'
    if pt.exists():
        pkg['model'] = str(pt)
        try:
            obj = torch.load(str(pt), map_location='cpu')
            if isinstance(obj, dict):
                keys = sorted(obj.keys())
                shapes = {k: tuple(v.shape) for k, v in obj.items()}
                pkg['state_dict'] = shapes
                # find likely output layer
                for cand in ['layer3.weight','layer2.weight','layer1.weight','fc.weight']:
                    if cand in obj:
                        pkg['output_layer'] = {'name': cand, 'shape': tuple(obj[cand].shape)}
                        break
            else:
                pkg['torchscript'] = True
                # try run example
                try:
                    import torch as _t
                    x = _t.randn(1,6)
                    m = obj
                    outv = m(x)
                    pkg['example_output_shape'] = tuple(outv.detach().cpu().shape)
                except Exception as e:
                    pkg['example_output_error'] = str(e)
        except Exception as e:
            pkg['load_error'] = str(e)
    # meta.json
    for meta_name in ['meta.json', f'{pt.stem}.meta.json', str(pt.name)+'.meta.json']:
        mfp = d / meta_name
        if mfp.exists():
            try:
                pkg['meta'] = json.loads(mfp.read_text())
            except Exception:
                pkg['meta'] = 'unreadable'
            break
    # training data
    td = d / 'latest_data.csv'
    if td.exists():
        pkg['training_data'] = str(td)
        # inspect header and first 50 labels if present
        try:
            with td.open('r', encoding='utf-8', errors='ignore') as f:
                rdr = csv.reader(f)
                rows = []
                for i,row in enumerate(rdr):
                    if i==0:
                        header = row
                    else:
                        rows.append(row)
                    if i>50:
                        break
            pkg['training_header'] = header
            # try detect label column by name
            label_candidates = [h for h in header if 'label' in h.lower() or 'target' in h.lower() or 'y'==h.lower()]
            pkg['label_candidates_by_name'] = label_candidates
            if rows and label_candidates:
                idx = header.index(label_candidates[0])
                vals = list({r[idx] for r in rows if len(r)>idx})
                pkg['label_sample_values'] = vals[:10]
        except Exception as e:
            pkg['training_inspect_error'] = str(e)

    out['packages'].append(pkg)

print(json.dumps(out, indent=2))
