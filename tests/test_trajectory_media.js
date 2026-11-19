// Run with: node --test tests/test_trajectory_media.js
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(require('node:path').join(__dirname, '../robotenon/web/static/portal.js'), 'utf8');

function load(name, context) {
  const start = source.indexOf('function ' + name + '(');
  assert.ok(start >= 0);
  // Compile the complete declaration, including multi-line helper bodies.
  for (let end = source.indexOf('\n', start); end >= 0; end = source.indexOf('\n', end + 1)) {
    const candidate = source.slice(start, end);
    try { new vm.Script(candidate); } catch { continue; }
    vm.runInNewContext(candidate, context);
    return;
  }
  throw new Error('Incomplete function ' + name);
}

test('preloading stays bounded to two observation groups', () => {
  const loaded = [];
  const context = {trajectoryPlaybackPreloadGroup: group => loaded.push(group)};
  load('trajectoryPlaybackPrimeImages', context);
  context.trajectoryPlaybackPrimeImages({}, Array.from({length: 1000}, (_, index) => ({hasObservation: index % 2 === 0, group: index})));
  assert.deepEqual(loaded, [0, 2]);
});

test('playback waits for the left camera along with the other cameras', () => {
  const context = {
    trajectoryPlaybackPreloadGroup() {},
    trajectoryObservationFrames: frames => frames,
    observationImageUrl: frame => frame.url,
    trajectoryPlaybackImageCache: {left: {state: 'loading'}, right: {state: 'ready'}, hand: {state: 'ready'}},
  };
  load('trajectoryPlaybackGroupReady', context);
  const group = {frames: ['left', 'right', 'hand'].map(url => ({url}))};
  assert.equal(context.trajectoryPlaybackGroupReady(group), false);
  context.trajectoryPlaybackImageCache.left.state = 'ready';
  assert.equal(context.trajectoryPlaybackGroupReady(group), true);
  context.trajectoryPlaybackImageCache.left.state = 'error';
  assert.equal(context.trajectoryPlaybackGroupReady(group), true, 'a failed camera must not stall playback forever');
});

test('video binding loads once, retries once and reports persistent errors', () => {
  const listeners = {};
  let loads = 0;
  const video = {dataset: {}, addEventListener: (event, handler) => { listeners[event] = handler; }, load: () => loads++};
  const caption = {};
  const context = {};
  load('bindTrajectoryVideo', context);
  context.bindTrajectoryVideo(video, '/media?kind=video', caption);
  context.bindTrajectoryVideo(video, '/media?kind=video', caption);
  assert.equal(loads, 1, 'polling must not reset the player');
  listeners.error();
  assert.equal(video.src, '/media?kind=video&retry=1');
  assert.equal(loads, 2);
  listeners.error();
  assert.equal(loads, 2);
  assert.match(caption.textContent, /加载失败/);
});

test('running trajectories follow new visits but preserve manual selection and playback', () => {
  const context = {trajectoryFollowLatest: true, trajectoryEpisodeIsRunning: () => true,
    trajectoryPlaybackActive: false, trajectoryPlaybackPaused: false, trajectoryPlaybackCompleted: false,
    trajectorySelectedSequenceOrder: 5};
  load('trajectoryRuntimeSelection', context);
  const detail = {runtime: {active_node: 'observe', sequence: [{id: 'observe', order: 5}, {id: 'observe', order: 11}]}};
  assert.equal(context.trajectoryRuntimeSelection(detail).order, 11);
  context.trajectoryFollowLatest = false;
  assert.equal(context.trajectoryRuntimeSelection(detail).order, 5);
  context.trajectoryFollowLatest = true;
  context.trajectoryPlaybackActive = true;
  assert.equal(context.trajectoryRuntimeSelection(detail).order, 5);
});

test('task search and status filters compose without changing the source list', () => {
  const context = {trajectoryItemLabel: item => item.task_id, trajectoryIndex: item => item.global_index,
    trajectoryEpisodeIsRunning: item => item.status === 'running'};
  load('filterTrajectoryTasks', context);
  const items = [{task_id: 'CloseDishwasher', global_index: 4, status: 'completed'},
    {task_id: 'CloseCabinet', global_index: 3, status: 'running'},
    {task_id: 'OpenDishwasher', global_index: 18, status: 'failed'}];
  assert.equal(context.filterTrajectoryTasks(items, 'dishwasher', 'all').length, 2);
  assert.equal(context.filterTrajectoryTasks(items, 'CLOSE', 'running')[0], items[1]);
  assert.equal(context.filterTrajectoryTasks(items, '18', 'error')[0], items[2]);
  assert.equal(context.filterTrajectoryTasks(items, 'missing', 'all').length, 0);
  assert.equal(items.length, 3);
  const outcomes = [
    {status: 'completed', task_success: false},
    {status: 'completed', task_success: true},
    {status: 'completed'},
    {status: 'running', task_success: true},
  ];
  assert.deepEqual(Array.from(context.filterTrajectoryTasks(outcomes, '', 'completed')), [outcomes[0], outcomes[2]]);
  assert.deepEqual(Array.from(context.filterTrajectoryTasks(outcomes, '', 'completed_success')), [outcomes[1]]);
  assert.equal(context.filterTrajectoryTasks(outcomes, '', 'all').length, 4);
});

