#!/usr/bin/env node
// Evaluate the opaque Sentinel VM program in a minimal non-browser JS runtime.

const [dx, key] = process.argv.slice(2);
if (!dx || !key) process.exit(2);

globalThis.window = globalThis;
globalThis.document = {
  scripts: [],
  documentElement: { getAttribute: () => null },
};
globalThis.screen ??= { width: 1920, height: 1080 };
globalThis.navigator ??= {
  userAgent: "Codex Desktop/42.3.0",
  language: "en-US",
  languages: ["en-US", "en"],
  hardwareConcurrency: 8,
};
globalThis.atob ??= value => Buffer.from(value, "base64").toString("binary");
globalThis.btoa ??= value => Buffer.from(value, "binary").toString("base64");

const H=0,U=1,W=2,G=3,K=4,q=5,J=6,Y=7,X=8,F=9,Z=10,Q=11,$=12,E=13,T=14,N=15,R=16,I=17,A=18,O=19,S=20,C=21,L=22,V=23,D=24,NOOP1=25,NOOP2=26,M=27,NOOP3=28,LESS=29,FN=30,MUL=33,DIV=34,SUB=35;
const state = new Map();
let steps = 0;
let serial = Promise.resolve();
let halted = false;

function xor(value, secret) {
  let result = "";
  for (let i=0; i<value.length; i++) result += String.fromCharCode(value.charCodeAt(i) ^ secret.charCodeAt(i % secret.length));
  return result;
}

async function run() {
  while (!halted && (state.get(F) || []).length > 0) {
    const [op, ...args] = state.get(F).shift() || [];
    const result = state.get(op)?.(...args);
    if (result && typeof result.then === "function") await result;
    steps++;
    // Opaque programs may keep replenishing the instruction queue. Yield so
    // the protocol's 500 ms fallback timer can fire instead of being starved
    // by an unbounded synchronous microtask chain.
    if ((steps & 255) === 0) await new Promise(resolve => setImmediate(resolve));
  }
}

function initialize() {
  state.clear();
  state.set(H, value => solve(value, String(state.get(R))));
  state.set(U, (out,a) => state.set(out, xor(String(state.get(a)), String(state.get(R)))));
  state.set(W, (out,value) => state.set(out,value));
  state.set(q, (out,value) => { const old=state.get(out); Array.isArray(old) ? old.push(state.get(value)) : state.set(out,old+state.get(value)); });
  state.set(SUB, (out,value) => { const old=state.get(out); Array.isArray(old) ? old.splice(old.indexOf(state.get(value)),1) : state.set(out,old-state.get(value)); });
  state.set(LESS, (out,a,b) => state.set(out,Number(state.get(a))<Number(state.get(b))));
  state.set(MUL, (out,a,b) => state.set(out,Number(state.get(a))*Number(state.get(b))));
  state.set(DIV, (out,a,b) => { const n=Number(state.get(b)); state.set(out,n===0?0:Number(state.get(a))/n); });
  state.set(J, (out,obj,key) => { const value=state.get(obj); state.set(out,value[String(state.get(key))]); });
  state.set(Y, (fn,...args) => state.get(fn)(...args.map(arg=>state.get(arg))));
  state.set(I, (out,fn,...args) => { try { const value=state.get(fn)(...args.map(arg=>state.get(arg))); if(value?.then)return value.then(v=>state.set(out,v)).catch(e=>state.set(out,String(e))); state.set(out,value); } catch(e) { state.set(out,String(e)); } });
  state.set(E, (out,fn,...args) => { try { state.get(fn)(...args); } catch(e) { state.set(out,String(e)); } });
  state.set(X, (out,value) => state.set(out,state.get(value)));
  state.set(Z, globalThis.window);
  state.set(Q, (out,pattern) => state.set(out,null));
  state.set($, out => state.set(out,state));
  state.set(T, (out,value) => state.set(out,JSON.parse(String(state.get(value)))));
  state.set(N, (out,value) => state.set(out,JSON.stringify(state.get(value))));
  state.set(A, out => state.set(out,atob(String(state.get(out)))));
  state.set(O, out => state.set(out,btoa(String(state.get(out)))));
  state.set(S, (a,b,fn,...args) => state.get(a)===state.get(b) ? state.get(fn)(...args) : null);
  state.set(C, (a,b,delta,fn,...args) => Math.abs(Number(state.get(a))-Number(state.get(b)))>Number(state.get(delta)) ? state.get(fn)(...args) : null);
  state.set(V, (value,fn,...args) => state.get(value)===undefined ? null : state.get(fn)(...args));
  state.set(D, (out,obj,key) => { const value=state.get(obj); state.set(out,value[String(state.get(key))].bind(value)); });
  state.set(M, (out,value) => Promise.resolve(state.get(value)).then(v=>state.set(out,v)));
  state.set(L, (out,queue) => { const old=[...(state.get(F)||[])]; state.set(F,[...queue]); return run().catch(e=>state.set(out,String(e))).finally(()=>state.set(F,old)); });
  state.set(NOOP1,()=>{}); state.set(NOOP2,()=>{}); state.set(NOOP3,()=>{});
}

function solve(payload, secret) {
  return serial = serial.then(() => new Promise((resolve,reject) => {
    initialize(); steps=0; halted=false; state.set(R,secret); let done=false;
    const timer=setTimeout(()=>{ if(!done){done=true;halted=true;resolve(String(steps));}},500);
    state.set(G,value=>{if(!done){done=true;halted=true;clearTimeout(timer);resolve(btoa(String(value)));}});
    state.set(K,value=>{if(!done){done=true;halted=true;clearTimeout(timer);reject(new Error(btoa(String(value))));}});
    state.set(FN,(out,target,params,queue)=>{const array=Array.isArray(queue), names=array?params:[], instructions=(array?queue:params)||[];state.set(out,(...values)=>{if(done)return;const old=[...(state.get(F)||[])];if(array)for(let i=0;i<names.length;i++)state.set(names[i],values[i]);state.set(F,[...instructions]);return run().then(()=>state.get(target)).catch(String).finally(()=>state.set(F,old));});});
    try { state.set(F,JSON.parse(xor(atob(payload),String(state.get(R))))); run().catch(e=>{if(!done){done=true;clearTimeout(timer);resolve(btoa(`${steps}: ${String(e)}`));}}); }
    catch(e) { done=true;clearTimeout(timer);resolve(btoa(`${steps}: ${String(e)}`)); }
  }));
}

process.stdout.write(await solve(dx,key));
