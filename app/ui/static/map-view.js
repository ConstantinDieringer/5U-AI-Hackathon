"use strict";
const OPENFREEMAP_STYLE = "https://tiles.openfreemap.org/styles/positron";
let MAP_LIBRARY, AIRPORT_COORDS, NETWORK_CARD, NETWORK_MAP, NETWORK_DATA;
const NETWORK_MARKERS = new Map();

function flightArc(a,b,steps=64) {
  const rad=Math.PI/180,vector=p=>[Math.cos(p.lat*rad)*Math.cos(p.lon*rad),Math.cos(p.lat*rad)*Math.sin(p.lon*rad),Math.sin(p.lat*rad)];
  const av=vector(a),bv=vector(b),angle=Math.acos(Math.max(-1,Math.min(1,av.reduce((sum,v,i)=>sum+v*bv[i],0)))),sin=Math.sin(angle);
  let previous=a.lon;
  return Array.from({length:steps+1},(_,i)=>{
    const t=i/steps;
    let lon,lat;
    if(Math.abs(sin)<1e-8){let end=b.lon;while(end-a.lon>180)end-=360;while(end-a.lon< -180)end+=360;lon=a.lon+(end-a.lon)*t;lat=a.lat+(b.lat-a.lat)*t;}
    else {const x=av.map((v,j)=>Math.sin((1-t)*angle)/sin*v+Math.sin(t*angle)/sin*bv[j]);lon=Math.atan2(x[1],x[0])/rad;lat=Math.atan2(x[2],Math.hypot(x[0],x[1]))/rad;}
    while(lon-previous>180)lon-=360;while(lon-previous< -180)lon+=360;previous=lon;
    return [lon,lat];
  });
}
function networkGeoJSON(points,routes,airports) {
  const visible=new Set(points.map(p=>p.iata));
  return {type:"FeatureCollection",features:routes.filter(r=>visible.has(r.origin)&&visible.has(r.destination)&&airports[r.origin]&&airports[r.destination]&&r.origin!==r.destination).map(r=>({
    type:"Feature",id:r.origin+"-"+r.destination,properties:{origin:r.origin,destination:r.destination,requests:r.requests},
    geometry:{type:"LineString",coordinates:flightArc(airports[r.origin],airports[r.destination])}
  }))};
}
function airportMapCard(points,routes) {
  if(!NETWORK_CARD) {
    const reset=el("button",{class:"btn",onclick:()=>NETWORK_MAP?.flyTo({center:[8,22],zoom:1.2,bearing:0,pitch:0,duration:matchMedia("(prefers-reduced-motion:reduce)").matches?0:600})},"World view");
    NETWORK_CARD=el("section",{class:"card s12 airport-map-card"},
      el("div",{class:"card-h"},el("div",{},el("h2",{},"Your global network"),el("p",{class:"caption"},"Requested lanes between your airports. Drag, scroll and zoom.")),reset),
      el("div",{class:"map-layout"},
        el("aside",{class:"event-panel","aria-label":"Events affecting your airports"},el("div",{class:"event-head"},el("h3",{},"Airport alerts")),el("div",{class:"event-list"},emptyState("clock","Checking alerts …"))),
        el("div",{class:"map-main"},el("div",{id:"airport-map",class:"openfreemap","aria-label":"Airports and requested flight lanes on OpenFreeMap"}),
          el("div",{class:"map-status caption",role:"status"},"Loading map …"))),
      el("p",{class:"caption map-source"},el("a",{href:"https://github.com/mwgg/Airports",target:"_blank",rel:"noopener noreferrer"},"Airport data mwgg/Airports"),
        " · Alerts ",el("a",{href:"https://www.gdacs.org/",target:"_blank",rel:"noopener noreferrer"},"GDACS"),
        " and ",el("a",{href:"https://nasstatus.faa.gov/",target:"_blank",rel:"noopener noreferrer"},"FAA NAS status")));
  }
  NETWORK_DATA={points,routes};
  requestAnimationFrame(()=>updateNetworkMap());
  loadAirportEvents(points);
  return NETWORK_CARD;
}

