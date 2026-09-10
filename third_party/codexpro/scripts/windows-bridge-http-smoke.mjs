// Real loopback HTTP/SDK integration; does not invoke Windows or touch the desktop.
import assert from 'node:assert/strict';
import http from 'node:http';
import { once } from 'node:events';
import { registerWindowsBridgeTools } from '../dist/windowsBridge.js';
const before = { ...process.env };
const credential = 'f'.repeat(32);
const tools = [
  {name:'Snapshot',inputSchema:{type:'object',properties:{use_vision:{type:'boolean'},use_ui_tree:{type:'boolean'},use_dom:{type:'boolean'}},additionalProperties:false}},
  {name:'Move',inputSchema:{type:'object',properties:{loc:{type:'array',items:{type:'integer'}},label:{type:'integer'},drag:{type:'boolean'}},additionalProperties:false}},
  {name:'Screenshot',inputSchema:{type:'object',properties:{}}},
  {name:'Registry',inputSchema:{type:'object',properties:{}}}
];
const actions = [];
let initialized = 0, failMove = false, pageRequests = 0;
const server = http.createServer(async (request, response) => {
  if (request.headers.authorization !== `Bearer ${credential}`) { response.writeHead(401).end(); return; }
  if (request.method !== 'POST') { response.writeHead(405).end(); return; }
  let raw = '';
  for await (const chunk of request) raw += chunk;
  let rpc;
  try { rpc = JSON.parse(raw); } catch { response.writeHead(400).end(); return; }
  if (rpc.id === undefined) { response.writeHead(202).end(); return; }
  const send = result => { response.writeHead(200, {'Content-Type':'application/json'}); response.end(JSON.stringify({jsonrpc:'2.0',id:rpc.id,result})); };
  if (rpc.method === 'initialize') {
    initialized++;
    send({protocolVersion:'2025-11-25',serverInfo:{name:'fixture-windows-mcp',version:'4.0.3'},capabilities:{tools:{listChanged:false}}});
  } else if (rpc.method === 'ping') {
    send({});
  } else if (rpc.method === 'tools/list') {
    pageRequests++;
    send(rpc.params?.cursor ? {tools:tools.slice(2)} : {tools:tools.slice(0,2),nextCursor:'page-2'});
  } else if (rpc.method === 'tools/call') {
    actions.push(rpc.params);
    if (rpc.params.name === 'Move' && failMove) { response.writeHead(500).end('injected failure after receiving action'); return; }
    if (rpc.params.name === 'Snapshot') {
      send({content:[{type:'text',text:'Opened Windows:\nName       Depth  Status    Width  Height  Handle\n---------  -----  ------  -------  ------  ------\nFixture A      0  Normal       800     600    1234\n\nUI Tree:\nNo elements found.'}]});
    } else if (rpc.params.name === 'Screenshot') {
      send({content:[{type:'image',mimeType:'image/png',data:'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jWZkAAAAASUVORK5CYII='}]});
    } else {
      send({content:[{type:'text',text:'ok'}]});
    }
  } else {
    response.writeHead(200, {'Content-Type':'application/json'});
    response.end(JSON.stringify({jsonrpc:'2.0',id:rpc.id,error:{code:-32601,message:'Method not found'}}));
  }
});
server.listen(0, '127.0.0.1');
await once(server, 'listening');
const port = server.address().port;
for (const k of Object.keys(process.env)) if(k.startsWith('CODEXPRO_WINDOWS_')) delete process.env[k];
Object.assign(process.env,{CODEXPRO_WINDOWS_ENABLED:'1',CODEXPRO_WINDOWS_BRIDGE_URL:`http://127.0.0.1:${port}/mcp`,CODEXPRO_WINDOWS_BRIDGE_TOKEN:credential,CODEXPRO_WINDOWS_PROFILE:'desktop_ui',CODEXPRO_WINDOWS_CALL_TIMEOUT_MS:'1000'});
const handlers = new Map();
registerWindowsBridgeTools({registerTool:(name,_descriptor,handler)=>handlers.set(name,handler)}, {connectionTest:false});
const call = (name,args={})=>handlers.get(name)(args);
function good(result) { assert.notEqual(result.isError,true,JSON.stringify(result)); return result.structuredContent; }
try {
  const results = await Promise.all([call('windows_list_tools'),call('windows_list_tools'),call('windows_backend_status')]);
  assert.equal(good(results[0]).tool_count,4);
  assert.equal(good(results[1]).tool_count,4);
  assert.equal(good(results[2]).status,'connected');
  assert.equal(initialized,1);
  assert.equal(pageRequests,2);
  const match=good(await call('windows_call',{tool:'SearchWindow',arguments:{name:'Fixture'}}));
  assert.equal(match.result.matches[0].handle,'1234');
  assert.deepEqual(actions[0],{name:'Snapshot',arguments:{use_vision:false,use_ui_tree:false,use_dom:false}});
  const image=await call('windows_call',{tool:'Screenshot'});good(image);
  assert.equal(image.content[0].type,'image');
  // A later request must not inherit an abort signal that expired with connection age.
  await new Promise(r=>setTimeout(r,1200));
  good(await call('windows_call',{tool:'MouseMove',arguments:{loc:[1,2]}}));
  assert.equal(initialized,1);
  const count=actions.length;
  assert.equal((await call('windows_call',{tool:'Registry'})).isError,true);
  assert.equal(actions.length,count);
  failMove=true;
  assert.equal((await call('windows_call',{tool:'MouseMove',arguments:{loc:[3,4]}})).isError,true);
  assert.equal(actions.length,count+1);
  console.log(JSON.stringify({suite:'windows-bridge-http',passed:true,initialized,pageRequests,nativeActions:actions.length,mutatingFailureAttempts:1,ageTimeoutRecovered:true}));
} finally {
  server.closeAllConnections();
  await new Promise(resolve=>server.close(resolve));
  for(const k of Object.keys(process.env))if(k.startsWith('CODEXPRO_WINDOWS_'))delete process.env[k];
  for(const[k,v]of Object.entries(before))if(k.startsWith('CODEXPRO_WINDOWS_'))process.env[k]=v;
}