test('timeline retains submillisecond precision and avoids rounded zero labels', () => {
  const context = {};
  load('trajectoryGanttDate', context);
  load('trajectoryGanttDurationText', context);
  const delta = context.trajectoryGanttDate('2026-09-20T00:00:00.000700+00:00') - context.trajectoryGanttDate('2026-09-20T00:00:00.000100+00:00');
  assert.ok(delta > .59 && delta < .61);
  assert.equal(context.trajectoryGanttDurationText(delta), '<1 ms');
});

test('JSON paths disambiguate dotted keys and arrays', () => {
  const context = {};
  load('configJsonPath', context);
  assert.equal(context.configJsonPath(['parameters','policy','chunk_duration_ms']), '$.parameters.policy.chunk_duration_ms');
  assert.equal(context.configJsonPath(['a.b',0,'x']), '$["a.b"][0].x');
});

test('nested JSON overrides preserve array siblings and do not mutate the source', () => {
  const context = {};
  load('configOverrideAt', context);
  const base = {tools:[{name:'a',timeout:10},{name:'b',timeout:20}]};
  const patch = context.configOverrideAt(base,['tools',1,'timeout'],30);
  assert.equal(patch.tools[0].timeout,10);
  assert.equal(patch.tools[1].name,'b');
  assert.equal(patch.tools[1].timeout,30);
  assert.equal(base.tools[1].timeout,20);
});

test('timeline resolves repeated nodes by invocation path rather than display order', () => {
 const c={};load('trajectoryGanttRuntimeNode',c);
 const sequence=[{id:'plan',order:1,events:[{path:['g',0,'plan'],step:0}]},{id:'plan',order:8,events:[{path:['g',4,'plan'],step:4}]}];
 const detail={runtime:{sequence},timeline:{spans:[]}};
 assert.equal(c.trajectoryGanttRuntimeNode(detail,{node:'plan',step:4,attributes:{path:['g',4,'plan']}}).order,8);
 assert.equal(c.trajectoryGanttRuntimeNode(detail,{node:'plan',step:4}).order,8);
 assert.equal(c.trajectoryGanttRuntimeNode(detail,{node:'plan'}),null);
 assert.equal(c.trajectoryGanttRuntimeNode(detail,{attributes:{caller_path:['g',0,'plan'],caller_node:'plan'}}).order,1);
});

test('timeline keeps first selected call when adding and promotes next when removing', () => {
 const seen=[],c={trajectoryGanttVisIndexFromId:id=>Number(id.split('-').pop()),trajectoryGanttVisSyncSpan:(detail,span)=>seen.push(span),trajectoryGanttVisRenderMarkers(){},trajectoryGanttVisUpdateStatus(){}};
 load('trajectoryGanttVisApplySelection',c);
 const state={spanByIndex:{2:'first',4:'second'},lastSelectionIds:[]};
 c.trajectoryGanttVisApplySelection(state,['gantt-span-2']);
 c.trajectoryGanttVisApplySelection(state,['gantt-span-4','gantt-span-2']);
 c.trajectoryGanttVisApplySelection(state,['gantt-span-4']);
 c.trajectoryGanttVisApplySelection(state,[]);
 assert.deepEqual(seen,['first','first','second']);
});

test('experiment search filters before paging and clamps page after refresh', () => {
  const context = {}; load('experimentListSlice', context);
  const rows=Array.from({length:45},(_,i)=>({job_id:'exp-'+i,config:i%2?'robot':'sim',status:'completed'}));
  let result=context.experimentListSlice(rows,'sim completed',2,10);
  assert.equal(result.total,23);assert.equal(result.pages,3);assert.equal(result.items[0].job_id,'exp-20');
  result=context.experimentListSlice(rows,'exp-44',20,10);
  assert.equal(result.page,1);assert.equal(result.total,1);
  result=context.experimentListSlice(rows,'missing',2,10);
  assert.equal(result.page,1);assert.equal(result.items.length,0);
});

