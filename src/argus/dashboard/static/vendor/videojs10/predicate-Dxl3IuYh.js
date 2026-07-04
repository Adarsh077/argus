function e(e){let t=globalThis.customElements;!t||t.get(e.tagName)||t.define(e.tagName,e)}
/**
* @license
* Copyright 2021 Google LLC
* SPDX-License-Identifier: BSD-3-Clause
*/
var t=class extends Event{constructor(e,t,n,r){super(`context-request`,{bubbles:!0,composed:!0}),this.context=e,this.contextTarget=t,this.callback=n,this.subscribe=r??!1}};
/**
* @license
* Copyright 2021 Google LLC
* SPDX-License-Identifier: BSD-3-Clause
*/
function n(e){return e}const r=n(Symbol.for(`@videojs/player`)),i=n(Symbol.for(`@videojs/media`)),a=n(Symbol.for(`@videojs/container`));function o(e){return typeof e==`string`}function s(e){return typeof e==`number`}function c(e){return typeof e==`function`}function l(e){return e===null}function u(e){return e===void 0}function d(e){return typeof e==`object`&&!!e}export{o as a,i as c,t as d,e as f,d as i,r as l,l as n,u as o,s as r,a as s,c as t,n as u};
//# sourceMappingURL=predicate-Dxl3IuYh.js.map