// ---------- airport alerts: hazards and airport status near the network ----------
let EVENTS_SIG="",EVENTS_AT=0,EVENTS_CODES="",EVENT_LEVELS=new Map();
const EVENT_RANK={red:0,orange:1,green:2};
async function loadAirportEvents(points) {
  const codes=[...new Set(points.map(p=>p.iata))].sort().join(",");
  if(codes===EVENTS_CODES&&Date.now()-EVENTS_AT<60000)return;
  EVENTS_CODES=codes;EVENTS_AT=Date.now();
  const data=await api("/api/airport-events?airports="+encodeURIComponent(codes)).catch(()=>null);
  const sig=JSON.stringify(data);if(sig===EVENTS_SIG)return;EVENTS_SIG=sig;
  renderAirportEvents(data);
}
function eventDate(iso) {return iso?new Date(iso).toLocaleDateString("en-GB",{day:"numeric",month:"short"}):"";}
function eventItem_(e) {
  const go=()=>focusAirport(e.airports[0].iata);
  return el("article",{class:"event-item "+e.level,tabindex:"0",role:"button","aria-label":e.title+". Show on map",onclick:go,onkeydown:k=>{if(["Enter"," "].includes(k.key)){k.preventDefault();go();}}},
    el("div",{class:"event-top"},el("span",{class:"event-dot"}),el("strong",{},e.title)),
    el("div",{class:"event-meta"},el("span",{class:"badge-s "+e.level},e.kind),...e.airports.slice(0,4).map(a=>el("span",{class:"chip"},a.distance_km?a.iata+" · "+a.distance_km+" km":a.iata))),
    e.detail?el("p",{class:"caption"},e.detail):null,
    el("div",{class:"event-foot caption"},el("span",{},[e.source,e.starts_at?eventDate(e.starts_at)+(e.ends_at?" to "+eventDate(e.ends_at):""):""].filter(Boolean).join(" · ")),
      e.url?el("a",{href:e.url,target:"_blank",rel:"noopener noreferrer",onclick:k=>k.stopPropagation()},"Details"):null));
}
function renderAirportEvents(data) {
  const list=NETWORK_CARD.querySelector(".event-list"),head=NETWORK_CARD.querySelector(".event-head");
  EVENT_LEVELS=new Map();
  head.replaceChildren(el("h3",{},"Airport alerts"));
  if(!data)list.replaceChildren(emptyState("alert","Alerts could not be loaded."));
  else if(!data.enabled)list.replaceChildren(emptyState("check","Alerts are switched off (AIRPORT_EVENTS)."));
  else {
    const serious=data.events.filter(e=>e.level!=="green").length,down=data.sources.filter(s=>!s.ok).map(s=>s.name);
    head.append(el("span",{class:"badge-s "+(serious?"orange":"green")},serious?serious+" active":"All clear"),
      down.length?el("p",{class:"caption event-warn"},down.join(" and ")+" not reachable, showing the last known state."):"");
    for(const e of data.events)for(const a of e.airports){const cur=EVENT_LEVELS.get(a.iata);if(!cur||EVENT_RANK[e.level]<EVENT_RANK[cur])EVENT_LEVELS.set(a.iata,e.level);}
    list.replaceChildren(...(data.events.length?data.events.map(eventItem_):[emptyState("check","No known events near your airports right now.")]));
  }
  markAffectedAirports();
}
async function focusAirport(iata) {
  const airports=await (AIRPORT_COORDS||Promise.resolve(null)).catch(()=>null),a=airports?.[iata];if(!a||!NETWORK_MAP)return;
  NETWORK_MAP.flyTo({center:[a.lon,a.lat],zoom:5,duration:matchMedia("(prefers-reduced-motion:reduce)").matches?0:800});
}
function markAffectedAirports() {
  for(const [code,marker] of NETWORK_MARKERS){const level=EVENT_LEVELS.get(code),node=marker.getElement();node.classList.remove("alert-red","alert-orange","alert-green");if(level)node.classList.add("alert-"+level);}
}
function mapStatus(text,retry=false) {
  const status=NETWORK_CARD.querySelector(".map-status");status.hidden=!text;
  status.replaceChildren(...[text,retry?el("button",{class:"btn ghost",onclick:()=>{NETWORK_MAP?.remove();NETWORK_MAP=null;NETWORK_MARKERS.clear();updateNetworkMap();}},"Reload"):null].filter(item=>item!==null));
}
async function updateNetworkMap() {
  if(!NETWORK_CARD?.isConnected)return;
  if(NETWORK_MAP){NETWORK_MAP.resize();if(NETWORK_MAP.getSource("requested-routes"))drawNetwork();return;}
  try {
    MAP_LIBRARY ||= import("/static/vendor/maplibre/maplibre-gl.mjs").then(lib=>{lib.setWorkerUrl(new URL("/static/vendor/maplibre/maplibre-gl-worker.mjs",location.href).href);lib.setWorkerCount(2);return lib;}).catch(error=>{MAP_LIBRARY=null;throw error;});
    AIRPORT_COORDS ||= api("/static/geo/airports.json").catch(error=>{AIRPORT_COORDS=null;throw error;});
    const [lib]=await Promise.all([MAP_LIBRARY,AIRPORT_COORDS]);
    if(NETWORK_MAP||!NETWORK_CARD.isConnected)return;
    mapStatus("Loading map …");
    const map=NETWORK_MAP=new lib.Map({container:NETWORK_CARD.querySelector(".openfreemap"),style:OPENFREEMAP_STYLE,
      center:[8,22],zoom:1.2,minZoom:.5,maxZoom:12,maplibreLogo:true,
      attributionControl:{compact:true},
      locale:{"NavigationControl.ZoomIn":"Zoom in","NavigationControl.ZoomOut":"Zoom out","NavigationControl.ResetBearing":"Reset north"}});
    map.addControl(new lib.NavigationControl({showCompass:false}),"top-right");
    map.on("style.load",()=>{
      const color=getComputedStyle(document.documentElement).getPropertyValue("--blue").trim()||"#007AFF";
      map.addSource("requested-routes",{type:"geojson",data:{type:"FeatureCollection",features:[]}});
      map.addLayer({id:"route-glow",type:"line",source:"requested-routes",paint:{"line-color":color,"line-width":7,"line-opacity":.09}});
      map.addLayer({id:"flight-connections",type:"line",source:"requested-routes",layout:{"line-cap":"round","line-join":"round"},paint:{"line-color":color,"line-width":["interpolate",["linear"],["get","requests"],1,1.4,30,3],"line-opacity":.6}});
      drawNetwork();mapStatus("");
    });
    map.on("error",()=>mapStatus("Map data could not be loaded completely.",true));
    map.on("mouseenter","flight-connections",()=>map.getCanvas().style.cursor="pointer");
    map.on("mouseleave","flight-connections",()=>map.getCanvas().style.cursor="");
    map.on("click","flight-connections",event=>{
      const route=event.features?.[0]?.properties;if(!route)return;
      new lib.Popup().setLngLat(event.lngLat).setDOMContent(el("div",{},el("strong",{},route.origin+" to "+route.destination),el("p",{},route.requests+" requests"))).addTo(map);
    });
  } catch {mapStatus("The map could not be loaded.",true);}
}
async function drawNetwork() {
  if(!NETWORK_MAP?.getSource("requested-routes")||!NETWORK_DATA)return;
  const map=NETWORK_MAP;
  const [airports,lib]=await Promise.all([AIRPORT_COORDS,MAP_LIBRARY]);
  if(map!==NETWORK_MAP||!map.getSource("requested-routes"))return;
  const {points,routes}=NETWORK_DATA;
  NETWORK_MAP.getSource("requested-routes").setData(networkGeoJSON(points,routes,airports));
  const visible=new Set();
  for(const point of points) {
    const airport=airports[point.iata];if(!airport)continue;visible.add(point.iata);
    let marker=NETWORK_MARKERS.get(point.iata);
    if(!marker) {
      const button=el("button",{type:"button",class:"airport-marker",onclick:()=>openAirport(point.iata)},el("span",{class:"airport-dot"}),el("span",{class:"airport-code"},point.iata));
      marker=new lib.Marker({element:button}).setLngLat([airport.lon,airport.lat]).addTo(NETWORK_MAP);NETWORK_MARKERS.set(point.iata,marker);
    }
    const title=`${point.iata} · ${airport.name}\n${point.departures} departures · ${point.arrivals} arrivals`;
    marker.getElement().title=title;marker.getElement().setAttribute("aria-label",title+". Open requests");
  }
  for(const [code,marker] of NETWORK_MARKERS)if(!visible.has(code)){marker.remove();NETWORK_MARKERS.delete(code);}
  markAffectedAirports();
}
