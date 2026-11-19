"""Run one real benchmark through upstream SyncEpisodeRunner or RoboTenon Graph.
No environment or action is mocked; only the policy returns a fixed action sequence.
"""
from __future__ import annotations
import argparse, asyncio, copy, importlib, json, random, sys, traceback,uuid
from pathlib import Path
import numpy as np
CONFIG={}; REPORT={}; OUT=None; COUNTS={}; ACTION_INDEX=0

def seed_all(seed):
 random.seed(seed);np.random.seed(seed)
 # Seed already-loaded torch without importing it into JAX-only environments.
 if 'torch' in sys.modules:sys.modules['torch'].manual_seed(seed)

def serial(value,arrays,prefix='root'):
 if isinstance(value,np.generic):return value.item()
 if isinstance(value,np.ndarray):
  arrays[prefix]=value.copy();return {'array':prefix,'shape':list(value.shape),'dtype':str(value.dtype)}
 if hasattr(value,'shape') and hasattr(value,'dtype'):
  try:return serial(np.asarray(value),arrays,prefix)
  except Exception:pass
 if isinstance(value,dict):return {str(k):serial(v,arrays,prefix+'/'+str(k)) for k,v in value.items()}
 if isinstance(value,(list,tuple)):return [serial(v,arrays,prefix+'/'+str(i)) for i,v in enumerate(value)]
 if isinstance(value,np.generic):return value.item()
 if value is None or isinstance(value,(str,int,float,bool)):return value
 return {'type':type(value).__name__}

def capture(kind,value):
 idx=COUNTS.get(kind,0);COUNTS[kind]=idx+1;arrays={};meta=serial(value,arrays)
 stem=f'{kind}-{idx:03d}';np.savez_compressed(OUT/(stem+'.npz'),**arrays)
 (OUT/(stem+'.json')).write_text(json.dumps(meta,sort_keys=True,allow_nan=True))
 REPORT.setdefault(kind,[]).append(stem)

def physical(native):
 result={};seen=set()
 def visit(obj,path,depth):
  if obj is None or id(obj) in seen or depth>8:return
  seen.add(id(obj))
  for key in ('qpos','qvel','ctrl','time','_elapsed_steps','elapsed_steps','_step_count','layout_id','style_id'):
   try:
    v=getattr(obj,key,None)
    if v is not None and not callable(v):result[path+'/'+key]=np.asarray(v).copy()
   except Exception:pass
  for key in ('_env','env','unwrapped','sim','data'):
   try:visit(getattr(obj,key,None),path+'/'+key,depth+1)
   except Exception:pass
 visit(native,'native',0)
 try:
  env=native._env.unwrapped
  robot=env.agent.robot
  for name in ('get_qpos','get_qvel'):
   value=getattr(robot,name)()
   if hasattr(value,'cpu'):value=value.detach().cpu().numpy()
   result['agent/'+name]=np.asarray(value).copy()
 except AttributeError:pass
 return result

def instrument(cls):
 original_init=cls.__init__;original_start=cls.start_episode;original_obs=cls.get_observation;original_action=cls.apply_action
 def init(self,*a,**kw):
  seed_all(CONFIG['seed']);original_init(self,*a,**kw)
 async def start(self,task,*a,**kw):
  seed_all(CONFIG['seed']);capture('task',task);await original_start(self,task,*a,**kw);capture('reset_state',physical(self))
  if CONFIG['benchmark']=='robocasa365':
   env=self._env.unwrapped.env
   capture('scene',{'meta':env.get_ep_meta(),'anchor':env.init_robot_base_pos_anchor,'rng':env.rng.bit_generator.state})
 async def obs(self):
  value=await original_obs(self);capture('observation',value);return value
 async def action(self,a):
  capture('action',a);await original_action(self,a)
  raw=getattr(self,'_last_result',None)
  capture('step',{'physical':physical(self),'reward':getattr(raw,'reward',None),'done':getattr(raw,'done',None),'info':getattr(raw,'info',None)})
 cls.__init__=init;cls.start_episode=start;cls.get_observation=obs;cls.apply_action=action

class Policy:
 def start_episode(self,info):pass
 def act(self,obs):
  global ACTION_INDEX
  capture('policy_input',obs);a=copy.deepcopy(CONFIG['actions'][ACTION_INDEX]);ACTION_INDEX+=1;return a
 def end_episode(self,result):REPORT['policy_result']=serial(result,{})
 def close(self):pass
