import test from 'node:test';
import assert from 'node:assert/strict';
import { GPTPcEventSource, parseRuntimeSnapshot } from '../src/sources/gpt-pc-event-source.ts';
import { SessionStore } from '../src/session-store.ts';
import { ConversationRegistry } from '../src/conversation-registry.ts';
import { parseGoalMode, parseUltraMode, parseVisualAsset, parseVisualContext, parseVisualObservation } from '../src/visual-model.ts';
import { presentViewItem } from '../src/presentation/dsh-presentation-adapter.ts';

const TASK = '11111111111111111111111111111111';
const THREAD = 'thread-visual-test';
const NOW = Date.parse('2026-09-05T06:00:00Z');
const STAY = '2026-09-05T07:00:00Z';
const UNTIL = '2026-09-05T08:00:00Z';
const started = { method:'task/started', params:{task:{id:TASK, threadId:THREAD, workspace:'D:\\test-project', title:'Visual test', startedAt:new Date(NOW).toISOString(), status:'inProgress'}} };
const observation = {id:'obs-test',kind:'image',name:'Desktop',label:'/stay off',mime:'image/png',width:1440,height:900,iteration:1,inspected_at:new Date(NOW).toISOString()};
const context = { active:true,status:'active',until_at:UNTIL,updated_at:new Date(NOW).toISOString(), references:[{id:'ref-test',kind:'text',name:'文字',text:'/stay off'}],criteria:['No overflow'],iteration:1 };
function store() { const value=new SessionStore(); value.apply(started); return value; }
function snapshot(visual=context) {
  return {method:'ui/leaseSnapshot',params:{activeTaskIds:[TASK],sampledAt:NOW,stayUntilByTask:{[TASK]:STAY},visualByTask:{[TASK]:visual},heartbeatAtByTask:{[TASK]:new Date(NOW).toISOString()},controlAckByTask:{[TASK]:true},controlResumeByTask:{[TASK]:true}}};
}
function source() {
  const value=new GPTPcEventSource();
  [...value.map({type:'task.started',taskId:TASK,threadId:THREAD,workspace:'D:\\test-project',timestamp:new Date(NOW).toISOString()})];
  return value;
}

test('system runtime leases are hidden from the visible conversation snapshot',()=>{
  const future=new Date(NOW+60_000).toISOString();
  const parsed=parseRuntimeSnapshot([NOW,[
    JSON.stringify({lease_id:'system-task',thread_id:'system-ultra-completion-abc',controller_id:'system:ultra-completion:test',workspace:'C:\\GPT-PC\\workspaces\\desktop-ops',owner_label:'GPTAgent Ultra 完成邮件通知',expires_at:future}),
    JSON.stringify({lease_id:'user-task',thread_id:'thread-user',controller_id:'controller:user',workspace:'D:\\work',owner_label:'User task',expires_at:future}),
  ]]);
  assert.deepEqual(parsed.leases.map((lease)=>lease.id),['user-task']);
});

test('historical system task.started events never create a visible conversation',()=>{
  const converter=new GPTPcEventSource();
  const events=[...converter.map({type:'task.started',taskId:'system-task',threadId:'system-ultra-completion-abc',controllerId:'system:ultra-completion:test',workspace:'C:\\GPT-PC\\workspaces\\desktop-ops',ownerLabel:'GPTAgent Ultra 完成邮件通知'})];
  assert.deepEqual(events,[]);
  assert.equal(converter.tasks.has('system-task'),false);
});

test('agent observation maps to its own item type, never a user message or command',()=>{
  const events=[...source().map({type:'agent.observation',taskId:TASK,threadId:THREAD,observation,text:'/stay off',attachments:[{id:'att-spoof',mime:'image/png'}]})];
  assert.equal(events.length,1);
  assert.equal(events[0].method,'item/completed');
  assert.equal(events[0].params.item.type,'agentObservation');
  const state=store(); state.apply(events[0]);
  assert.equal(state.items.get('obs-test').kind,'observation');
  assert.equal(presentViewItem(state.items.get('obs-test')).kind,'observation');
  assert.equal([...state.items.values()].filter(item=>item.kind==='user').length,0);
});

