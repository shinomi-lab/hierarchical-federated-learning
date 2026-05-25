import csv
from pathlib import Path
p = Path('received_files/20251105_130035/latest_data.csv')
if not p.exists():
    print('file not found', p)
else:
    cols = []
    with p.open('r', encoding='utf-8', errors='ignore') as f:
        rdr = csv.reader(f)
        for i,row in enumerate(rdr):
            if i==0:
                ncols = len(row)
                cols = [set() for _ in range(ncols)]
            for j,val in enumerate(row):
                if val!='':
                    cols[j].add(val)
            if i>2000:
                break
    for idx,s in enumerate(cols):
        print(idx, 'unique_count', len(s), 'sample', list(s)[:10])
