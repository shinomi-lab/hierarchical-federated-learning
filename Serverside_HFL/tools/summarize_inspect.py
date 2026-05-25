import json
from pathlib import Path
j = json.loads(Path('received_files').parent.joinpath('tools','inspect_models_and_data.py').read_text()) if False else None
# Instead, just re-run the inspect script and filter
import subprocess, json
p = subprocess.run(['C:/Users/tetsu/Serverside/.venv/Scripts/python.exe','tools/inspect_models_and_data.py'], capture_output=True, text=True)
if p.returncode!=0:
    print('ERROR', p.stderr)
    raise SystemExit(1)
out = json.loads(p.stdout)
for pkg in out['packages']:
    if pkg.get('state_dict') or pkg.get('torchscript') or pkg.get('training_data') or pkg.get('meta') or pkg.get('load_error'):
        print('PACKAGE', pkg['ts'])
        if pkg.get('state_dict'):
            print('  state_dict keys count:', len(pkg['state_dict']))
            if pkg.get('output_layer'):
                print('  output_layer:', pkg['output_layer'])
        if pkg.get('torchscript'):
            print('  torchscript true; example_output_shape=', pkg.get('example_output_shape'), 'error=', pkg.get('example_output_error'))
        if pkg.get('meta'):
            print('  meta exists')
        if pkg.get('training_data'):
            print('  training_data:', pkg.get('training_data'))
            print('   header:', pkg.get('training_header'))
            print('   label_candidates_by_name:', pkg.get('label_candidates_by_name'))
            if pkg.get('label_sample_values'):
                print('   label_sample_values:', pkg.get('label_sample_values'))
        if pkg.get('load_error'):
            print('  load_error:', pkg.get('load_error'))
        print('')
