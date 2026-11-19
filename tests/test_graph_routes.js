const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(require('node:path').join(__dirname, '../robotenon/web/static/portal.js'), 'utf8');
function load(name, context) {
  const start = source.indexOf('function ' + name + '(');
  for (let end = source.indexOf('\n', start); end >= 0; end = source.indexOf('\n', end + 1)) {
    const declaration = source.slice(start, end);
    try { new vm.Script(declaration); } catch { continue; }
    vm.runInNewContext(declaration, context);
    return;
  }
  throw new Error('Missing function ' + name);
}
test('graph display preserves next, goto, self loop and default separately', () => {
  const context = {};
  ['normalizeGraph', 'graphEdges', 'layoutGraph'].forEach(name => load(name, context));
  const graph = context.normalizeGraph({nodes: [
    {id: 'A', next: 'next B when true'},
    {id: 'B', predecessors: ['A'], goto: 'goto A when false', loop: {every: '20ms', when: 'true'}, default: 'END'},
  ]});
  const edges = JSON.parse(JSON.stringify(context.graphEdges(graph)));
  assert.equal(edges.length, 4);
  assert.ok(edges.some(e => e.source === 'A' && e.target === 'B' && e.kind === 'next'));
  assert.ok(edges.some(e => e.source === 'B' && e.target === 'A' && e.kind === 'goto'));
  assert.ok(edges.some(e => e.source === 'B' && e.target === 'B' && e.kind === 'loop' && e.label === 'loop every 20ms when true'));
  assert.ok(edges.some(e => e.target === 'END' && e.label === 'default'));
  const layout = context.layoutGraph([...graph.nodes, {id: 'END'}], edges);
  assert.ok(layout.rank.B > layout.rank.A);
});
test('same target next and goto remain distinct in the display', () => {
  const context = {};
  ['normalizeGraph', 'graphEdges'].forEach(name => load(name, context));
  const graph = context.normalizeGraph({nodes: [
    {id: 'A', next: 'B', goto: 'B', loop: false},
    {id: 'B', predecessors: ['A']},
  ]});
  const edges = context.graphEdges(graph);
  assert.equal(edges.length, 2);
  assert.ok(edges.some(e => e.kind === 'next'));
  assert.ok(edges.some(e => e.kind === 'goto'));
});
test('predecessor OR groups render every dependency edge', () => {
  const context = {};
  ['normalizeGraph', 'graphEdges'].forEach(name => load(name, context));
  const graph = context.normalizeGraph({nodes: [
    {id: 'A', next: 'C'},
    {id: 'B', next: 'C'},
    {id: 'C', predecessors: [['A', 'B']]},
  ]});
  const edges = context.graphEdges(graph);
  assert.ok(edges.some(e => e.source === 'A' && e.target === 'C'));
  assert.ok(edges.some(e => e.source === 'B' && e.target === 'C'));
});
test('loop tooltip explains condition trigger separately from execution trigger', () => {
  const context = {};
  load('graphEdgeDescription', context);
  assert.match(context.graphEdgeDescription({kind: 'loop', label: 'loop every 20ms'}), /ctx.trigger/);
  assert.match(context.graphEdgeDescription({kind: 'loop'}), /额外循环一次/);
  assert.equal(context.graphEdgeDescription({kind: 'next', label: 'next when ready'}), 'next when ready');
});

test('long graphs wrap into compact bands without overlapping nodes', () => {
  const context = {};
  load('layoutGraph', context);
  const nodes=Array.from({length:12},(_,i)=>({id:'n'+i}));
  const edges=nodes.slice(1).map((n,i)=>({source:'n'+i,target:n.id,kind:'next'}));
  edges.push({source:'n10',target:'n1',kind:'goto'});
  const layout=context.layoutGraph(nodes,edges,'n0');
  assert.ok(layout.width <= 1000);
  assert.equal(layout.bands.length,3);
  assert.equal(layout.positions.n3.x,layout.positions.n4.x);
  assert.ok(layout.positions.n4.y>layout.positions.n3.y);
  const positions=Object.values(layout.positions);
  positions.forEach((a,i)=>positions.slice(i+1).forEach(b=>assert.ok(Math.abs(a.x-b.x)>=144||Math.abs(a.y-b.y)>=76)));
});

test('configured entry graph wins over a larger registered graph', () => {
  const context={graphCatalog:{entry:{},large:{}},graphCatalogOrder:['large','entry']};
  load('graphRootFromSnapshot',context);
  assert.equal(context.graphRootFromSnapshot({configured_graph_id:'entry',primary_graph_id:'large'}),'entry');
});
