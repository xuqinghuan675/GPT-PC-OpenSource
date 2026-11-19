import fs from "node:fs";
import { createHash } from "node:crypto";

const LOOPBACK_HOSTS = new Set(["127.0.0.1", "::1", "localhost"]);
const MAX_INPUT_BYTES = 2 * 1024 * 1024;
const ACTIONS = new Set(["read", "prepare_probe", "proof_probe", "admission", "start", "receipt"]);
const MACHINE_CONFIG_URL = new URL("../config/machine.local.toml", import.meta.url);

function configuredPluginHint() {
  let text;
  try { text = fs.readFileSync(MACHINE_CONFIG_URL, "utf8"); }
  catch { return ""; }
  const match = text.match(/^\s*plugin_app_id\s*=\s*"(asdk_app_[a-f0-9]{32})"\s*(?:#.*)?$/m);
  return match ? "plugin:" + match[1] : "";
}
function normalizePluginHint(request) {
  const payload = request?.args?.payload;
  if (!payload || !["prepare_probe","proof_probe","admission","start"].includes(request.action)) return true;
  const pluginHint = configuredPluginHint();
  if (!pluginHint) return false;
  const hints = Array.isArray(payload.system_hints) ? payload.system_hints.filter(item => typeof item === "string") : [];
  payload.system_hints = [...hints.filter(item => !item.startsWith("plugin:asdk_app_")), pluginHint];
  return true;
}

const PAGE_RUNTIME = String.raw`
const SENTINEL_SDK = "https://chatgpt.com/backend-api/sentinel/sdk.js";
const SENTINEL_PREPARE = "/backend-api/sentinel/chat-requirements/prepare";
const SENTINEL_FINALIZE = "/backend-api/sentinel/chat-requirements/finalize";
const CONVERSATION_PREPARE = "/backend-api/f/conversation/prepare";
const CONVERSATION = "/backend-api/f/conversation";

function bridgeError(code, message, httpStatus=0) {
  const error = new Error(message);
  error.code = code;
  error.httpStatus = httpStatus;
  return error;
}
function utf8Base64(value) {
  const bytes = new TextEncoder().encode(String(value));
  let binary = "";
  for (let i=0;i<bytes.length;i+=0x8000) binary += String.fromCharCode(...bytes.subarray(i,i+0x8000));
  return btoa(binary);
}
function utf8FromBase64(value) {
  const binary=atob(value);
  return new TextDecoder().decode(Uint8Array.from(binary,ch=>ch.charCodeAt(0)));
}
function xorText(text,key) {
  if(!key)return text;
  let out="";
  for(let i=0;i<text.length;i++) out+=String.fromCharCode(text.charCodeAt(i)^key.charCodeAt(i%key.length));
  return out;
}
function proofConfig() {
  const globals=["window","document","navigator","performance","crypto","fetch"];
  const signals=[
    "webdriver−"+String(Boolean(navigator.webdriver)),
    "vendor−"+(navigator.vendor||""),
    "language−"+(navigator.language||""),
    "hardwareConcurrency−"+(navigator.hardwareConcurrency||0),
  ];
  return [
    screen.availHeight||screen.height||0,new Date().toString(),4294705152,1,navigator.userAgent,
    SENTINEL_SDK,"",navigator.language||"en-US",Array.from(navigator.languages||[navigator.language||"en-US"]).join(","),
    Math.random(),signals[Math.floor(Math.random()*signals.length)],"location",
    globals[Math.floor(Math.random()*globals.length)],performance.now(),crypto.randomUUID(),"",
    navigator.hardwareConcurrency||8,Math.round(performance.timeOrigin||Date.now()-performance.now()),0,0,0,0,0,0,0,
  ];
}

class TurnstileOrderedMap {
  constructor(){this.keys=[];this.values=new Map();}
  add(key,value){if(!this.values.has(key))this.keys.push(key);this.values.set(key,value);}
}
function turnstileString(value) {
  if(value==null)return "undefined";
  if(typeof value==="number")return String(value);
  if(typeof value==="string"){
    return new Map([
      ["window.Math","[object Math]"],["window.Reflect","[object Reflect]"],
      ["window.performance","[object Performance]"],["window.localStorage","[object Storage]"],
      ["window.Object","function Object() { [native code] }"],["window.Reflect.set","function set() { [native code] }"],
      ["window.performance.now","function () { [native code] }"],["window.Object.create","function create() { [native code] }"],
      ["window.Object.keys","function keys() { [native code] }"],["window.Math.random","function random() { [native code] }"],
    ]).get(value)??value;
  }
  if(Array.isArray(value)&&value.every(item=>typeof item==="string"))return value.join(",");
  return String(value);
}
function solveTurnstile(dx,requirementsToken) {
  if(!dx)return "";
  let tokens;
  try{tokens=JSON.parse(xorText(utf8FromBase64(dx),requirementsToken));}catch{return "";}
  if(!Array.isArray(tokens))return "";
  const values=new Map(),started=performance.now();let result="";
  const get=(key,fallback=null)=>values.has(key)?values.get(key):fallback;
  const set=(key,value)=>values.set(key,value);
  const property=(instance,key)=>{
    const name=turnstileString(key);
    if(instance instanceof TurnstileOrderedMap)return instance.values.get(name);
    if(instance&&instance.constructor===Object)return instance[name];
    if(Array.isArray(instance)){const index=Number.parseInt(name,10);return Number.isInteger(index)?instance[index]:undefined;}
    if(typeof instance==="string"&&name==="location"&&instance==="window.document")return "https://chatgpt.com/";
    if(typeof instance==="string"&&name&&name!=="undefined"&&name!=="None")return instance+"."+name;
    return null;
  };
  const call=(target,args)=>{
    switch(target){
      case "window.performance.now":return performance.now()-started+Math.random();
      case "window.Object.create":return new TurnstileOrderedMap();
      case "window.Object.keys":{
        const item=args[0];
        if(item==="window.localStorage")return Object.keys(localStorage);
        if(item instanceof TurnstileOrderedMap)return [...item.keys];
        if(item&&item.constructor===Object)return Object.keys(item);
        return null;
      }
      case "window.Math.random":return Math.random();
      case "window.Reflect.set":{
        const [object,key,value]=args;
        if(object instanceof TurnstileOrderedMap){object.add(String(key),value);return true;}
        if(object&&object.constructor===Object){object[String(key)]=value;return true;}
        return false;
      }
      default:return typeof target==="function"?target(...args):null;
    }
  };
  const runQueue=(limit=20000)=>{
    let steps=0;
    while(Array.isArray(values.get(9))&&values.get(9).length){
      if(++steps>limit)throw new Error("turnstile_vm_step_limit");
      const token=values.get(9).shift();
      if(!Array.isArray(token)||!token.length)continue;
      const operation=values.get(Number(token[0]));
      if(typeof operation!=="function")continue;
      try{operation(...token.slice(1));}catch{}
    }
  };
  const finite=value=>Number.isFinite(value)?value:0;
  values.set(1,(a,b)=>set(a,xorText(turnstileString(get(a)),turnstileString(get(b)))));
  values.set(2,(a,b)=>set(a,b));
  values.set(3,a=>{result=utf8Base64(a);});
  values.set(5,(a,b)=>{
    const current=get(a),incoming=get(b);
    if(Array.isArray(current))set(a,[...current,incoming]);
    else if(["string","number"].includes(typeof current)||["string","number"].includes(typeof incoming))set(a,turnstileString(current)+turnstileString(incoming));
    else set(a,"NaN");
  });
  values.set(6,(a,b,c)=>set(a,property(get(b),get(c))));
  values.set(7,(a,...rest)=>call(get(a),rest.map(get)));
  values.set(8,(a,b)=>set(a,values.get(b)));
  values.set(9,tokens);values.set(10,"window");values.set(11,a=>set(a,null));values.set(12,a=>set(a,values));
  values.set(13,(a,b,...rest)=>{try{call(get(b),rest);}catch(error){set(a,String(error));}});
  values.set(14,(a,b)=>set(a,JSON.parse(values.get(b))));
  values.set(15,(a,b)=>set(a,JSON.stringify(values.get(b))));
  values.set(16,requirementsToken);
  values.set(17,(a,b,...rest)=>set(a,call(get(b),rest.map(get))));
  values.set(18,a=>set(a,utf8FromBase64(turnstileString(values.get(a)))));
  values.set(19,(a,b)=>set(a,utf8Base64(turnstileString(values.get(a)))));
  values.set(20,(a,b,c,...rest)=>{if(get(a)===get(b)&&typeof get(c)==="function")get(c)(...rest);});
  values.set(21,(a,b,c,d,...rest)=>{if(Math.abs(finite(Number(get(a))-Number(get(b))))>Math.abs(finite(Number(get(c))))&&typeof get(d)==="function")get(d)(...rest);});
  values.set(22,(a,b)=>{const previous=[...(values.get(9)??[])];values.set(9,[...(b??[])]);runQueue();set(a,"None");values.set(9,previous);});
  values.set(23,(a,b,...rest)=>{if(get(a)!=null&&typeof get(b)==="function")get(b)(...rest);});
  values.set(24,(a,b,c)=>set(a,property(get(b),get(c))));
  values.set(25,()=>{});values.set(26,()=>{});
  values.set(27,(a,b)=>{const current=get(a),incoming=get(b);if(Array.isArray(current)){const i=current.indexOf(incoming);if(i>=0)current.splice(i,1);}else{const next=current-incoming;set(a,Number.isNaN(next)?0:next);}});
  values.set(28,()=>{});values.set(29,(a,b,c)=>{try{set(a,get(b)<get(c));}catch{set(a,false);}});
  values.set(30,(a,_b,c,d=null)=>{
    const captures=Array.isArray(d),keys=captures?c:[],program=[...((captures?d:c)??[])];
    set(a,(...args)=>{const previous=[...(values.get(9)??[])];if(captures)keys.forEach((key,i)=>{if(i<args.length)set(key,args[i]);});values.set(9,[...program]);runQueue();values.set(9,previous);});
  });
  values.set(33,(a,b,c)=>set(a,finite(Number(get(b))*Number(get(c)))));
  values.set(34,(a,b)=>set(a,get(b)));
  try{runQueue();}catch{return "";}
  return result;
}

async function sessionInfo(expectedAccountTag="",timeoutMs=12000) {
  const response=await fetch("/api/auth/session",{credentials:"include",cache:"no-store",signal:AbortSignal.timeout(timeoutMs)});
  if(!response.ok)throw bridgeError("AUTH_REQUIRED","ChatGPT session is unavailable",response.status);
  const session=await response.json();
  if(!session.accessToken||!session.user?.id)throw bridgeError("AUTH_REQUIRED","ChatGPT session has no access token");
  const hash=await crypto.subtle.digest("SHA-256",new TextEncoder().encode(session.user.id));
  const accountTag=Array.from(new Uint8Array(hash)).map(x=>x.toString(16).padStart(2,"0")).join("");
  if(expectedAccountTag&&accountTag!==expectedAccountTag)throw bridgeError("AUTH_ACCOUNT_CHANGED","The logged-in ChatGPT account changed");
  return {token:session.accessToken,accountTag};
}
function targetHeaders(token,path,traceId="") {
  const headers={Authorization:"Bearer "+token,"Content-Type":"application/json",Accept:"*/*","x-openai-target-path":path,"x-openai-target-route":path};
  if(traceId)headers["x-oai-turn-trace-id"]=traceId;
  return headers;
}
async function jsonPost(path,body,token,traceId="") {
  const response=await fetch(path,{method:"POST",credentials:"include",cache:"no-store",headers:targetHeaders(token,path,traceId),body:JSON.stringify(body),signal:AbortSignal.timeout(12000)});
  const text=await response.text();let data=null;try{data=text?JSON.parse(text):{};}catch{}
  if(!response.ok||!data)throw bridgeError("WEB_PREPARE_REJECTED","ChatGPT "+path+" returned HTTP "+response.status,response.status);
  return data;
}
function clientContext() {
  return {app_name:"chatgpt.com",page_width:window.innerWidth,page_height:window.innerHeight,screen_width:screen.width,screen_height:screen.height,pixel_ratio:devicePixelRatio,
    has_web_push_capabilities:"PushManager" in window,web_push_notification_permission:typeof Notification==="undefined"?"default":Notification.permission};
}
function conversationPreparePayload(payload) {
  const message=(payload.messages||[]).at(-1);
  const body={action:payload.action||"next",conversation_id:payload.conversation_id,parent_message_id:payload.parent_message_id||"client-created-root",model:payload.model,
    client_prepare_state:"success",client_prepare_dispatch:"immediate",client_prepare_source:"context_change",timezone_offset_min:payload.timezone_offset_min,timezone:payload.timezone,
    conversation_mode:payload.conversation_mode||{kind:"primary_assistant"},system_hints:payload.system_hints||[],
    partial_query:message?{id:message.id,author:message.author,content:message.content}:undefined,supports_buffering:payload.supports_buffering!==false,
    supported_encodings:payload.supported_encodings||["v1"],client_contextual_info:clientContext(),local_function_names:payload.local_function_names||[]};
  if(payload.thinking_effort)body.thinking_effort=payload.thinking_effort;
  return body;
}

async function pagePrepare(args) {
  if(location.origin!=="https://chatgpt.com")throw bridgeError("ORIGIN_MISMATCH","Not on chatgpt.com");
  const auth=await sessionInfo(args.account_tag||"");
  const payload=structuredClone(args.payload||{});
  payload.timezone=Intl.DateTimeFormat().resolvedOptions().timeZone;
  payload.timezone_offset_min=new Date().getTimezoneOffset();
  payload.client_contextual_info={...(payload.client_contextual_info||{}),...clientContext()};
  const traceId=crypto.randomUUID();
  const conduitData=await jsonPost(CONVERSATION_PREPARE,conversationPreparePayload(payload),auth.token,traceId);
  const conduitToken=String(conduitData?.conduit_token||"").trim();
  if(!conduitToken)throw bridgeError("CONDUIT_TOKEN_MISSING","Conversation prepare returned no conduit token");
  const config=proofConfig(),p="gAAAAAC"+utf8Base64(JSON.stringify(config));
  const prepared=await jsonPost(SENTINEL_PREPARE,{p},auth.token);
  const prepareToken=String(prepared?.prepare_token||"").trim();
  if(!prepareToken)throw bridgeError("SENTINEL_PREPARE_TOKEN_MISSING","Sentinel prepare returned no token");
  if(prepared?.arkose?.required===true)throw bridgeError("SENTINEL_ARKOSE_REQUIRED","Interactive Arkose verification is required");
  return {
    account_tag:auth.accountTag,payload,trace_id:traceId,conduit_token:conduitToken,prepare_token:prepareToken,
    p,proof_config:config,pow_required:prepared?.proofofwork?.required===true,pow_seed:String(prepared?.proofofwork?.seed||""),
    pow_difficulty:String(prepared?.proofofwork?.difficulty||""),turnstile_required:prepared?.turnstile?.required===true,
    turnstile_dx:String(prepared?.turnstile?.dx||""),
  };
}
async function finalizeBundle(prepared,proofToken) {
  const auth=await sessionInfo(prepared.account_tag);
  const turnstileToken=prepared.turnstile_required?solveTurnstile(prepared.turnstile_dx,prepared.p):"";
  if(prepared.turnstile_required&&!turnstileToken)throw bridgeError("SENTINEL_TURNSTILE_UNSOLVED","The current server-issued Turnstile VM could not be evaluated");
  const body={prepare_token:prepared.prepare_token};
  if(proofToken)body.proof_token=proofToken;
  if(turnstileToken)body.turnstile_token=turnstileToken;
  const finalized=await jsonPost(SENTINEL_FINALIZE,body,auth.token);
  const token=String(finalized?.token||"").trim();
  if(!token)throw bridgeError("SENTINEL_FINAL_TOKEN_MISSING","Sentinel finalize returned no token");
  return {auth,token,proofToken,turnstileToken,soToken:String(finalized?.so_token||"").trim()};
}
async function pageFinalizeOnly(args) {
  try{
    await finalizeBundle(args.prepared,args.proof_token||"");
    return {status:"ready",account_tag:args.prepared.account_tag,conduit:true,sentinel:true,pow_required:!!args.prepared.pow_required,turnstile_required:!!args.prepared.turnstile_required,arkose_required:false};
  }catch(error){
    return {status:"rejected",error:error?.code||"WEB_ADMISSION_FAILED",http_status:error?.httpStatus||0};
  }
}
async function pageStartPrepared(args) {
  const receipts=window.__GPTAGENT_ULTRA_RECEIPTS||=new Map();
  if(receipts.has(args.operation_id))return {...receipts.get(args.operation_id)};
  if(receipts.size>=64)return {status:"rejected",error:"PAGE_RECEIPT_CAPACITY"};
  const prepared=args.prepared||{};
  const state={status:"dispatching",conversation_id:null,user_message_id:prepared.payload?.messages?.[0]?.id||null,http_status:null,mutation_started:false};
  receipts.set(args.operation_id,state);
  void(async()=>{
    try{
      const bundle=await finalizeBundle(prepared,args.proof_token||"");
      const headers={Authorization:"Bearer "+bundle.auth.token,"Content-Type":"application/json",Accept:"text/event-stream","Oai-Language":navigator.language||"en-US",
        "Oai-Session-Id":crypto.randomUUID(),"x-conduit-token":prepared.conduit_token,"x-oai-turn-trace-id":prepared.trace_id,
        "x-openai-target-path":CONVERSATION,"x-openai-target-route":CONVERSATION,"openai-sentinel-chat-requirements-token":bundle.token};
      if(bundle.proofToken)headers["openai-sentinel-proof-token"]=bundle.proofToken;
      if(bundle.turnstileToken)headers["openai-sentinel-turnstile-token"]=bundle.turnstileToken;
      if(bundle.soToken)headers["openai-sentinel-so-token"]=bundle.soToken;
      const deviceId=localStorage.getItem("oai-did")||"";if(deviceId)headers["Oai-Device-Id"]=deviceId;
      state.mutation_started=true;
      const response=await fetch(CONVERSATION,{method:"POST",credentials:"include",cache:"no-store",headers,body:JSON.stringify(prepared.payload)});
      state.http_status=response.status;
      if(!response.ok){Object.assign(state,{status:"rejected",error:[401,403].includes(response.status)?"WEB_ADMISSION_REQUIRED":"WEB_SEND_REJECTED"});return;}
      if(!response.body){Object.assign(state,{status:"unknown",error:"WEB_STREAM_MISSING"});return;}
      const reader=response.body.getReader(),decoder=new TextDecoder();let buffer="";
      const findConversation=(value,depth=0)=>{
        if(!value||typeof value!=="object"||depth>8)return "";
        if(typeof value.conversation_id==="string")return value.conversation_id;
        for(const child of Object.values(value)){const found=findConversation(child,depth+1);if(found)return found;}return "";
      };
      try{
        while(true){
          const {value,done}=await reader.read();if(done)break;
          buffer+=decoder.decode(value,{stream:true});
          if(buffer.length>2*1024*1024){Object.assign(state,{status:"unknown",error:"WEB_EVENT_TOO_LARGE"});break;}
          let nl;while((nl=buffer.indexOf("\n"))>=0){
            const line=buffer.slice(0,nl).trim();buffer=buffer.slice(nl+1);
            if(!line.startsWith("data:"))continue;const text=line.slice(5).trim();if(!text||text==="[DONE]")continue;
            try{const cid=findConversation(JSON.parse(text));if(cid){state.conversation_id=cid;state.status="accepted";}}catch{}
          }
        }
      }finally{reader.releaseLock();}
      state.stream_ended=true;
      if(!state.conversation_id)Object.assign(state,{status:"unknown",error:"CONVERSATION_ID_NOT_OBSERVED"});
    }catch(error){
      Object.assign(state,{status:state.conversation_id?"accepted":(state.mutation_started?"unknown":"rejected"),error:error?.code||"WEB_SEND_NETWORK_UNKNOWN",http_status:error?.httpStatus||state.http_status||0});
    }
  })();
  return {...state};
}
function pageReceipt(args) {
  return {...((window.__GPTAGENT_ULTRA_RECEIPTS||new Map()).get(args.operation_id)||{status:"unknown",error:"PAGE_RECEIPT_MISSING"})};
}

async function pageRead(args) {
  if(location.origin!=="https://chatgpt.com")return {error:"ORIGIN_MISMATCH"};
  // BrowserWebTransport historically gave Runtime.evaluate a 25s command
  // budget while this read path could spend 12s in sessionInfo and another
  // 12s in the backend GET. Renderer scheduling/JSON work then pushed the
  // outer CDP command over 25s, producing the misleading
  // "CDP command timeout: Runtime.evaluate". Keep the page-side read budget
  // comfortably below the outer command budget; mutation/preflight paths keep
  // their existing 12s request budget.
  const auth=await sessionInfo("",8000);
  const response=await fetch(args.path,{credentials:"include",cache:"no-store",headers:{Authorization:"Bearer "+auth.token},signal:AbortSignal.timeout(10000)});
  if(!response.ok)return {error:"WEB_READ_REJECTED",http_status:response.status,retry_after:response.headers.get("retry-after"),account_tag:auth.accountTag};
  let data;try{data=await response.json();}catch{return {error:"WEB_READ_INVALID",http_status:response.status};}
  if(args.kind==="conversation"){
    const entries=Object.entries(data.mapping||{}),mapping={};
    for(const [id,node] of entries.slice(-10000)){
      const message=node.message||{},type=message.content?.content_type;
      const visible=["user","assistant"].includes(message.author?.role)&&!message.metadata?.is_visually_hidden_from_conversation&&!message.hidden&&!["analysis","justify","confidence"].includes(message.channel)&&["text","multimodal_text"].includes(type);
      mapping[id]={parent:node.parent,children:node.children||[],message:{id:message.id,role:message.author?.role,status:message.status,end_turn:message.end_turn===true,channel:message.channel,
        content_type:type,text:visible?(message.content?.parts||[]).filter(x=>typeof x==="string").join("\n").slice(0,64000):"",model:message.metadata?.model_slug,effort:message.metadata?.thinking_effort}};
    }
    data={conversation_id:data.conversation_id||data.id,current_node:data.current_node,project_id:data.gizmo_id||data.project_id,mapping,truncated:entries.length>10000};
  }else if(args.kind==="projects"){
    if(!Array.isArray(data.items))return {error:"PROJECT_SCHEMA_CHANGED"};
    data={items:data.items.map(x=>{const g=x.gizmo?.gizmo||x.gizmo;return {id:g?.id,name:g?.display?.name,type:g?.gizmo_type,short_url:g?.short_url};}),cursor:data.cursor??null};
  }else if(args.kind==="project_chats"){
    if(!Array.isArray(data.items))return {error:"PROJECT_CHAT_SCHEMA_CHANGED"};
    data={items:data.items.map(x=>({id:x.id||x.conversation_id,create_time:x.create_time})),cursor:data.cursor??null};
  }else if(args.kind==="project"){
    const g=data.gizmo?.gizmo||data.gizmo||data.resource?.gizmo;data={id:g?.id,name:g?.display?.name,type:g?.gizmo_type,short_url:g?.short_url};
  }else if(args.kind==="models"){
    data={models:(data.models||[]).map(x=>({slug:x.slug,title:x.title})),versions:(data.versions||[]).map(x=>({id:x.id,enabled:x.enabled,presets:x.intelligence_presets||[]}))};
  }
  return {http_status:response.status,account_tag:auth.accountTag,data};
}
`;

function fail(code,message){process.stdout.write(JSON.stringify({ok:false,error:code,message}));process.exitCode=2;}
function solveProof(seed,difficulty,baseConfig){
  if(!seed||!/^[0-9a-f]+$/iu.test(difficulty)||difficulty.length%2!==0)throw new Error("invalid proof challenge");
  const target=Buffer.from(difficulty,"hex");
  for(let nonce=0;nonce<500000;nonce++){
    const config=[...baseConfig];config[3]=nonce;config[9]=nonce>>1;
    const encoded=Buffer.from(JSON.stringify(config),"utf8").toString("base64");
    const digest=createHash("sha3-512").update(seed).update(encoded).digest();
    if(digest.subarray(0,target.length).compare(target)<=0)return "gAAAAAB"+encoded;
  }
  throw new Error("proof search exhausted");
}

async function main(){
  const raw=fs.readFileSync(0);
  if(raw.length>MAX_INPUT_BYTES)return fail("REQUEST_TOO_LARGE","CDP bridge request is too large");
  let request;try{request=JSON.parse(raw.toString("utf8"));}catch{return fail("REQUEST_INVALID","CDP bridge expects one JSON request on stdin");}
  if(!ACTIONS.has(request.action))return fail("ACTION_INVALID","Unsupported CDP bridge action");
  if(!normalizePluginHint(request))return fail("PLUGIN_APP_ID_REQUIRED","Current machine config is missing a valid chatgpt.plugin_app_id");
  const endpoint=new URL(String(request.cdp_endpoint||""));
  if(endpoint.protocol!=="http:"||!LOOPBACK_HOSTS.has(endpoint.hostname)||endpoint.username||endpoint.password||(endpoint.pathname!=="/"&&endpoint.pathname!=="")||endpoint.search||endpoint.hash){
    return fail("CDP_ENDPOINT_INVALID","CDP endpoint must be an unauthenticated loopback HTTP URL");
  }
  const timeoutMs=Math.max(1000,Math.min(Number(request.timeout_ms||20000),60000));
  const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),timeoutMs);let ws;
  try{
    const response=await fetch(new URL("/json/list",endpoint),{signal:controller.signal,cache:"no-store"});
    if(!response.ok)throw new Error("CDP target list HTTP "+response.status);
    const pages=await response.json(),projectId=String(request.args?.project_id||request.args?.payload?.gizmo_id||"");
    const target=pages.find(item=>item?.type==="page"&&projectId&&String(item.url||"").includes(projectId))||
      pages.find(item=>item?.type==="page"&&String(item.url||"").startsWith("https://chatgpt.com/"));
    if(!target?.webSocketDebuggerUrl)return fail("CHATGPT_PAGE_MISSING","No ChatGPT page is available in the dedicated browser");
    // The managed GPTAgent Chrome intentionally runs minimized.  Chromium can
    // background/freeze an inactive ChatGPT renderer strongly enough that the
    // browser-level CDP endpoint still answers and Target.attach succeeds, but
    // page-domain commands (including a trivial Runtime.evaluate) never get a
    // reply.  Wake the selected page through the browser's discovery endpoint
    // before opening its page websocket.  /json/activate is browser-process
    // control, so it still works when that renderer is currently suspended.
    const activateResponse=await fetch(new URL("/json/activate/"+encodeURIComponent(String(target.id||"")),endpoint),{
      signal:controller.signal,
      cache:"no-store",
    });
    if(!activateResponse.ok)throw new Error("CDP target activate HTTP "+activateResponse.status);
    ws=new WebSocket(target.webSocketDebuggerUrl);let sequence=0;const pending=new Map();
    const rejectAll=error=>{for(const waiter of pending.values()){clearTimeout(waiter.timer);waiter.reject(error);}pending.clear();};
    ws.onmessage=event=>{let message;try{message=JSON.parse(event.data);}catch{return;}const waiter=pending.get(message.id);if(!waiter)return;pending.delete(message.id);clearTimeout(waiter.timer);waiter.resolve(message);};
    await new Promise((resolve,reject)=>{const openTimer=setTimeout(()=>reject(new Error("CDP websocket open timeout")),Math.min(timeoutMs,5000));ws.onopen=()=>{clearTimeout(openTimer);resolve();};ws.onerror=()=>{clearTimeout(openTimer);reject(new Error("CDP websocket open failed"));};});
    ws.onerror=()=>rejectAll(new Error("CDP websocket error"));ws.onclose=()=>rejectAll(new Error("CDP websocket closed"));
    const call=(method,params={})=>new Promise((resolve,reject)=>{const id=++sequence;const timer=setTimeout(()=>{if(pending.delete(id))reject(new Error("CDP command timeout: "+method));},timeoutMs);pending.set(id,{resolve,reject,timer});ws.send(JSON.stringify({id,method,params}));});
    const evaluate=async(name,args)=>{
      const expression="(async()=>{"+PAGE_RUNTIME+";return await "+name+"("+JSON.stringify(args||{})+");})()";
      const result=await call("Runtime.evaluate",{expression,awaitPromise:true,returnByValue:true,userGesture:false});
      if(result?.error)throw new Error(result.error.message||"Runtime.evaluate failed");
      if(result?.result?.exceptionDetails){const detail=result.result.exceptionDetails;throw new Error(detail.exception?.description||detail.text||"Page evaluation failed");}
      return result?.result?.result?.value??null;
    };

    let value;
    if(request.action==="read")value=await evaluate("pageRead",request.args);
    else if(request.action==="receipt")value=await evaluate("pageReceipt",request.args);
    else{
      const preparedAt=Date.now();
      const prepared=await evaluate("pagePrepare",request.args);
      const prepareMs=Date.now()-preparedAt;
      if(request.action==="prepare_probe"){
        value={status:"prepared",prepare_ms:prepareMs,pow_required:!!prepared?.pow_required,turnstile_required:!!prepared?.turnstile_required};
      }else{
        const proofAt=Date.now();
        let proofToken="";
        if(prepared?.pow_required)proofToken=solveProof(String(prepared.pow_seed||""),String(prepared.pow_difficulty||""),prepared.proof_config||[]);
        const proofMs=Date.now()-proofAt;
        if(request.action==="proof_probe"){
          value={status:"proof_ready",prepare_ms:prepareMs,proof_ms:proofMs,pow_required:!!prepared?.pow_required,turnstile_required:!!prepared?.turnstile_required};
        }else if(request.action==="admission"){
          value=await evaluate("pageFinalizeOnly",{prepared,proof_token:proofToken});
          if(value&&typeof value==="object")value={...value,prepare_ms:prepareMs,proof_ms:proofMs};
        }else{
          value=await evaluate("pageStartPrepared",{operation_id:request.args?.operation_id,prepared,proof_token:proofToken});
        }
      }
    }
    process.stdout.write(JSON.stringify({ok:true,value,target_id:target.id||"",target_url:target.url||""}));
  }catch(error){fail("CDP_BRIDGE_FAILED",String(error?.message||error));}
  finally{clearTimeout(timer);try{ws?.close();}catch{}}
}
await main();
