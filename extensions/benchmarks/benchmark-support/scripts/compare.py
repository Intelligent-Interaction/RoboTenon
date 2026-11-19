import json,sys
from pathlib import Path
import numpy as np
ROOT=Path(sys.argv[1] if len(sys.argv)>1 else 'outputs/action-parity');results=[]
for case in sorted(ROOT.glob('*/seed-*/case.json')):
 folder=case.parent;row={'benchmark':folder.parent.name,'seed':json.loads(case.read_text())['seed'],'status':'unverified','comparisons':[]}
 try:
  a=json.loads((folder/'upstream/report.json').read_text());b=json.loads((folder/'robotenon/report.json').read_text())
  if a['status']!='passed' or b['status']!='passed':row['errors']={'upstream':a.get('error',a['status']),'robotenon':b.get('error',b['status'])};results.append(row);continue
  exact=True
  for kind in ['task','reset_state','observation','action','step','policy_input']:
   if a.get(kind,[])!=b.get(kind,[]):exact=False;row.setdefault('call_count_differences',{})[kind]=[len(a.get(kind,[])),len(b.get(kind,[]))]
   for stem in a.get(kind,[]):
    ap=folder/'upstream'/stem;bp=folder/'robotenon'/stem
    if not bp.with_suffix('.json').exists():continue
    same_meta=json.loads(ap.with_suffix('.json').read_text())==json.loads(bp.with_suffix('.json').read_text());exact &= same_meta
    with np.load(ap.with_suffix('.npz'),allow_pickle=False) as aa,np.load(bp.with_suffix('.npz'),allow_pickle=False) as bb:
     same_keys=set(aa.files)==set(bb.files);exact &=same_keys
     record={'stage':stem,'metadata_equal':same_meta,'array_keys_equal':same_keys,'arrays':{}}
     for k in aa.files:
      if k not in bb:continue
      x,y=aa[k],bb[k];equal=x.shape==y.shape and x.dtype==y.dtype and np.array_equal(x,y,equal_nan=True);exact &=equal
      metric={'equal':bool(equal),'shape':list(x.shape),'dtype':str(x.dtype)}
      if x.shape==y.shape and x.size and np.issubdtype(x.dtype,np.number):
       delta=np.abs(x.astype(np.float64)-y.astype(np.float64));metric.update(max_abs=float(np.nanmax(delta)),mean_abs=float(np.nanmean(delta)),different_fraction=float(np.mean(x!=y)))
      record['arrays'][k]=metric
     row['comparisons'].append(record)
  am=a.get('result',{}).get('metrics') or {};bm=b.get('policy_result',{});bm=bm.get('metrics',bm)
  # Runner bookkeeping is projected outside benchmark metrics by RoboTenon.
  # Compare only native metric fields on both sides.
  am={k:v for k,v in am.items() if k not in ('steps','elapsed_sec','status')};bm={k:v for k,v in bm.items() if k not in ('steps','elapsed_sec','status')};row['metrics']={'upstream':am,'robotenon':bm,'equal':am==bm}
  exact &= am==bm
  row['observations']=len(a.get('observation',[]));row['actions']=len(a.get('action',[]));row['status']='exact_match' if exact else 'difference'
 except Exception as e:row['error']=str(e)
 results.append(row)
(ROOT/'comparison.json').write_text(json.dumps(results,indent=2));print([(x['benchmark'],x['seed'],x['status'],x.get('metrics')) for x in results])
