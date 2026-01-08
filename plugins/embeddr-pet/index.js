(function(h,a,v,d){"use strict";var f={exports:{}},m={};/**
 * @license React
 * react-jsx-runtime.production.js
 *
 * Copyright (c) Meta Platforms, Inc. and affiliates.
 *
 * This source code is licensed under the MIT license found in the
 * LICENSE file in the root directory of this source tree.
 */var j=Symbol.for("react.transitional.element"),C=Symbol.for("react.fragment");function x(t,e,n){var o=null;if(n!==void 0&&(o=""+n),e.key!==void 0&&(o=""+e.key),"key"in e){n={};for(var l in e)l!=="key"&&(n[l]=e[l])}else n=e;return e=n.ref,{$$typeof:j,type:t,key:o,ref:e!==void 0?e:null,props:n}}m.Fragment=C,m.jsx=x,m.jsxs=x,f.exports=m;var s=f.exports;/**
 * @license lucide-react v0.544.0 - ISC
 *
 * This source code is licensed under the ISC license.
 * See the LICENSE file in the root directory of this source tree.
 */const _=t=>t.replace(/([a-z0-9])([A-Z])/g,"$1-$2").toLowerCase(),E=t=>t.replace(/^([A-Z])|[\s-_]+(\w)/g,(e,n,o)=>o?o.toUpperCase():n.toLowerCase()),P=t=>{const e=E(t);return e.charAt(0).toUpperCase()+e.slice(1)},k=(...t)=>t.filter((e,n,o)=>!!e&&e.trim()!==""&&o.indexOf(e)===n).join(" ").trim(),A=t=>{for(const e in t)if(e.startsWith("aria-")||e==="role"||e==="title")return!0};/**
 * @license lucide-react v0.544.0 - ISC
 *
 * This source code is licensed under the ISC license.
 * See the LICENSE file in the root directory of this source tree.
 */var T={xmlns:"http://www.w3.org/2000/svg",width:24,height:24,viewBox:"0 0 24 24",fill:"none",stroke:"currentColor",strokeWidth:2,strokeLinecap:"round",strokeLinejoin:"round"};/**
 * @license lucide-react v0.544.0 - ISC
 *
 * This source code is licensed under the ISC license.
 * See the LICENSE file in the root directory of this source tree.
 */const N=a.forwardRef(({color:t="currentColor",size:e=24,strokeWidth:n=2,absoluteStrokeWidth:o,className:l="",children:i,iconNode:c,...u},r)=>a.createElement("svg",{ref:r,...T,width:e,height:e,stroke:t,strokeWidth:o?Number(n)*24/Number(e):n,className:k("lucide",l),...!i&&!A(u)&&{"aria-hidden":"true"},...u},[...c.map(([g,F])=>a.createElement(g,F)),...Array.isArray(i)?i:[i]]));/**
 * @license lucide-react v0.544.0 - ISC
 *
 * This source code is licensed under the ISC license.
 * See the LICENSE file in the root directory of this source tree.
 */const p=(t,e)=>{const n=a.forwardRef(({className:o,...l},i)=>a.createElement(N,{ref:i,iconNode:e,className:k(`lucide-${_(P(t))}`,`lucide-${t}`,o),...l}));return n.displayName=P(t),n};/**
 * @license lucide-react v0.544.0 - ISC
 *
 * This source code is licensed under the ISC license.
 * See the LICENSE file in the root directory of this source tree.
 */const S=p("cat",[["path",{d:"M12 5c.67 0 1.35.09 2 .26 1.78-2 5.03-2.84 6.42-2.26 1.4.58-.42 7-.42 7 .57 1.07 1 2.24 1 3.44C21 17.9 16.97 21 12 21s-9-3-9-7.56c0-1.25.5-2.4 1-3.44 0 0-1.89-6.42-.5-7 1.39-.58 4.72.23 6.5 2.23A9.04 9.04 0 0 1 12 5Z",key:"x6xyqk"}],["path",{d:"M8 14v.5",key:"1nzgdb"}],["path",{d:"M16 14v.5",key:"1lajdz"}],["path",{d:"M11.25 16.25h1.5L12 17l-.75-.75Z",key:"12kq1m"}]]);/**
 * @license lucide-react v0.544.0 - ISC
 *
 * This source code is licensed under the ISC license.
 * See the LICENSE file in the root directory of this source tree.
 */const y=p("fish",[["path",{d:"M6.5 12c.94-3.46 4.94-6 8.5-6 3.56 0 6.06 2.54 7 6-.94 3.47-3.44 6-7 6s-7.56-2.53-8.5-6Z",key:"15baut"}],["path",{d:"M18 12v.5",key:"18hhni"}],["path",{d:"M16 17.93a9.77 9.77 0 0 1 0-11.86",key:"16dt7o"}],["path",{d:"M7 10.67C7 8 5.58 5.97 2.73 5.5c-1 1.5-1 5 .23 6.5-1.24 1.5-1.24 5-.23 6.5C5.58 18.03 7 16 7 13.33",key:"l9di03"}],["path",{d:"M10.46 7.26C10.2 5.88 9.17 4.24 8 3h5.8a2 2 0 0 1 1.98 1.67l.23 1.4",key:"1kjonw"}],["path",{d:"m16.01 17.93-.23 1.4A2 2 0 0 1 13.8 21H9.5a5.96 5.96 0 0 0 1.49-3.98",key:"1zlm23"}]]);/**
 * @license lucide-react v0.544.0 - ISC
 *
 * This source code is licensed under the ISC license.
 * See the LICENSE file in the root directory of this source tree.
 */const M=p("volume-2",[["path",{d:"M11 4.702a.705.705 0 0 0-1.203-.498L6.413 7.587A1.4 1.4 0 0 1 5.416 8H3a1 1 0 0 0-1 1v6a1 1 0 0 0 1 1h2.416a1.4 1.4 0 0 1 .997.413l3.383 3.384A.705.705 0 0 0 11 19.298z",key:"uqj9uw"}],["path",{d:"M16 9a5 5 0 0 1 0 6",key:"1q6k2b"}],["path",{d:"M19.364 18.364a9 9 0 0 0 0-12.728",key:"ijwkga"}]]),b=()=>{try{const t=new(window.AudioContext||window.webkitAudioContext),e=t.createOscillator(),n=t.createGain();e.connect(n),n.connect(t.destination),e.type="sine",e.frequency.setValueAtTime(523.25,t.currentTime),e.frequency.exponentialRampToValueAtTime(1046.5,t.currentTime+.1),n.gain.setValueAtTime(.1,t.currentTime),n.gain.exponentialRampToValueAtTime(.001,t.currentTime+.5),e.start(),e.stop(t.currentTime+.5)}catch(t){console.error("Failed to play sound",t)}},R={idle:["( ^_^ )","( -_- )","( ^_^ )","( O_O )"],generating:["( >_< )","( <_> )","( >_< )","( <_> )"],happy:["( ^o^ )","( ^_^ )","( ^o^ )","( ^_^ )"]},w={id:"examples.pet-panel",name:"Desktop Pet",description:"A cute companion that reacts to your workflow",version:"1.0.0",author:"Embeddr",initialize:t=>{console.log("[PetPanelPlugin] Initializing");const e=t.events.on("generation:complete",()=>{console.log("[PetPanelPlugin] Received generation:complete event"),localStorage.getItem("pet-plugin-sound")==="true"&&b()});return()=>{console.log("[PetPanelPlugin] Cleaning up"),e()}},components:[{id:"pet-panel-toggle",location:"zen-toolbox-tab",label:"Pet",component:({api:t})=>{const[e,n]=a.useState(!1),[o,l]=a.useState(0),[i,c]=a.useState("idle"),u=t.stores.generation.isGenerating;return a.useEffect(()=>t.events.on("pet:feed",()=>{c("happy"),setTimeout(()=>c("idle"),2e3)}),[t.events]),a.useEffect(()=>{let r;return u?c("generating"):i==="generating"&&(c("happy"),r=setTimeout(()=>c("idle"),2e3)),()=>{r&&clearTimeout(r)}},[u]),a.useEffect(()=>{const r=setInterval(()=>{l(g=>(g+1)%4)},500);return()=>clearInterval(r)},[]),s.jsxs(s.Fragment,{children:[s.jsxs(d.Button,{variant:e?"secondary":"outline",className:"w-full justify-start",onClick:()=>n(!e),children:[s.jsx(S,{className:"w-4 h-4 mr-2"}),e?"Close Pet":"Open Pet"]}),e&&v.createPortal(s.jsx(d.DraggablePanel,{id:"pet-panel",title:"Desktop Pet",isOpen:e,onClose:()=>n(!1),defaultPosition:{x:100,y:100},defaultSize:{width:200,height:150},className:"absolute z-50",children:s.jsxs("div",{className:"flex flex-col items-center justify-center h-full bg-background p-4",children:[s.jsx("div",{className:"text-2xl font-mono font-bold mb-4",children:R[i][o]}),s.jsx("div",{className:"text-xs text-muted-foreground",children:i==="idle"?"Waiting...":i==="generating"?"Working hard!":"Done!"})]})}),document.body)]})}}],actions:[{id:"sound-config",location:"zen-toolbox-action",label:"Sound Settings",icon:M,component:()=>{const[t,e]=a.useState(()=>localStorage.getItem("pet-plugin-sound")==="true"),n=o=>{e(o),localStorage.setItem("pet-plugin-sound",String(o)),o&&b()};return s.jsxs("div",{className:"flex items-center justify-between space-x-2",children:[s.jsxs(d.Label,{htmlFor:"sound-mode",className:"flex flex-col space-y-1",children:[s.jsx("span",{children:"Enable Sound"}),s.jsx("span",{className:"font-normal text-xs text-muted-foreground",children:"Play a ding when done"})]}),s.jsx(d.Switch,{id:"sound-mode",checked:t,onCheckedChange:n})]})}},{id:"feed-pet",location:"zen-toolbox-action",label:"Feed Pet",icon:y,component:({api:t})=>{const e=()=>{t.toast.success("Yum! The pet is happy."),t.events.emit("pet:feed")};return s.jsxs("div",{className:"flex flex-col gap-2",children:[s.jsx("p",{className:"text-xs text-muted-foreground",children:"Give the pet a treat to keep morale high during long generations."}),s.jsxs(d.Button,{size:"sm",onClick:e,className:"w-full",children:[s.jsx(y,{className:"w-4 h-4 mr-2"}),"Feed Treat"]})]})}}]};typeof window<"u"&&window.Embeddr&&window.Embeddr.registerPlugin(w),h.PetPanelPlugin=w,Object.defineProperty(h,Symbol.toStringTag,{value:"Module"})})(this.EmbeddrPlugin=this.EmbeddrPlugin||{},React,ReactDOM,EmbeddrUI);