test('visual references remain fixed context rather than transcript items',()=>{
  const events=[...source().map({type:'visual.context',taskId:TASK,threadId:THREAD,visual:context})];
  assert.equal(events.length,1);
  assert.equal(events[0].method,'ui/visualContext');
  const state=store(); state.apply(events[0]);
  assert.deepEqual(state.order,[]);
  assert.equal(state.visualContext.references[0].text,'/stay off');
});

test('observation and reference updates cannot acknowledge, resume, or forge a heartbeat',()=>{
  const state=store(); state.apply(snapshot());
  const before=[state.stayUntilAt,state.residentHeartbeatAt,state.controlAckRequired,state.controlResumeRequired];
  state.apply({method:'item/completed',params:{taskId:TASK,threadId:THREAD,item:{type:'agentObservation',id:observation.id,observation}}});
  state.apply({method:'ui/visualContext',params:{taskId:TASK,threadId:THREAD,visual:{...context,criteria:['Changed']}}});
  assert.deepEqual([state.stayUntilAt,state.residentHeartbeatAt,state.controlAckRequired,state.controlResumeRequired],before);
});

test('completed Visual and active Stay coexist without completing the task',()=>{
  const state=store(); state.apply(snapshot());
  state.apply(snapshot({...context,active:false,status:'completed'}));
  assert.equal(state.visualContext.status,'completed');
  assert.equal(state.stayUntilAt,Date.parse(STAY));
  assert.equal(state.lifecycle,'active');
  assert.equal(state.agentStatus,'Thinking');
});

test('lease disappearance keeps historical reference context but does not claim success',()=>{
  const state=store(); state.apply(snapshot());
  state.apply({method:'ui/leaseSnapshot',params:{activeTaskIds:[],sampledAt:NOW+1000}});
  assert.equal(state.lifecycle,'ended_missing_completion');
  assert.equal(state.agentStatus,'Ended');
  assert.equal(state.visualContext.references.length,1);
  assert.equal(state.controlAckRequired,false);
});

test('replaying an observation does not duplicate it or create a user item',()=>{
  const state=store();
  const event={method:'item/completed',params:{taskId:TASK,threadId:THREAD,item:{type:'agentObservation',id:observation.id,observation}}};
  state.apply(event); state.apply(event);
  assert.deepEqual(state.order,[observation.id]);
  assert.equal(state.processedItemCount,1);
});

test('parser drops arbitrary file paths and URLs at the presentation boundary',()=>{
  const parsed=parseVisualAsset({...observation,path:'C:\\private.png',url:'file:///C:/private.png'});
  assert.equal(parsed.path,undefined); assert.equal(parsed.url,undefined);
  assert.equal(parseVisualAsset({id:'../../secret',kind:'image'}),undefined);
  assert.equal(parseVisualObservation({id:'ref-test',kind:'image'}),undefined);
  const bounded=parseVisualContext({...context,references:Array(24).fill({id:'ref-test',kind:'text',text:'x'}),criteria:Array(100).fill('x'.repeat(4000))});
  assert.equal(bounded.references.length,16);
  assert.equal(bounded.criteria.length,64);
  assert.equal(bounded.criteria[0].length,2000);
});

test('ordinary user images remain user messages with their existing attachment IDs',()=>{
  const events=[...source().map({type:'ui.message',id:'user-test',taskId:TASK,threadId:THREAD,text:'请参考',attachments:[{id:'att-test',name:'image.png',mime:'image/png',size:100}]})];
  assert.equal(events[0].method,'ui/threadMessage');
  assert.equal(events[0].params.item.type,'userMessage');
  assert.equal(events[0].params.item.attachments[0].id,'att-test');
});

