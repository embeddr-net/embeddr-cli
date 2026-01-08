(function(y,c,g){"use strict";var w={exports:{}},b={};/**
 * @license React
 * react-jsx-runtime.production.js
 *
 * Copyright (c) Meta Platforms, Inc. and affiliates.
 *
 * This source code is licensed under the MIT license found in the
 * LICENSE file in the root directory of this source tree.
 */var A=Symbol.for("react.transitional.element"),S=Symbol.for("react.fragment");function f(n,e,t){var o=null;if(t!==void 0&&(o=""+t),e.key!==void 0&&(o=""+e.key),"key"in e){t={};for(var a in e)a!=="key"&&(t[a]=e[a])}else t=e;return e=t.ref,{$$typeof:A,type:n,key:o,ref:e!==void 0?e:null,props:t}}b.Fragment=S,b.jsx=f,b.jsxs=f,w.exports=b;var l=w.exports;/**
 * @license lucide-react v0.544.0 - ISC
 *
 * This source code is licensed under the ISC license.
 * See the LICENSE file in the root directory of this source tree.
 */const j=n=>n.replace(/([a-z0-9])([A-Z])/g,"$1-$2").toLowerCase(),T=n=>n.replace(/^([A-Z])|[\s-_]+(\w)/g,(e,t,o)=>o?o.toUpperCase():t.toLowerCase()),k=n=>{const e=T(n);return e.charAt(0).toUpperCase()+e.slice(1)},v=(...n)=>n.filter((e,t,o)=>!!e&&e.trim()!==""&&o.indexOf(e)===t).join(" ").trim(),P=n=>{for(const e in n)if(e.startsWith("aria-")||e==="role"||e==="title")return!0};/**
 * @license lucide-react v0.544.0 - ISC
 *
 * This source code is licensed under the ISC license.
 * See the LICENSE file in the root directory of this source tree.
 */var I={xmlns:"http://www.w3.org/2000/svg",width:24,height:24,viewBox:"0 0 24 24",fill:"none",stroke:"currentColor",strokeWidth:2,strokeLinecap:"round",strokeLinejoin:"round"};/**
 * @license lucide-react v0.544.0 - ISC
 *
 * This source code is licensed under the ISC license.
 * See the LICENSE file in the root directory of this source tree.
 */const L=c.forwardRef(({color:n="currentColor",size:e=24,strokeWidth:t=2,absoluteStrokeWidth:o,className:a="",children:r,iconNode:s,...i},u)=>c.createElement("svg",{ref:u,...I,width:e,height:e,stroke:n,strokeWidth:o?Number(t)*24/Number(e):t,className:v("lucide",a),...!r&&!P(i)&&{"aria-hidden":"true"},...i},[...s.map(([m,p])=>c.createElement(m,p)),...Array.isArray(r)?r:[r]]));/**
 * @license lucide-react v0.544.0 - ISC
 *
 * This source code is licensed under the ISC license.
 * See the LICENSE file in the root directory of this source tree.
 */const d=(n,e)=>{const t=c.forwardRef(({className:o,...a},r)=>c.createElement(L,{ref:r,iconNode:e,className:v(`lucide-${j(k(n))}`,`lucide-${n}`,o),...a}));return t.displayName=k(n),t};/**
 * @license lucide-react v0.544.0 - ISC
 *
 * This source code is licensed under the ISC license.
 * See the LICENSE file in the root directory of this source tree.
 */const M=d("gamepad-2",[["line",{x1:"6",x2:"10",y1:"11",y2:"11",key:"1gktln"}],["line",{x1:"8",x2:"8",y1:"9",y2:"13",key:"qnk9ow"}],["line",{x1:"15",x2:"15.01",y1:"12",y2:"12",key:"krot7o"}],["line",{x1:"18",x2:"18.01",y1:"10",y2:"10",key:"1lcuu1"}],["path",{d:"M17.32 5H6.68a4 4 0 0 0-3.978 3.59c-.006.052-.01.101-.017.152C2.604 9.416 2 14.456 2 16a3 3 0 0 0 3 3c1 0 1.5-.5 2-1l1.414-1.414A2 2 0 0 1 9.828 16h4.344a2 2 0 0 1 1.414.586L17 18c.5.5 1 1 2 1a3 3 0 0 0 3-3c0-1.545-.604-6.584-.685-7.258-.007-.05-.011-.1-.017-.151A4 4 0 0 0 17.32 5z",key:"mfqc10"}]]);/**
 * @license lucide-react v0.544.0 - ISC
 *
 * This source code is licensed under the ISC license.
 * See the LICENSE file in the root directory of this source tree.
 */const C=d("heart",[["path",{d:"M2 9.5a5.5 5.5 0 0 1 9.591-3.676.56.56 0 0 0 .818 0A5.49 5.49 0 0 1 22 9.5c0 2.29-1.5 4-3 5.5l-5.492 5.313a2 2 0 0 1-3 .019L5 15c-1.5-1.5-3-3.2-3-5.5",key:"mvr1a0"}]]);/**
 * @license lucide-react v0.544.0 - ISC
 *
 * This source code is licensed under the ISC license.
 * See the LICENSE file in the root directory of this source tree.
 */const x=d("loader-circle",[["path",{d:"M21 12a9 9 0 1 1-6.219-8.56",key:"13zald"}]]);/**
 * @license lucide-react v0.544.0 - ISC
 *
 * This source code is licensed under the ISC license.
 * See the LICENSE file in the root directory of this source tree.
 */const _=d("play",[["path",{d:"M5 5a2 2 0 0 1 3.008-1.728l11.997 6.998a2 2 0 0 1 .003 3.458l-12 7A2 2 0 0 1 5 19z",key:"10ikf1"}]]);/**
 * @license lucide-react v0.544.0 - ISC
 *
 * This source code is licensed under the ISC license.
 * See the LICENSE file in the root directory of this source tree.
 */const G=d("sparkles",[["path",{d:"M11.017 2.814a1 1 0 0 1 1.966 0l1.051 5.558a2 2 0 0 0 1.594 1.594l5.558 1.051a1 1 0 0 1 0 1.966l-5.558 1.051a2 2 0 0 0-1.594 1.594l-1.051 5.558a1 1 0 0 1-1.966 0l-1.051-5.558a2 2 0 0 0-1.594-1.594l-5.558-1.051a1 1 0 0 1 0-1.966l5.558-1.051a2 2 0 0 0 1.594-1.594z",key:"1s2grr"}],["path",{d:"M20 2v4",key:"1rf3ol"}],["path",{d:"M22 4h-4",key:"gwowj6"}],["circle",{cx:"4",cy:"20",r:"2",key:"6kqj1y"}]]);/**
 * @license lucide-react v0.544.0 - ISC
 *
 * This source code is licensed under the ISC license.
 * See the LICENSE file in the root directory of this source tree.
 */const h=d("zap",[["path",{d:"M4 14a1 1 0 0 1-.78-1.63l9.9-10.2a.5.5 0 0 1 .86.46l-1.92 6.02A1 1 0 0 0 13 10h7a1 1 0 0 1 .78 1.63l-9.9 10.2a.5.5 0 0 1-.86-.46l1.92-6.02A1 1 0 0 0 11 14z",key:"1xq2db"}]]),E=(n,e,t)=>{const[o,a]=c.useState(()=>{var r;try{return((r=JSON.parse(localStorage.getItem("zen-plugin-settings")||"{}")[n])==null?void 0:r[e])??t}catch{return t}});return c.useEffect(()=>{const r=()=>{var s;try{const u=((s=JSON.parse(localStorage.getItem("zen-plugin-settings")||"{}")[n])==null?void 0:s[e])??t;a(u)}catch{}};return window.addEventListener("local-storage",r),window.addEventListener("storage",r),()=>{window.removeEventListener("local-storage",r),window.removeEventListener("storage",r)}},[n,e,t]),o},N={default:{label:"Default",icon:h,containerClass:"bg-transparent",buttonClass:"bg-primary hover:bg-primary/90 shadow-lg transition-all hover:scale-[1.02] active:scale-[0.98]",textClass:"text-muted-foreground",loader:x,playIcon:_},pixel:{label:"Retro Pixel",icon:M,containerClass:"bg-transparent font-mono",buttonClass:"bg-green-600 hover:bg-green-500 border-4 border-b-8 border-green-800 active:border-b-4 active:translate-y-1 rounded-none shadow-none text-white font-black tracking-widest",textClass:"text-green-400 font-mono text-[10px] uppercase",loader:x,playIcon:_},kawaii:{label:"Kawaii",icon:C,containerClass:"bg-transparent",buttonClass:"bg-pink-400 hover:bg-pink-300 border-4 border-white shadow-[0_0_0_4px_rgba(244,114,182,0.5)] rounded-3xl text-white font-bold animate-pulse hover:animate-none",textClass:"text-pink-400 font-comic text-xs",loader:G,playIcon:C},cyber:{label:"Cyberpunk",icon:h,containerClass:"bg-transparent",buttonClass:"bg-cyan-950/50 hover:bg-cyan-900/50 border border-cyan-500 text-cyan-400 shadow-[0_0_15px_rgba(6,182,212,0.5)] hover:shadow-[0_0_25px_rgba(6,182,212,0.8)] font-mono tracking-[0.2em] uppercase backdrop-blur-md",textClass:"text-cyan-600 font-mono text-[10px] uppercase",loader:x,playIcon:h}},$={id:"core.generate-button",name:"Generate Button",description:"A movable panel with a large generate button",version:"1.0.0",settings:[{key:"buttonText",type:"string",label:"Button Text",description:"Custom text for the generate button",defaultValue:""},{key:"theme",type:"select",label:"Theme",description:"Visual theme for the button",defaultValue:"default",options:[{label:"Default",value:"default"},{label:"Retro Pixel",value:"pixel"},{label:"Kawaii",value:"kawaii"},{label:"Cyberpunk",value:"cyber"}]}],components:[{id:"generate-panel",location:"zen-overlay",label:"Generate",component:({api:n})=>{const{isGenerating:e,selectedWorkflow:t,generations:o}=n.stores.generation,a=E("core.generate-button","theme","default"),r=E("core.generate-button","buttonText",""),s=o.filter(p=>p.status==="pending"||p.status==="processing"||p.status==="queued").length,i=N[a]||N.default,u=i.loader,m=i.playIcon;return l.jsxs("div",{className:g.cn("flex flex-col items-center justify-center h-full p-4 gap-2 relative group",i.containerClass),children:[l.jsx(g.Button,{variant:"default",size:"lg",className:g.cn("w-full h-full min-h-[60px] text-lg font-bold",i.buttonClass),onClick:()=>n.events.emit("zen:generate"),disabled:!t,children:e?l.jsxs(l.Fragment,{children:[l.jsx(u,{className:"mr-2 h-6 w-6 animate-spin"}),a==="pixel"?`QUEUED [${s}]`:a==="kawaii"?`Cooking! (${s})`:a==="cyber"?`EXECUTING [${s}]`:`Generating (${s})`]}):l.jsxs(l.Fragment,{children:[l.jsx(m,{className:"mr-2 h-6 w-6 fill-current"}),r||(a==="pixel"?"START":a==="kawaii"?"Make Magic!":a==="cyber"?"INITIALIZE":"Generate")]})}),t&&l.jsx("div",{className:g.cn("text-center truncate max-w-full px-2",i.textClass),children:t.name})]})},defaultSize:{width:200,height:120},defaultPosition:{x:window.innerWidth-220,y:window.innerHeight-140},options:{hideHeader:!0,transparent:!0}}]};typeof window<"u"&&window.Embeddr&&window.Embeddr.registerPlugin($),y.GenerateButtonPlugin=$,Object.defineProperty(y,Symbol.toStringTag,{value:"Module"})})(this.EmbeddrPlugin=this.EmbeddrPlugin||{},React,EmbeddrUI);
