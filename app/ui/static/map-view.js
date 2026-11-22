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
    const reset=el("button",{class:"btn",onclick:()=>NETWORK_MAP?.flyTo({center:[8,22],zoom:1.2,bearing:0,pitch:0,duration:matchMedia("(prefers-reduced-motion:reduce)").matches?0:600})},"Weltansicht");
    NETWORK_CARD=el("section",{class:"card s12 airport-map-card"},
      el("div",{class:"card-h"},el("div",{},el("h2",{},"Euer weltweites Netzwerk"),el("p",{class:"caption"},"Angefragte Strecken zwischen euren Flughäfen. Ziehen, scrollen und zoomen.")),reset),
      el("div",{id:"airport-map",class:"openfreemap","aria-label":"Flughäfen und angefragte Flugverbindungen auf OpenFreeMap"}),
      el("div",{class:"map-status caption",role:"status"},"Positron Karte wird geladen …"),
      el("p",{class:"caption map-source"},el("a",{href:"https://github.com/mwgg/Airports",target:"_blank",rel:"noopener noreferrer"},"Flughafendaten mwgg/Airports")));
  }
  NETWORK_DATA={points,routes};
  requestAnimationFrame(()=>updateNetworkMap());
  return NETWORK_CARD;
}
function mapStatus(text,retry=false) {
  const status=NETWORK_CARD.querySelector(".map-status");status.hidden=!text;
  status.replaceChildren(...[text,retry?el("button",{class:"btn ghost",onclick:()=>{NETWORK_MAP?.remove();NETWORK_MAP=null;NETWORK_MARKERS.clear();updateNetworkMap();}},"Erneut laden"):null].filter(item=>item!==null));
}
async function updateNetworkMap() {
  if(!NETWORK_CARD?.isConnected)return;
  if(NETWORK_MAP){NETWORK_MAP.resize();if(NETWORK_MAP.getSource("requested-routes"))drawNetwork();return;}
  try {
    MAP_LIBRARY ||= import("/static/vendor/maplibre/maplibre-gl.mjs").then(lib=>{lib.setWorkerUrl(new URL("/static/vendor/maplibre/maplibre-gl-worker.mjs",location.href).href);lib.setWorkerCount(2);return lib;}).catch(error=>{MAP_LIBRARY=null;throw error;});
    AIRPORT_COORDS ||= api("/static/geo/airports.json").catch(error=>{AIRPORT_COORDS=null;throw error;});
    const [lib]=await Promise.all([MAP_LIBRARY,AIRPORT_COORDS]);
    if(NETWORK_MAP||!NETWORK_CARD.isConnected)return;
    mapStatus("Positron Karte wird geladen …");
    const map=NETWORK_MAP=new lib.Map({container:NETWORK_CARD.querySelector(".openfreemap"),style:OPENFREEMAP_STYLE,
      center:[8,22],zoom:1.2,minZoom:.5,maxZoom:12,maplibreLogo:true,
      attributionControl:{compact:true},
      locale:{"NavigationControl.ZoomIn":"Vergrößern","NavigationControl.ZoomOut":"Verkleinern","NavigationControl.ResetBearing":"Norden anzeigen"}});
    map.addControl(new lib.NavigationControl({showCompass:false}),"top-right");
    map.on("style.load",()=>{
      const color=getComputedStyle(document.documentElement).getPropertyValue("--blue").trim()||"#007AFF";
      map.addSource("requested-routes",{type:"geojson",data:{type:"FeatureCollection",features:[]}});
      map.addLayer({id:"route-glow",type:"line",source:"requested-routes",paint:{"line-color":color,"line-width":7,"line-opacity":.09}});
      map.addLayer({id:"flight-connections",type:"line",source:"requested-routes",layout:{"line-cap":"round","line-join":"round"},paint:{"line-color":color,"line-width":["interpolate",["linear"],["get","requests"],1,1.4,30,3],"line-opacity":.6}});
      drawNetwork();mapStatus("");
    });
    map.on("error",()=>mapStatus("Kartendaten konnten nicht vollständig geladen werden.",true));
    map.on("mouseenter","flight-connections",()=>map.getCanvas().style.cursor="pointer");
    map.on("mouseleave","flight-connections",()=>map.getCanvas().style.cursor="");
    map.on("click","flight-connections",event=>{
      const route=event.features?.[0]?.properties;if(!route)return;
      new lib.Popup().setLngLat(event.lngLat).setDOMContent(el("div",{},el("strong",{},route.origin+" nach "+route.destination),el("p",{},route.requests+" Anfragen"))).addTo(map);
    });
  } catch {mapStatus("Die Positron Karte konnte nicht geladen werden.",true);}
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
    const title=`${point.iata} · ${airport.name}\n${point.departures} Abflüge · ${point.arrivals} Ankünfte`;
    marker.getElement().title=title;marker.getElement().setAttribute("aria-label",title+". Anfragen öffnen");
  }
  for(const [code,marker] of NETWORK_MARKERS)if(!visible.has(code)){marker.remove();NETWORK_MARKERS.delete(code);}
}