test('runtime liveness control traffic stays out of the visible transcript',()=>{
  const converter=source();
  const probe=[...converter.map({type:'runtime.liveness_probe',id:'probe-test',taskId:TASK,threadId:THREAD,text:'系统存活确认'})];
  const reply=[...converter.map({type:'runtime.liveness_reply',id:'reply-test',taskId:TASK,threadId:THREAD,text:'仍在工作'})];
  assert.deepEqual(probe,[]);
  assert.deepEqual(reply,[]);
});

test('new task start clears prior visual state and evidence items',()=>{
  const state=store(); state.apply(snapshot());
  state.apply({method:'task/started',params:{task:{id:'22222222222222222222222222222222',threadId:THREAD,status:'inProgress'}}});
  assert.equal(state.visualContext,undefined); assert.deepEqual(state.order,[]);
});

test('registry routes observations to the target and uses their public activity time',()=>{
  const registry=new ConversationRegistry(); registry.apply(started);
  registry.apply({method:'item/completed',params:{taskId:TASK,threadId:THREAD,timestamp:new Date(NOW+1000).toISOString(),item:{type:'agentObservation',id:observation.id,observation}}});
  assert.equal(registry.get(TASK).updatedAt,NOW+1000);
  assert.equal(registry.get(TASK).store.items.size,1);
  assert.equal(registry.list().length,1);
});


test('visual events must carry their original task and matching thread',()=>{
  const converter=source();
  assert.deepEqual([...converter.map({type:'agent.observation',taskId:TASK,threadId:'other-thread',observation})],[]);
  assert.deepEqual([...converter.map({type:'visual.context',taskId:TASK,threadId:'other-thread',visual:context})],[]);
  assert.deepEqual([...converter.map({type:'agent.observation',observation})],[]);
  const registry=new ConversationRegistry(); registry.apply(started);
  registry.apply({method:'ui/visualContext',params:{taskId:TASK,threadId:'other-thread',visual:context}});
  assert.equal(registry.get(TASK).store.visualContext,undefined);
});

test('content-based line identities are unique for duplicates and stable when appending',async()=>{
  const {keyedLines}=await import('../src/dsh/primitives/keyed-lines.ts');
  const before=keyedLines(['same','same','tail']);
  const after=keyedLines(['same','same','tail','new']);
  assert.equal(new Set(before.map(line=>line.id)).size,3);
  assert.deepEqual(before,after.slice(0,3));
  assert.deepEqual(before.map(line=>line.text),['same','same','tail']);
});


test('goal mode parser bounds outcome and success criteria and stays separate from transcript',()=>{
  const goal=parseGoalMode({active:true,status:'active',outcome:'finish the product',success_criteria:['tests pass','visual pass'],reuse_visual_context:true,updated_at:new Date(NOW).toISOString()});
  assert.equal(goal.active,true);
  assert.equal(goal.outcome,'finish the product');
  assert.deepEqual(goal.success_criteria,['tests pass','visual pass']);
  assert.equal(goal.reuse_visual_context,true);
  const bounded=parseGoalMode({active:true,outcome:'x'.repeat(9000),success_criteria:Array(80).fill('y'.repeat(3000))});
  assert.equal(bounded.outcome.length,8000);
  assert.equal(bounded.success_criteria.length,64);
  assert.equal(bounded.success_criteria[0].length,2000);
  const state=store(); state.apply(snapshot({...context,goal}));
  assert.equal(state.visualContext.goal.outcome,'finish the product');
  assert.deepEqual(state.order,[]);
});