class Connection:
 def __init__(self):self.policy=Policy()
 async def start_episode(self,info):return self.policy.start_episode(info)
 async def act(self,obs):return self.policy.act(obs)
 async def end_episode(self,result):return self.policy.end_episode(result)
def create_policy():return Policy()

async def run(side):
 benchmark=CONFIG['benchmark']
 if benchmark=='molmospaces' and CONFIG.get('resource_manifest'):
  from molmo_spaces import molmo_spaces_constants as constants
  manifest=json.loads(Path(CONFIG['resource_manifest']).read_text())
  constants.DATA_TYPE_TO_SOURCE_TO_VERSION.clear()
  constants.DATA_TYPE_TO_SOURCE_TO_VERSION.update(manifest)
  REPORT['resource_manifest']=manifest
 if benchmark in ('mikasa','robomme','calvin'):
  import torch  # initialize before seed_all: task cue timing uses torch RNG
 reference=CONFIG['upstream_class'] if side=='upstream' else CONFIG['class']
 module,classname=reference.split(':');cls=getattr(importlib.import_module(module),classname)
 if side=='upstream' and CONFIG.get('reference_render_guard') and benchmark=='robocasa365':
  import gc
  from functools import wraps
  def protect(method):
   @wraps(method)
   def guarded(*a,**kw):
    enabled=gc.isenabled()
    if enabled:gc.disable()
    try:return method(*a,**kw)
    finally:
     if enabled:gc.enable()
   return guarded
  cls.reset=protect(cls.reset);cls.step=protect(cls.step)
  REPORT['reference_render_guard']=True
 instrument(cls)
 if side=='upstream' and CONFIG.get('safe_gl_finalizer') and benchmark=='robocasa365':
  import glfw, ctypes
  from robosuite.utils.binding_utils import MjRenderContext
  old_del=MjRenderContext.__del__
  def safe_del(self):
   previous=glfw.get_current_context()
   own=getattr(getattr(self,'gl_ctx',None),'_context',None)
   addr=lambda x:ctypes.cast(x,ctypes.c_void_p).value if x else None
   try:
    self.gl_ctx.make_current()
    old_del(self)
   finally:
    if previous and addr(previous)!=addr(own):glfw.make_context_current(previous)
  MjRenderContext.__del__=safe_del
  REPORT['safe_gl_finalizer']=True
 if benchmark=='robocasa365' and CONFIG.get('trace_placement'):
  original_rng=np.random.default_rng
  def traced_rng(seed=None):
   rng=original_rng(seed)
   if seed is None:REPORT.setdefault('unseeded_rng_sites',[]).append(traceback.format_stack(limit=7))
   return rng
  np.random.default_rng=traced_rng
 if benchmark=='robocasa365' and CONFIG.get('trace_placement'):
  import robocasa.utils.env_utils as eu
  old_place=eu.compute_robot_base_placement_pose
  original_sampler=eu.get_single_fixture_sampler
  def trace_sampler(env, cfg, *a, **kw):
   capture('sampler_input', {'names': [x['name'] for x in cfg] if isinstance(cfg,list) else cfg['name'], 'rng': env.rng.bit_generator.state})
   return original_sampler(env,cfg,*a,**kw)
  eu.get_single_fixture_sampler=trace_sampler
  def trace_place(env,ref_fixture,*a,**kw):
   before={'reference':ref_fixture.name,'pos':ref_fixture.pos.copy(),'rot':ref_fixture.rot,'rng':env.rng.bit_generator.state,'fixtures':{k:{'pos':f.pos.copy(),'rot':getattr(f,'rot',None)} for k,f in env.fixtures.items()}}
   value=old_place(env,ref_fixture,*a,**kw)
   capture('placement',{'input':before,'result':value});return value
  eu.compute_robot_base_placement_pose=trace_place
 params=copy.deepcopy(CONFIG['params'])
 if benchmark=='molmospaces' and side=='robotenon' and CONFIG.get('resource_manifest'):
  # Graph task enumeration runs in a child process, so the pin must also be a
  # constructor parameter handled by the extension runtime adapter.
  params['resource_manifest']=CONFIG['resource_manifest']
 if CONFIG.get('disable_gc'):
  import gc
  gc.disable()
  REPORT['disable_gc']=True
 if benchmark=='vlabench' and 'seed' in params and side=='upstream':
  # The pinned upstream adapter has no seed argument. Seed its real loader
  # explicitly as the reference oracle; the extension implements this opt-in.
  reference_seed=params.pop('seed')
  import VLABench.envs as envs
  from VLABench.envs.dm_env import LM4ManipDMEnv
  old_init=LM4ManipDMEnv.__init__
  def seeded_init(self,*a,**kw):kw['random_state']=reference_seed;old_init(self,*a,**kw)
  LM4ManipDMEnv.__init__=seeded_init
  old_load=envs.load_env
  def seeded_load(*a,**kw):
   py_state,np_state=random.getstate(),np.random.get_state()
   try:
    random.seed(reference_seed);np.random.seed(reference_seed)
    return old_load(*a,**kw)
   finally:random.setstate(py_state);np.random.set_state(np_state)
  envs.load_env=seeded_load
  REPORT['reference_seed_adapter']=reference_seed
 if CONFIG.get('controlled_reset'):
  # Diagnostic only: seed the real simulator at construction, on BOTH sides.
  if benchmark=='robocasa365':
   from robocasa.wrappers.gym_wrapper import RoboCasaGymEnv
   old=RoboCasaGymEnv.__init__
   def seeded(self,*a,**kw):kw['seed']=CONFIG['seed'];old(self,*a,**kw)
   RoboCasaGymEnv.__init__=seeded
  elif benchmark=='vlabench':
   from VLABench.envs.dm_env import LM4ManipDMEnv
   old=LM4ManipDMEnv.__init__
   def seeded(self,*a,**kw):kw['random_state']=CONFIG['seed'];old(self,*a,**kw)
   LM4ManipDMEnv.__init__=seeded
  REPORT['controlled_reset']=True
 if side=='upstream':
  from vla_eval.runners.sync_runner import SyncEpisodeRunner
  cls.configure_render(CONFIG.get('render','gpu'));native=cls(**params)
  try:
   native.get_metadata()
   task=dict(native.get_tasks()[CONFIG.get('task_index',0)]);task['episode_idx']=0
   recorder=None
   if benchmark=='libero_mem':
    from vla_eval.recording import EpisodeRecorder,RecordingStore
    recorder=EpisodeRecorder(store=RecordingStore(OUT/'recording.sqlite'),sid='parity',eid='episode',eval_id='test',output_dir=OUT,filename_stem='test',context={},record_video=False,record_step=True)
    REPORT['upstream_recording']='official EpisodeRecorder; NullEpisodeRecorder lacks _next_step required by upstream LIBERO-Mem'
   REPORT['result']=await SyncEpisodeRunner().run_episode(native,task,Connection(),max_steps=len(CONFIG['actions']),recorder=recorder)
  finally:native.cleanup()
 else:
  from robotenon.runtime.experiment import ExperimentRequest,run_experiment
  from robotenon_benchmark_support.configure import application_config,write_application_config
  cfg=application_config({'benchmark_id':benchmark+'_benchmark','params':params,'max_steps':len(CONFIG['actions']),'max_tasks':1,'episodes_per_task':1,'render':CONFIG.get('render','gpu')},policy_factory='parity_probe:create_policy')
  cfg['safety_state']={'backend':'memory','require_shared':False}
  path=OUT/'graph.json';write_application_config(cfg,path)
  result=await run_experiment(ExperimentRequest(config=path,output_dir=OUT/'runs',run_id='parity-'+uuid.uuid4().hex[:8]))
  REPORT['graph_summary']={k:result.get(k) for k in ('num_episodes','num_errors','num_cancelled')}
  if result.get('num_errors') or result.get('num_episodes')!=1:raise RuntimeError('Graph did not finish one successful episode: '+str(REPORT['graph_summary']))
 REPORT['status']='passed'

def main():
 global CONFIG,OUT
 p=argparse.ArgumentParser();p.add_argument('--case',type=Path,required=True);p.add_argument('--side',choices=['upstream','robotenon'],required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args();CONFIG=json.loads(a.case.read_text());OUT=a.output;OUT.mkdir(parents=True,exist_ok=True);REPORT.update(side=a.side,seed=CONFIG['seed'],benchmark=CONFIG['benchmark'],status='error')
 try:asyncio.run(run(a.side))
 except Exception as e:REPORT.update(error=str(e),traceback=traceback.format_exc())
 finally:(OUT/'report.json').write_text(json.dumps(serial(REPORT,{}),indent=2))
 print(a.side,CONFIG['benchmark'],REPORT['status'],REPORT.get('error',''),flush=True)
 if REPORT['status']!='passed':raise SystemExit(1)
if __name__=='__main__':
 sys.modules['parity_probe']=sys.modules[__name__];main()
