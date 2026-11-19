"""Compare consecutive RGB frames, not just matching frame indices."""
import json,sys
from pathlib import Path
import numpy as np
root=Path(sys.argv[1]);rows=[]
for case in sorted(root.glob('*/seed-*/case.json')):
 folder=case.parent;row={'benchmark':folder.parent.name,'seed':json.loads(case.read_text())['seed'],'steps':[]}
 try:
  reports=[json.loads((folder/s/'report.json').read_text()) for s in ('upstream','robotenon')]
  names=reports[0]['observation']
  for i in range(1,len(names)):
   with np.load(folder/'upstream'/(names[i-1]+'.npz')) as ap,np.load(folder/'upstream'/(names[i]+'.npz')) as an,np.load(folder/'robotenon'/(names[i-1]+'.npz')) as bp,np.load(folder/'robotenon'/(names[i]+'.npz')) as bn:
    images={}
    for k in an.files:
     if an[k].ndim!=3 or an[k].dtype!=np.uint8:continue
     da=an[k].astype(np.int16)-ap[k].astype(np.int16);db=bn[k].astype(np.int16)-bp[k].astype(np.int16)
     images[k]={'upstream_changed_channels':int(np.count_nonzero(da)),'robotenon_changed_channels':int(np.count_nonzero(db)),'delta_equal':bool(np.array_equal(da,db)),'delta_max_abs':int(np.max(np.abs(da-db))),'upstream_frame_range':[int(an[k].min()),int(an[k].max())],'robotenon_frame_range':[int(bn[k].min()),int(bn[k].max())]}
    row['steps'].append({'action_index':i-1,'images':images})
  row['all_step_deltas_equal']=all(x['delta_equal'] for s in row['steps'] for x in s['images'].values())
  row['each_step_has_visual_change']=bool(row['steps']) and all(any(x['upstream_changed_channels'] and x['robotenon_changed_channels'] for x in s['images'].values()) for s in row['steps'])
 except Exception as e:row['error']=str(e)
 rows.append(row)
(root/'image-changes.json').write_text(json.dumps(rows,indent=2))
print([(r['benchmark'],r['seed'],r.get('all_step_deltas_equal'),r.get('each_step_has_visual_change'),r.get('error')) for r in rows])