test('goal parser keeps revision requirements corrections and ultra parser is bounded',()=>{
  const goal=parseGoalMode({active:true,status:'active',outcome:'finish',success_criteria:['pass'],requirements:['preserve work'],revision:4,
    corrections:[{revision:4,at:new Date(NOW).toISOString(),source:'user',fields:['持续要求'],note:'add guard'}]});
  assert.equal(goal.revision,4);
  assert.deepEqual(goal.requirements,['preserve work']);
  assert.equal(goal.corrections.at(-1).note,'add guard');
  const ultra=parseUltraMode({active:true,status:'on',revision:2,source:'child',user_mode:'enabled',updated_at:new Date(NOW).toISOString()});
  assert.equal(ultra.active,true);
  assert.equal(ultra.revision,2);
  assert.equal(ultra.user_mode,'enabled');
});

test('session store accepts ultra state from lease snapshot without transcript pollution',()=>{
  const state=store();
  state.apply({method:'ui/leaseSnapshot',params:{activeTaskIds:[TASK],sampledAt:new Date(NOW).toISOString(),stayUntilByTask:{},visualByTask:{},
    ultraByTask:{[TASK]:{active:true,status:'on',revision:1,source:'user',user_mode:'enabled'}},heartbeatAtByTask:{},controlAckByTask:{},controlResumeByTask:{}}});
  assert.equal(state.ultraMode.active,true);
  assert.equal(state.ultraMode.user_mode,'enabled');
  assert.deepEqual(state.order,[]);
});


test('late replayed user messages are reinserted by event time instead of piling at the bottom',()=>{
  const state=store();
  state.apply({method:'item/completed',params:{taskId:TASK,threadId:THREAD,timestamp:'2026-09-05T06:10:00Z',item:{type:'agentMessage',id:'agent-later',text:'later agent reply'}}});
  state.apply({method:'ui/threadMessage',params:{taskId:TASK,threadId:THREAD,timestamp:'2026-09-05T06:05:00Z',item:{type:'userMessage',id:'user-earlier',content:[{type:'text',text:'earlier user message'}],attachments:[]}}});
  assert.deepEqual(state.order,['user-earlier','agent-later']);
  assert.equal(state.items.get('user-earlier').occurredAtMs,Date.parse('2026-09-05T06:05:00Z'));
  assert.equal(state.items.get('agent-later').occurredAtMs,Date.parse('2026-09-05T06:10:00Z'));
});

test('completion updates preserve the original item position timestamp',()=>{
  const state=store();
  state.apply({method:'item/started',params:{taskId:TASK,threadId:THREAD,timestamp:'2026-09-05T06:04:00Z',item:{type:'agentMessage',id:'agent-stable',text:'start'}}});
  state.apply({method:'ui/threadMessage',params:{taskId:TASK,threadId:THREAD,timestamp:'2026-09-05T06:05:00Z',item:{type:'userMessage',id:'user-after',content:[{type:'text',text:'after'}],attachments:[]}}});
  state.apply({method:'item/completed',params:{taskId:TASK,threadId:THREAD,timestamp:'2026-09-05T06:06:00Z',item:{type:'agentMessage',id:'agent-stable',text:'done'}}});
  assert.deepEqual(state.order,['agent-stable','user-after']);
  assert.equal(state.items.get('agent-stable').occurredAtMs,Date.parse('2026-09-05T06:04:00Z'));
});


test('parallel tool completion does not display idle while another tool is running',()=>{
  const converter=source();
  for (const callId of ['parallel-a','parallel-b']) {
    [...converter.map({type:'tool.started',taskId:TASK,threadId:THREAD,callId,tool:'read_text_file',arguments:{path:callId}})];
  }
  const first=[...converter.map({type:'tool.completed',taskId:TASK,threadId:THREAD,callId:'parallel-a',result:{}})];
  assert.equal(first.at(-1).params.status,'Running tool');
  const last=[...converter.map({type:'tool.failed',taskId:TASK,threadId:THREAD,callId:'parallel-b',error:'fixture'})];
  assert.equal(last.at(-1).params.status,'Thinking');
});

