// Hermetic windows-mcp 0.8.2 contract regressions: no desktop access or network.
import assert from 'node:assert/strict';
import { Client } from '@modelcontextprotocol/client';
import { registerWindowsBridgeTools } from '../dist/windowsBridge.js';
const savedEnv={...process.env};
const original=Object.fromEntries(['connect','close','getServerVersion','listTools','callTool','ping'].map(k=>[k,Client.prototype[k]]));
const obj=properties=>({type:'object',properties,additionalProperties:false});
const integer={type:'integer'},string={type:'string'},boolean={type:'boolean'},loc={type:'array',items:{type:'integer'},minItems:2,maxItems:2};
const inventory=[
 ['App',obj({mode:string,name:string})],['Snapshot',obj({use_vision:boolean,use_ui_tree:boolean,use_dom:boolean})],['Screenshot',obj({})],
 ['Click',obj({loc,label:integer,button:string,clicks:integer})],['Type',obj({text:string,loc,label:integer})],['Move',obj({loc,label:integer,drag:boolean})],
 ['Scroll',obj({loc,label:integer,type:string,direction:string,wheel_times:integer})],['Shortcut',{...obj({shortcut:string}),required:['shortcut']}],
 ['Wait',obj({duration:integer})],['WaitFor',obj({condition:string,text:string,window_name:string,timeout:{type:'number'}})],
 ...['MultiSelect','MultiEdit','PowerShell','FileSystem','Registry','Process','Clipboard','Scrape','Notification'].map(n=>[n,obj({})])
].map(([name,inputSchema])=>({name,inputSchema,description:`Fixture ${name}`}));
const windowsText='Cursor Position: (1,2)\nFocused Window:\nNo active window found\n\nOpened Windows:\nName               Depth  Status      Width    Height    Handle\n-----------------  -----  --------  -------  --------  --------\nEditor 中文            0  Normal        800       600      5411\nBrowser                0  Normal       1024       768      5412\n\nUI Tree:\nEditor not-a-window';
let state;
function setup(env={}) {
 for(const k of Object.keys(process.env)) if(k.startsWith('CODEXPRO_WINDOWS_')) delete process.env[k];
 Object.assign(process.env,{CODEXPRO_WINDOWS_ENABLED:'1',CODEXPRO_WINDOWS_BRIDGE_URL:'http://127.0.0.1:28732/mcp',CODEXPRO_WINDOWS_BRIDGE_TOKEN:'x'.repeat(32),CODEXPRO_WINDOWS_PROFILE:'desktop_ui'},env);
 state={connects:0,closes:0,lists:0,calls:[],tools:structuredClone(inventory),failure:false,malformed:false};
 Client.prototype.connect=async function(transport){state.connects++;state.transport=transport;await new Promise(r=>setImmediate(r));};
 Client.prototype.getServerVersion=()=>({name:'windows-mcp',version:'4.0.3'});
 Client.prototype.close=async function(){state.closes++;this.onclose?.();};
 Client.prototype.ping=async()=>({});
 Client.prototype.listTools=async()=>{state.lists++;return {tools:state.tools};};
 Client.prototype.callTool=async req=>{
  state.calls.push(structuredClone(req));
  if(state.failure) throw new Error('injected connection failure');
  if(req.name==='Snapshot') return {content:[{type:'text',text:state.malformed?'changed format':windowsText}]};
  if(req.name==='Screenshot') return {content:[{type:'text',text:'desktop metadata'},{type:'image',mimeType:'image/png',data:'aGVsbG8='}],structuredContent:{width:800,height:600}};
  return {content:[{type:'text',text:'fixture ok'}]};
 };
 const handlers=new Map();registerWindowsBridgeTools({registerTool:(n,_d,h)=>handlers.set(n,h)},{connectionTest:false});
 return(name,args={})=>handlers.get(name)(args);
}
const cases=[];const test=(n,f)=>cases.push([n,f]);
function success(r){assert.notEqual(r.isError,true,JSON.stringify(r));return r.structuredContent;}
test('disabled ignores stale endpoint and never connects',async()=>{const c=setup({CODEXPRO_WINDOWS_ENABLED:'0'});const r=success(await c('windows_backend_status'));assert.equal(r.status,'disabled');assert.equal(r.enabled,false);assert.equal(r.reachable,false);assert.equal(state.connects,0);assert.equal((await c('windows_call',{tool:'Click',arguments:{loc:[1,2]}})).isError,true);assert.equal(state.calls.length,0);assert.equal(state.connects,0);});
test('unset bridge never probes another project default port',async()=>{const c=setup({CODEXPRO_WINDOWS_ENABLED:'',CODEXPRO_WINDOWS_BRIDGE_URL:'',CODEXPRO_WINDOWS_BRIDGE_TOKEN:''});assert.equal(success(await c('windows_backend_status')).status,'disabled');assert.equal(state.connects,0);});
test('enabled bridge missing project endpoint never guesses default port',async()=>{const c=setup({CODEXPRO_WINDOWS_BRIDGE_URL:''});assert.equal(success(await c('windows_backend_status')).status,'configuration_error');assert.equal(state.connects,0);});
test('enabled missing credential is configuration_error',async()=>{const c=setup({CODEXPRO_WINDOWS_BRIDGE_TOKEN:''});assert.equal(success(await c('windows_backend_status')).status,'configuration_error');assert.equal(state.connects,0);});
test('remote endpoint cannot receive credentials',async()=>{const c=setup({CODEXPRO_WINDOWS_BRIDGE_URL:'https://example.com/mcp'});const r=await c('windows_backend_status');assert.equal(state.connects,0);assert.ok(r.isError||r.structuredContent.status==='configuration_error');});
test('current native UI names allowed',async()=>{const c=setup();for(const tool of ['Screenshot','Move','Scroll','Shortcut','WaitFor','MultiSelect','MultiEdit'])success(await c('windows_call',{tool}));assert.equal(state.calls.length,7);});
test('system and network tools remain denied',async()=>{const c=setup();for(const tool of ['PowerShell','FileSystem','Registry','Process','Clipboard','Scrape','Notification'])assert.equal((await c('windows_call',{tool})).isError,true);assert.equal(state.calls.length,0);});
test('system_full native behavior preserved',async()=>{const c=setup({CODEXPRO_WINDOWS_PROFILE:'system_full'});success(await c('windows_call',{tool:'Registry'}));assert.equal(state.calls.length,1);});
test('discovery preserves native schema allowed flags and separate aliases',async()=>{const c=setup();const r=success(await c('windows_list_tools'));assert.equal(r.tool_count,19);assert.deepEqual(r.tools.find(t=>t.name==='Shortcut').inputSchema,inventory.find(t=>t.name==='Shortcut').inputSchema);assert.equal(r.tools.find(t=>t.name==='Shortcut').call_allowed,true);assert.equal(r.tools.find(t=>t.name==='Registry').call_allowed,false);assert.ok(r.compatibility_aliases.some(t=>t.name==='SearchWindow'&&t.native_tool==='Snapshot'&&t.inputSchema));});
test('HotKey translates explicit combination once',async()=>{const c=setup();success(await c('windows_call',{tool:'HotKey',arguments:{keys:['ctrl','shift','s']}}));assert.deepEqual(state.calls,[{name:'Shortcut',arguments:{shortcut:'ctrl+shift+s'}}]);});
test('MouseMove coordinates never become dragging',async()=>{const c=setup();success(await c('windows_call',{tool:'MouseMove',arguments:{x:120,y:80}}));assert.deepEqual(state.calls,[{name:'Move',arguments:{loc:[120,80],drag:false}}]);});
test('MouseScroll explicit direction and wheel count',async()=>{const c=setup();success(await c('windows_call',{tool:'MouseScroll',arguments:{direction:'up',wheel_times:3}}));assert.deepEqual(state.calls,[{name:'Scroll',arguments:{type:'vertical',direction:'up',wheel_times:3}}]);});
test('DoubleClick enforces two clicks',async()=>{const c=setup();success(await c('windows_call',{tool:'DoubleClick',arguments:{loc:[10,20]}}));assert.deepEqual(state.calls,[{name:'Click',arguments:{loc:[10,20],button:'left',clicks:2}}]);});
test('ambiguous compatibility arguments never execute',async()=>{const c=setup();for(const [tool,args] of [['MouseMove',{x:1,y:2,loc:[3,4]}],['MouseMove',{x:1}],['MouseMove',{loc:[1,2],drag:true}],['MouseScroll',{delta:120}],['HotKey',{keys:['ctrl','c'],shortcut:'alt+f4'}],['DoubleClick',{loc:[1,2],clicks:3}],['SearchWindow',{query:'Editor',mode:'switch'}]])assert.equal((await c('windows_call',{tool,arguments:args})).isError,true);assert.equal(state.calls.length,0);});
test('SearchWindow readonly title search excludes unrelated UI tree',async()=>{const c=setup();const r=success(await c('windows_call',{tool:'SearchWindow',arguments:{query:'editor'}}));assert.equal(r.native_tool,'Snapshot');assert.equal(r.result.matches.length,1);assert.equal(r.result.matches[0].name,'Editor 中文');assert.equal(r.result.matches[0].handle,'5411');assert.deepEqual(state.calls,[{name:'Snapshot',arguments:{use_vision:false,use_ui_tree:false,use_dom:false}}]);});
test('changed window format is error not empty success',async()=>{const c=setup();state.malformed=true;assert.equal((await c('windows_call',{tool:'SearchWindow',arguments:{query:'Editor'}})).isError,true);assert.equal(state.calls.length,1);});
test('native legacy names take precedence over adapters',async()=>{const c=setup();state.tools.push({name:'MouseMove',inputSchema:obj({x:integer,y:integer})});success(await c('windows_call',{tool:'MouseMove',arguments:{x:1,y:2}}));assert.deepEqual(state.calls,[{name:'MouseMove',arguments:{x:1,y:2}}]);});
test('missing adapter target refuses',async()=>{const c=setup();state.tools=state.tools.filter(t=>t.name!=='Move');assert.equal((await c('windows_call',{tool:'MouseMove',arguments:{x:1,y:2}})).isError,true);assert.equal(state.calls.length,0);});
test('target schema drift refuses adapter',async()=>{const c=setup();state.tools.find(t=>t.name==='Shortcut').inputSchema=obj({new_keys:string});assert.equal((await c('windows_call',{tool:'HotKey',arguments:{keys:['ctrl','c']}})).isError,true);assert.equal(state.calls.length,0);});
test('Screenshot preserves image and structured result',async()=>{const c=setup();const r=await c('windows_call',{tool:'Screenshot'});success(r);assert.ok(r.content.some(b=>b.type==='image'&&b.data==='aGVsbG8='));assert.equal(r.structuredContent.result.width,800);});
test('concurrent discovery shares one connection',async()=>{const c=setup();await Promise.all(Array.from({length:6},()=>c('windows_list_tools')));assert.equal(state.connects,1);});
test('TTL does not discard a healthy client',async()=>{const c=setup();await c('windows_backend_status');const now=Date.now;try{Date.now=()=>now()+11000;await c('windows_backend_status');assert.equal(state.connects,1);}finally{Date.now=now;}});
test('no expiring connection-age abort signal',async()=>{const c=setup();await c('windows_backend_status');assert.equal(state.transport._requestInit?.signal,undefined);});
test('failed mutation never replayed',async()=>{const c=setup();state.failure=true;assert.equal((await c('windows_call',{tool:'Click',arguments:{loc:[1,2]}})).isError,true);assert.equal(state.calls.length,1);});
let failed=0;
try{for(const[n,f]of cases){try{await f();console.log(`PASS ${n}`);}catch(e){failed++;console.error(`FAIL ${n}: ${e.message}`);}}}finally{for(const[k,v]of Object.entries(original))Client.prototype[k]=v;for(const k of Object.keys(process.env))if(k.startsWith('CODEXPRO_WINDOWS_'))delete process.env[k];for(const[k,v]of Object.entries(savedEnv))if(k.startsWith('CODEXPRO_WINDOWS_'))process.env[k]=v;}
console.log(JSON.stringify({suite:'windows-bridge-contract',total:cases.length,passed:cases.length-failed,failed}));if(failed)process.exitCode=1;
