"use strict";
// Native selects retain their values and change events; the visible control is consistent across browsers.
let ACTIVE_SELECT = null, SELECT_SERIAL = 0;
const SELECTS = new WeakMap();
function isSelectOpen() { return Boolean(ACTIVE_SELECT); }
function closeSelect(focus = false) { ACTIVE_SELECT?.close(focus); }
function syncSelect(native) { SELECTS.get(native)?.(); }
function enhanceSelect(native) {
  if(native.dataset.enhanced) return;
  native.dataset.enhanced="true";
  const label=native.getAttribute("aria-label") || native.labels?.[0]?.textContent.trim() || "Choice";
  const id="choice-"+(++SELECT_SERIAL), shell=el("span",{class:"select-shell"});
  const trigger=el("button",{type:"button",id:id+"-trigger",class:"select-trigger",role:"combobox","aria-label":label,"aria-haspopup":"listbox","aria-expanded":"false","aria-controls":id+"-list"},
    el("span",{class:"select-value"}),el("span",{class:"select-chevron","aria-hidden":"true"}));
  shell.style.cssText=native.style.cssText;
  const labels=[...(native.labels||[])];native.before(shell);shell.append(native,trigger);
  labels.filter(l=>l.htmlFor===native.id).forEach(l=>l.htmlFor=trigger.id);
  native.classList.add("select-native");native.setAttribute("aria-hidden","true");native.tabIndex=-1;
  const update=()=>{trigger.querySelector(".select-value").textContent=native.selectedOptions[0]?.textContent || "Please choose";trigger.disabled=native.disabled;};
  SELECTS.set(native,update);native.addEventListener("change",update);update();
  let prefix="",prefixTimer;
  function open(focusList=false) {
    closeSelect();update();
    const options=[...native.options], searchable=label==="Airport" || options.length>8 || native.dataset.searchable==="true";
    const list=el("div",{id:id+"-list",class:"select-list",role:"listbox","aria-label":label});
    const search=searchable?el("input",{class:"select-search",type:"search",placeholder:"Search …","aria-label":"Search "+label,autocomplete:"off"}):null;
    const popup=el("div",{class:"select-popover"},search,list);document.body.append(popup);
    trigger.setAttribute("aria-expanded","true");let buttons=[];
    const close=focus=>{popup.remove();trigger.setAttribute("aria-expanded","false");if(ACTIVE_SELECT?.trigger===trigger)ACTIVE_SELECT=null;if(focus&&trigger.isConnected)trigger.focus();};
    ACTIVE_SELECT={trigger,close};
    const choose=value=>{native.value=value;update();close(true);native.dispatchEvent(new Event("change",{bubbles:true}));};
    const draw=()=>{
      const query=(search?.value || "").toLocaleLowerCase("en");
      buttons=options.filter(o=>o.textContent.toLocaleLowerCase("en").includes(query)).map(o=>el("button",{type:"button",class:"select-option",role:"option","aria-selected":String(o.value===native.value),tabindex:-1,disabled:o.disabled,onclick:()=>choose(o.value)},el("span",{},o.textContent),o.value===native.value?el("span",{"aria-hidden":"true"},"✓"):null));
      list.replaceChildren(...(buttons.length?buttons:[el("div",{class:"select-empty",role:"status"},"No matching options")]));
    };
    draw();search?.addEventListener("input",draw);
    const rect=trigger.getBoundingClientRect(), width=Math.min(Math.max(rect.width,searchable?290:230),innerWidth-24);
    const below=innerHeight-rect.bottom-12,above=rect.top-12,up=below<230&&above>below;
    popup.style.width=width+"px";popup.style.left=Math.max(12,Math.min(rect.left,innerWidth-width-12))+"px";
    popup.style.maxHeight=Math.min(380,up?above:below)+"px";
    if(up)popup.style.bottom=(innerHeight-rect.top+6)+"px";else popup.style.top=(rect.bottom+6)+"px";
    const keydown=e=>{
      if(e.key==="Escape"){e.preventDefault();e.stopPropagation();close(true);return;}
      if(e.key==="Tab"){close(true);return;}
      if(["ArrowDown","ArrowUp","Home","End"].includes(e.key)){
        e.preventDefault();e.stopPropagation();const enabled=buttons.filter(b=>!b.disabled);if(!enabled.length)return;
        let at=enabled.indexOf(document.activeElement);
        at=e.key==="Home"?0:e.key==="End"?enabled.length-1:(at+(e.key==="ArrowUp"?-1:1)+enabled.length)%enabled.length;
        enabled[at].focus();enabled[at].scrollIntoView({block:"nearest"});
      }
    };
    popup.addEventListener("keydown",keydown);
    if(search&&!focusList)search.focus();else (buttons.find(b=>b.getAttribute("aria-selected")==="true")||buttons.find(b=>!b.disabled))?.focus();
  }
  trigger.addEventListener("click",()=>ACTIVE_SELECT?.trigger===trigger?closeSelect():open());
  trigger.addEventListener("keydown",e=>{
    if(["ArrowDown","ArrowUp"].includes(e.key)){e.preventDefault();open(true);}
    else if(e.key.length===1 && !e.ctrlKey && !e.metaKey && e.key!==" "){
      e.preventDefault();clearTimeout(prefixTimer);prefix+=e.key.toLocaleLowerCase("en");prefixTimer=setTimeout(()=>prefix="",650);
      const option=[...native.options].find(o=>!o.disabled&&o.textContent.toLocaleLowerCase("en").startsWith(prefix));
      if(option){native.value=option.value;update();native.dispatchEvent(new Event("change",{bubbles:true}));}
    }
  });
}
document.addEventListener("DOMContentLoaded",()=>{
  document.querySelectorAll("select").forEach(enhanceSelect);
  new MutationObserver(records=>{
    if(ACTIVE_SELECT&&!ACTIVE_SELECT.trigger.isConnected)closeSelect();
    for(const record of records)for(const node of record.addedNodes)if(node.nodeType===1){if(node.matches("select"))enhanceSelect(node);node.querySelectorAll("select").forEach(enhanceSelect);}
  }).observe(document.body,{childList:true,subtree:true});
});
document.addEventListener("pointerdown",e=>{if(ACTIVE_SELECT&&!e.target.closest(".select-popover")&&!ACTIVE_SELECT.trigger.contains(e.target))closeSelect();});
window.addEventListener("resize",()=>closeSelect());
document.addEventListener("wheel",e=>{if(ACTIVE_SELECT&&!e.target.closest?.(".select-popover"))closeSelect();},{capture:true,passive:true});

let AIRPORT_NAMES;
async function airportOptions() {
  AIRPORT_NAMES ||= api("/static/geo/airports.json").catch(()=>{AIRPORT_NAMES=null;return {};});
  const [data,names]=await Promise.all([api("/api/business-metrics?days=0").catch(()=>null),AIRPORT_NAMES]);
  const codes=new Set((data?.airports||[]).map(p=>p.iata));if(S.requestAirport)codes.add(S.requestAirport);
  return [["","All airports"],...[...codes].sort().map(code=>[code,[code,names[code]?.city,names[code]?.name].filter(Boolean).join(" · ")])];
}