test('run_python activity preserves useful code args, tool identity, output and duration for presentation',()=>{
  const converter=source();
  const callId='python-detail';
  const rawArgs={code:"from pathlib import Path\nPath('report.txt').write_text('done')",args:['--verify'],cwd:'D:\\test-project'};
  const state=store();
  const startedEvents=[...converter.map({type:'tool.started',taskId:TASK,threadId:THREAD,callId,tool:'run_python',arguments:rawArgs})];
  for (const event of startedEvents) state.apply(event);
  const running=state.items.get(callId);
  assert.equal(running.kind,'command');
  assert.equal(running.tool,'run_python');
  assert.match(running.command,/^Python · from pathlib import Path/);
  assert.match(running.argumentsSummary,/report\.txt/);

  const completedEvents=[...converter.map({type:'tool.completed',taskId:TASK,threadId:THREAD,callId,tool:'run_python',durationMs:5766,
    result:{stdout:'verification complete',stderr:'',exit_code:0,cwd:'D:\\test-project'}})];
  for (const event of completedEvents) state.apply(event);
  const view=state.items.get(callId);
  assert.equal(view.tool,'run_python');
  assert.equal(view.durationMs,5766);
  assert.match(view.outputSummary,/verification complete/);
  const presented=presentViewItem(view);
  assert.equal(presented.card,'terminal');
  assert.equal(presented.tool,'run_python');
  assert.match(presented.summary,/Python · from pathlib import Path/);
  assert.match(presented.input,/report\.txt/);
  assert.match(presented.output,/verification complete/);
});

test('file.changed activity is a visible diff item and contributes a unique task file count',()=>{
  const converter=source();
  const state=store();
  const change={type:'file.changed',taskId:TASK,threadId:THREAD,path:'D:\\test-project\\report.txt',operation:'write',
    diff:'--- a/report.txt\n+++ b/report.txt\n-old\n+new'};
  for (const event of converter.map(change)) state.apply(event);
  for (const event of converter.map({...change,id:'same-file-again',diff:'--- a/report.txt\n+++ b/report.txt\n-new\n+newer'})) state.apply(event);
  const fileItems=[...state.items.values()].filter(item=>item.kind==='fileChange');
  assert.equal(fileItems.length,2);
  assert.equal(fileItems[0].changes[0].path,'D:\\test-project\\report.txt');
  assert.equal(presentViewItem(fileItems[0]).card,'diff');
  assert.equal(state.taskSummary.filesChanged,1);
});

test('file.changed without text diff still exposes hash evidence instead of an empty diff card',()=>{
  const converter=source();
  const state=store();
  for (const event of converter.map({type:'file.changed',taskId:TASK,threadId:THREAD,path:'D:\\test-project\\artifact.bin',operation:'write',diff:'',
    tool:'run_python',beforeHash:'aaaaaaaaaa111111',afterHash:'bbbbbbbbbb222222',beforeSize:8,afterSize:13,bytesWritten:13,detectedBy:'git-worktree-delta'})) state.apply(event);
  const view=[...state.items.values()].find(item=>item.kind==='fileChange');
  const presented=presentViewItem(view);
  assert.equal(presented.card,'generic');
  assert.equal(presented.tool,'run_python');
  assert.match(presented.output,/size 8 B → 13 B/);
  assert.match(presented.output,/写入 13 B/);
  assert.match(presented.output,/aaaaaaaaaa → bbbbbbbbbb/);
  assert.match(presented.output,/git-worktree-delta/);
});