test('bulk task targets keep all checked cases and reset between experiments', () => {
  const context={selectedJobId:'a',trajectoryCheckedJobId:'a',trajectoryCheckedKeys:new Set(['sim:0','sim:2']),
    trajectoryEpisodes:[0,1,2].map(i=>({adapter_id:'sim',global_index:i})),trajectoryAdapterId:'sim',
    trajectoryKey:item=>item.adapter_id+':'+item.global_index,trajectoryIndex:item=>item.global_index,
    trajectorySelectedItem:()=>null};
  load('trajectoryControlTargets',context);
  assert.equal(context.trajectoryControlTargets().length,2);
  context.selectedJobId='b';assert.equal(context.trajectoryControlTargets().length,0);
});

test('graph playback follows media from arbitrary nodes and skips repeated captures', () => {
  const context={Set};
  ['trajectoryVisualFrames','trajectoryObservationFrames','trajectoryObservationTemporalKey','trajectoryObservationFrameGroups','trajectoryObservationGroupList','trajectoryPlaybackStepValue','trajectoryPlaybackEntryGroups','trajectoryPlaybackAnnotateItems','trajectoryPlaybackBuildItems'].forEach(name=>load(name,context));
  const group=n=>({event:{step:n},frames:[{name:'custom_camera',uri:'cache://capture/'+n,modality:'rgb',shape:[16,16,3]}]});
  const detail={runtime:{sequence:[{id:'camera_custom',order:1,observation_groups:[group(1)]},
    {id:'reason_custom',order:2,observation_groups:[group(1)]},
    {id:'motion_custom',order:3,observation_groups:[group(2)]},
    {id:'final_custom',order:4,observation_groups:[]}]}};
  const items=context.trajectoryPlaybackBuildItems(detail);
  assert.deepEqual(Array.from(items,x=>x.hasObservation),[true,false,true,false]);
  assert.equal(items[2].nodeId,'motion_custom');
  assert.equal(items[2].observationTotal,2);
});

test('a new attempt discards old playback and image caches', () => {
  let cancelled=0,reset=0;
  const context={trajectoryPlaybackDetail:{identifiers:{attempt_id:'old',generation_started_at_ns:1}},
    trajectoryPlaybackImageCache:{old:{}},trajectoryPlaybackImageCacheKeys:['old'],
    trajectorySelectedSequenceOrder:55,trajectorySelectedNodeId:'reset',trajectoryFollowLatest:false,
    cancelTrajectoryPlayback(){cancelled++},resetTrajectoryGantt(){reset++}};
  load('trajectoryExecutionKey',context);load('syncTrajectoryPlayback',context);
  context.syncTrajectoryPlayback({identifiers:{attempt_id:'new',generation_started_at_ns:2}});
  assert.equal(cancelled,1);assert.equal(reset,1);
  assert.equal(Object.keys(context.trajectoryPlaybackImageCache).length,0);
  assert.equal(context.trajectoryPlaybackImageCacheKeys.length,0);
  assert.equal(context.trajectorySelectedSequenceOrder,0);
  assert.equal(context.trajectoryFollowLatest,true);
});

test('hour-long timing traces zoom beyond 6x down to a 100-millisecond window', () => {
  const start=Date.UTC(2026,8,20),duration=3*60*60*1000;
  let view={start:new Date(start),end:new Date(start+duration)};
  const state={start,end:start+duration,duration,timeline:{
    getWindow:()=>view,setWindow:(left,right)=>{view={start:left,end:right}}}};
  const context={Date,trajectoryGanttVisState:state,trajectorySelectedDetail:null,trajectoryGanttZoom:1,
    updateTrajectoryGanttZoomControls(){},trajectoryGanttVisUpdateStatus(){}};
  ['trajectoryGanttZoomLimit','trajectoryGanttZoomFromSlider','trajectoryGanttVisApplyZoom','setTrajectoryGanttZoom','trajectoryGanttVisWindow'].forEach(name=>load(name,context));
  const center=start+duration/2;
  for(let n=0;n<24;n++)context.setTrajectoryGanttZoom(context.trajectoryGanttZoom*2);
  assert.equal(view.end-view.start,100);
  assert.ok(Math.abs((+view.start+ +view.end)/2-center)<=24, 'Date rounding stays within one millisecond per zoom step');
  assert.equal(context.trajectoryGanttZoomFromSlider(0),1);
  assert.equal(context.trajectoryGanttZoomFromSlider(1000),duration/100);
  const refreshed=context.trajectoryGanttVisWindow(start,start+duration+60000,view);
  assert.equal(refreshed.end-refreshed.start,100,'live refresh preserves the zoomed window');
  context.setTrajectoryGanttZoom(1);
  assert.equal(view.end-view.start,duration);
});