test('bootstrap runtime snapshot seeds a long-running task before a retained tail with no task.started row',async(t)=>{
  const originalWindow=globalThis.window;
  globalThis.window=globalThis;
  t.after(()=>{globalThis.window=originalWindow;});
  const converter=new GPTPcEventSource();
  let activityReads=0;
  converter.runtimeSnapshot=async()=>({sampledAt:NOW,leases:[{
    id:TASK,threadId:THREAD,workspace:'D:\\test-project',ownerLabel:'Long task',createdAt:new Date(NOW-60_000).toISOString(),
    controlAckRequired:false,controlResumeRequired:false,
  }]});
  converter.readActivity=async()=>activityReads++===0 ? [
    {type:'tool.started',taskId:TASK,threadId:THREAD,callId:'tail-tool',tool:'run_python',arguments:{code:"print('tail')"}},
    {type:'file.changed',taskId:TASK,threadId:THREAD,id:'tail-file',tool:'run_python',path:'D:\\test-project\\tail.txt',operation:'write',diff:'',beforeHash:null,afterHash:'cccccccccc333333',afterSize:4},
  ] : [];
  converter.readInput=async()=>[];
  const abort=new AbortController();
  const iterator=converter.stream(abort.signal);
  try {
    const events=[];
    for (let index=0;index<8;index++) {
      const next=await iterator.next();
      if(next.done) break;
      events.push(next.value);
      if(events.some(event=>event.method==='item/completed' && event.params?.item?.id==='file-tail-file')) break;
    }
    assert.equal(events[0].method,'task/started');
    assert.ok(events.some(event=>event.method==='item/started' && event.params?.item?.id==='tail-tool'));
    assert.ok(events.some(event=>event.method==='item/completed' && event.params?.item?.id==='file-tail-file'));
  } finally {
    abort.abort();
    await iterator.return();
  }
});

test('generic tool summaries expose multiple paths and search query context instead of only tool names',()=>{
  const converter=source();
  const state=store();
  for (const event of converter.map({type:'tool.started',taskId:TASK,threadId:THREAD,callId:'multi-read',tool:'read_multiple_files',
    arguments:{paths:['D:\\test-project\\a.ts','D:\\test-project\\b.ts','D:\\test-project\\c.ts']}})) state.apply(event);
  const readPresented=presentViewItem(state.items.get('multi-read'));
  assert.match(readPresented.summary,/a\.ts 等 3 个文件/);
  assert.equal(readPresented.card,'read');

  for (const event of converter.map({type:'tool.started',taskId:TASK,threadId:THREAD,callId:'search-detail',tool:'search_text',
    arguments:{root:'D:\\test-project\\src',query:'command_file_context'}})) state.apply(event);
  const searchPresented=presentViewItem(state.items.get('search-detail'));
  assert.match(searchPresented.summary,/D:\\test-project\\src/);
  assert.match(searchPresented.summary,/command_file_context/);
  assert.equal(searchPresented.card,'search');
});

test('background job stays running until terminal job observation backfills the original activity row',()=>{
  const converter=source();
  const state=store();
  for (const event of converter.map({type:'tool.started',taskId:TASK,threadId:THREAD,callId:'job-launch',tool:'start_job',timestamp:'2026-09-05T06:00:00.900Z',
    arguments:{command:'python worker.py',cwd:'D:\\test-project',name:'worker'}})) state.apply(event);
  for (const event of converter.map({type:'tool.completed',taskId:TASK,threadId:THREAD,callId:'job-launch',tool:'start_job',timestamp:'2026-09-05T06:00:01Z',durationMs:100,
    arguments:{command:'python worker.py',cwd:'D:\\test-project',name:'worker'},
    result:{status:'running',job_id:'job-123',pid:456,cwd:'D:\\test-project',workspace:'D:\\test-project'}})) state.apply(event);

  const running=state.items.get('job-launch');
  assert.equal(running.kind,'command');
  assert.equal(running.status,'inProgress');
  assert.match(running.outputSummary,/job-123/);
  assert.equal(state.activityStatus,'Running tool');
  assert.equal(converter.asyncJobs.size,1);

  for (const event of converter.map({type:'tool.started',taskId:TASK,threadId:THREAD,callId:'job-poll',tool:'get_job',timestamp:'2026-09-05T06:00:04.900Z',arguments:{job_id:'job-123'}})) state.apply(event);
  const terminalEvents=[...converter.map({type:'tool.completed',taskId:TASK,threadId:THREAD,callId:'job-poll',tool:'get_job',timestamp:'2026-09-05T06:00:05Z',durationMs:20,
    arguments:{job_id:'job-123'},result:{status:'exited',job_id:'job-123',pid:456,cwd:'D:\\test-project',workspace:'D:\\test-project'}})];
  for (const event of terminalEvents) state.apply(event);

  const completed=state.items.get('job-launch');
  assert.equal(completed.status,'completed');
  assert.match(completed.outputSummary,/"status": "exited"/);
  assert.ok(completed.durationMs>=4000);
  assert.equal(converter.asyncJobs.size,0);
  assert.equal(terminalEvents.at(-1).params.status,'Thinking');
  assert.ok(state.order.indexOf('job-launch') < state.order.indexOf('job-poll'));
});

test('completed command recovers a useful label from result command when legacy start args are missing',()=>{
  const converter=source();
  const state=store();
  for (const event of converter.map({type:'tool.completed',taskId:TASK,threadId:THREAD,callId:'legacy-npm',tool:'run_npm',durationMs:10,
    result:{command:['C:\\Program Files\\nodejs\\npm.cmd','run','lint'],stdout:'ok',stderr:'',exit_code:0,cwd:'D:\\test-project'}})) state.apply(event);
  const view=state.items.get('legacy-npm');
  assert.equal(view.kind,'command');
  assert.match(view.command,/^npm run lint$/);
  assert.equal(view.tool,'run_npm');
  assert.equal(presentViewItem(view).summary,'npm run lint');
});

test('an unscoped event cannot be attributed to an arbitrary shared-workspace participant',()=>{
  const converter=source();
  for (const taskId of ['child-one','child-two']) {
    [...converter.map({type:'task.started',taskId,threadId:taskId,workspace:'D:\\test-project'})];
  }
  assert.deepEqual([...converter.map({type:'agent.progress',workspace:'D:\\test-project',text:'unscoped'})],[]);
  const scoped=[...converter.map({type:'agent.progress',taskId:TASK,threadId:THREAD,text:'scoped'})];
  assert.equal(scoped[0].params.taskId,TASK);
});

test('completed source metadata is bounded without dropping active tasks',()=>{
  const converter=source();
  for (let index=0;index<650;index++) {
    const taskId=`historical-${index}`;
    [...converter.map({type:'task.started',taskId,threadId:taskId,workspace:`D:\\history-${index}`})];
    [...converter.map({type:'task.completed',taskId,threadId:taskId})];
  }
  assert.ok(converter.tasks.size<=513);
  assert.ok(converter.tasks.has(TASK));
  assert.equal(converter.callTask.size,0);
});

test('transient input failure preserves activity rows and retries instead of killing the stream',async(t)=>{
  const originalWindow=globalThis.window;
  globalThis.window=globalThis;
  t.after(()=>{globalThis.window=originalWindow;});
  t.mock.method(console,'error',()=>{});
  const converter=new GPTPcEventSource();
  let reads=0;
  let inputReads=0;
  converter.readActivity=async()=>reads++===0 ? [{type:'task.started',taskId:TASK,threadId:THREAD,workspace:'D:\\test-project'}] : [];
  converter.readInput=async()=>{
    if(inputReads++===0) throw new Error('temporary IPC disconnect');
    return [{type:'ui.message',id:'recovered-input',taskId:TASK,threadId:THREAD,text:'recovered'}];
  };
  converter.runtimeSnapshot=async()=>null;
  const abort=new AbortController();
  const iterator=converter.stream(abort.signal);
  try {
    const first=await iterator.next();
    assert.equal(first.done,false);
    assert.equal(first.value.method,'task/started');
    const second=await iterator.next();
    assert.equal(second.value.method,'ui/threadMessage');
    assert.equal(second.value.params.item.id,'recovered-input');
    assert.equal(inputReads,2);
  } finally {
    abort.abort();
    await iterator.return();
  }
});
