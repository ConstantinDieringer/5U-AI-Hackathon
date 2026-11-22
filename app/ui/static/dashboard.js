"use strict";
const WORLD_API = "https://cdn.jsdelivr.net/npm/world-atlas@2.0.2/countries-110m.json";
let GEODATA;
function dashboardAmounts(data,key) { return data.amounts[key].length ? data.amounts[key].map(a=>money(a.minor,a.currency)).join(" · ") : "0 €"; }
async function loadOverview() {
  if(isSelectOpen())return;
  const version=S.dashboardVersion=(S.dashboardVersion||0)+1;
  const data=await api("/api/business-metrics?days="+(S.metricDays ?? 1)).catch(()=>null);
  if(S.page!=="overview" || version!==S.dashboardVersion) return;
  if(!data) {$("#overview").replaceChildren(el("div",{class:"card s12"},"Dashboard konnte nicht geladen werden.",el("button",{class:"btn",onclick:loadOverview},"Erneut versuchen")));return;}
  const kpis=[["Gebuchter Auftragswert",dashboardAmounts(data,"booked"),"Vereinbarte Kundenbeträge","hero"],
    ["Offenes Angebotsvolumen",dashboardAmounts(data,"pipeline"),"Aktuelle Angebote ohne Buchung"],
    ["Buchungen",data.booked, data.awaiting_price+" weitere Preisfreigaben offen"],
    ["Buchungsquote",pct(data.booked,data.requests),`${data.booked} Buchungen aus ${data.requests} Anfragen`]];
  $("#view-controls").replaceChildren(crmSelect("Zeitraum",[[1,"Letzte 24 Stunden"],[7,"Letzte 7 Tage"],[30,"Letzte 30 Tage"],[0,"Gesamter Zeitraum"]],S.metricDays ?? 1,e=>{S.metricDays=Number(e.target.value);loadOverview();}));
  const map=el("section",{class:"card s12 airport-map-card"},el("div",{class:"card-h"},el("div",{},el("h2",{},"Euer weltweites Netzwerk"),el("p",{class:"caption"},"Abflug- und Zielflughäfen aus euren Anfragen. Ziehen zum Verschieben und scrollen zum Zoomen."))),el("div",{class:"map-loading",role:"status"},"Karte wird geladen …"));
  $("#overview").replaceChildren(...kpis.map(([label,value,sub,tone])=>el("div",{class:"card kpi s3 "+(tone||"")},el("div",{class:"label"},label),el("div",{class:"value"},String(value)),el("div",{class:"caption"},sub))),
    ...(data.unknown_booked_value ? [el("p",{class:"s12 caption"},`${data.unknown_booked_value} Buchungen ohne gesicherten Kundenbetrag fehlen im Auftragswert.`)] : []), volumeChart(data),map);
  renderWorldMap(map,data.airports||[],data.routes||[]);
}
function chartBuckets(events,days) {
  const now=Date.now(), times=events.map(e=>new Date(e.at).getTime()).filter(Number.isFinite);
  const from=days ? now-days*86400000 : times.length ? Math.min(...times) : now-86400000;
  const span=Math.max(now-from,3600000), step=span<=2*86400000 ? 3600000 : Math.ceil(span/(31*86400000))*86400000;
  const start=Math.floor(from/step)*step, end=Math.floor(now/step)*step;
  const buckets=Array.from({length:Math.floor((end-start)/step)+1},(_,i)=>({at:start+i*step,minor:0}));
  for(const event of events) {const i=Math.floor((new Date(event.at).getTime()-start)/step);if(i>=0 && i<buckets.length) buckets[i].minor+=event.minor;}
  return {buckets,step};
}
function volumeChart(data) {
  const currencies=data.amounts.booked.map(a=>a.currency), currency=currencies.includes(S.chartCurrency)?S.chartCurrency:currencies.includes("EUR")?"EUR":currencies[0]||"EUR";
  const events=(data.booked_events||[]).filter(e=>e.currency===currency), {buckets,step}=chartBuckets(events,data.days);
  const card=el("section",{class:"card s12 volume-chart"},el("div",{class:"card-h"},el("div",{},el("h2",{},"Gebuchtes Auftragsvolumen"),el("p",{class:"caption"},"Vereinbarte Kundenbeträge je Buchungszeitpunkt. Währungen werden getrennt dargestellt.")),
    currencies.length>1 ? crmSelect("Währung",currencies.map(c=>[c,c]),currency,e=>{S.chartCurrency=e.target.value;loadOverview();}) : el("span",{class:"badge-s aside"},currency==="EUR"?"€":currency)));
  if(!events.length) {card.append(emptyState("box","Noch kein gebuchter Auftragswert in diesem Zeitraum."));return card;}
  const small=matchMedia("(max-width:600px)").matches;
  const width=small?640:1000,height=small?340:280,left=small?115:85,right=20,top=20,bottom=45,inner=width-left-right,plot=height-top-bottom;
  const max=Math.max(1,...buckets.map(b=>b.minor)), scale=plot/max, slot=inner/buckets.length;
  const chart=svg("svg",{viewBox:`0 0 ${width} ${height}`,class:"volume-svg",role:"img","aria-label":"Gebuchtes Auftragsvolumen nach Buchungszeitpunkt"});
  for(let i=0;i<=4;i++) {const y=top+plot-i*plot/4;chart.append(svg("line",{x1:left,x2:width-right,y1:y,y2:y,class:"chart-grid"}),svgText(money(Math.round(max*i/4),currency),{x:left-10,y:y+4,"text-anchor":"end",class:"chart-label"}));}
  const label=at=>new Date(at).toLocaleString("de-DE",step<86400000?{hour:"2-digit",minute:"2-digit"}:{day:"numeric",month:"short"});
  const coords=buckets.map((b,i)=>[left+(i+.5)*slot,top+plot-b.minor*scale]);
  const line=coords.map(([x,y],i)=>(i?"L":"M")+x.toFixed(2)+","+y.toFixed(2)).join(" ");
  const defs=svg("defs"),gradient=svg("linearGradient",{id:"volume-gradient",x1:0,y1:0,x2:0,y2:1});
  gradient.append(svg("stop",{offset:"0%","stop-color":"var(--blue)","stop-opacity":".22"}),svg("stop",{offset:"100%","stop-color":"var(--blue)","stop-opacity":".015"}));defs.append(gradient);
  chart.append(defs,svg("path",{d:line+` L${coords.at(-1)[0]},${top+plot} L${coords[0][0]},${top+plot} Z`,class:"volume-area",fill:"url(#volume-gradient)"}),svg("path",{d:line,class:"volume-line",pathLength:1}));
  buckets.forEach((bucket,i)=>{
    const [cx,cy]=coords[i],point=svg("circle",{cx,cy,r:bucket.minor?4:2,class:"volume-dot",tabindex:bucket.minor?"0":null,role:"img","aria-label":`${label(bucket.at)} ${money(bucket.minor,currency)}`});
    const title=svg("title");title.textContent=new Date(bucket.at).toLocaleString("de-DE")+" · "+money(bucket.minor,currency);point.append(title);chart.append(point);
    if(i%Math.max(1,Math.ceil(buckets.length/7))===0)chart.append(svgText(label(bucket.at),{x:cx,y:height-12,"text-anchor":"middle",class:"chart-label"}));
  });
  const table=el("details",{class:"chart-data"},el("summary",{},"Diagrammdaten anzeigen"),el("div",{class:"table-scroll"},el("table",{class:"list"},el("thead",{},el("tr",{},el("th",{},"Zeitpunkt"),el("th",{},"Auftragsvolumen"))),el("tbody",{},...buckets.map(b=>el("tr",{},el("td",{},new Date(b.at).toLocaleString("de-DE")),el("td",{},money(b.minor,currency))))))));
  card.append(chart,table);return card;
}
function svgText(text,attrs) {const node=svg("text",attrs);node.textContent=text;return node;}
async function geoData() {
  if(!GEODATA) GEODATA=(async()=>{
    let world;
    try {const r=await fetch(WORLD_API,{signal:AbortSignal.timeout(3500),cache:"force-cache",credentials:"omit",referrerPolicy:"no-referrer"});if(!r.ok)throw new Error();world=await r.json();if(world.type!=="Topology"||!Array.isArray(world.arcs)||!world.objects?.countries)throw new Error();}
    catch {world=await api("/static/geo/world.json");}
    return {world,airports:await api("/static/geo/airports.json")};
  })().catch(error=>{GEODATA=null;throw error;});
  return GEODATA;
}
function worldPaths(world) {
  const arcs=world.arcs.map(arc=>{let x=0,y=0;return arc.map(([dx,dy])=>{x+=dx;y+=dy;return [x*world.transform.scale[0]+world.transform.translate[0],y*world.transform.scale[1]+world.transform.translate[1]];});});
  const ring=ids=>ids.flatMap((id,i)=>{const points=id<0?[...arcs[~id]].reverse():arcs[id];return i?points.slice(1):points;});
  return world.objects.countries.geometries.map(country=>{
    const polygons=country.type==="Polygon"?[country.arcs]:country.arcs;
    return polygons.flatMap(poly=>poly.map(ids=>{let previous;return ring(ids).map(([lon,lat],i)=>{const x=(lon+180)*1000/360,y=(90-lat)*500/180,move=!i||Math.abs(x-previous)>500;previous=x;return `${move?"M":"L"}${x.toFixed(2)},${y.toFixed(2)}`;}).join(" ")+"Z";})).join(" ");
  });
}
async function renderWorldMap(card,points,routes) {
  try {
    const {world,airports}=await geoData();if(!card.isConnected)return;
    const graph=svg("svg",{viewBox:"0 0 1000 500",class:"world-map",role:"group",tabindex:0,"aria-label":"Weltkarte mit angefragten Flughäfen. Scrollen zum Zoomen, ziehen zum Verschieben. Pfeiltasten verschieben die Karte."});
    graph.append(svg("rect",{width:1000,height:500,class:"map-ocean",rx:16}));
    worldPaths(world).forEach(d=>graph.append(svg("path",{d,class:"map-country","fill-rule":"evenodd"})));
    const known=points.filter(p=>airports[p.iata]);
    const connectionLayer=svg("g",{class:"route-layer","aria-label":"Angefragte Verbindungen"});
    for(const route of routes) {
      const a=airports[route.origin],b=airports[route.destination];if(!a||!b||route.origin===route.destination)continue;
      const x1=(a.lon+180)*1000/360,y1=(90-a.lat)*500/180,y2=(90-b.lat)*500/180;
      let x2=(b.lon+180)*1000/360;if(x2-x1>500)x2-=1000;if(x2-x1< -500)x2+=1000;
      const dx=x2-x1,dy=y2-y1,distance=Math.hypot(dx,dy)||1,bend=Math.min(65,distance*.18);
      const cx=(x1+x2)/2+dy/distance*bend,cy=(y1+y2)/2-dx/distance*bend;
      for(const shift of [-1000,0,1000]) {
        const path=svg("path",{d:`M${x1+shift},${y1} Q${cx+shift},${cy} ${x2+shift},${y2}`,class:"flight-route","data-origin":route.origin,"data-destination":route.destination,"stroke-width":1+Math.min(1.5,Math.log2(1+route.requests)/3),pathLength:1});
        const title=svg("title");title.textContent=`${route.origin} nach ${route.destination} · ${route.requests} Anfragen`;path.append(title);connectionLayer.append(path,svg("path",{d:path.getAttribute("d"),class:"flight-flow",pathLength:1,"aria-hidden":"true"}));
      }
    }
    graph.append(connectionLayer);
    for(const point of known) {
      const a=airports[point.iata],x=(a.lon+180)*1000/360,y=(90-a.lat)*500/180;
      const marker=svg("g",{class:"map-airport",transform:`translate(${x},${y})`,tabindex:0,role:"button","aria-label":`${point.iata} ${a.name}, ${point.departures} Abflüge und ${point.arrivals} Ankünfte. Anfragen öffnen`});
      const title=svg("title");title.textContent=`${point.iata} · ${a.name}\n${point.departures} Abflüge · ${point.arrivals} Ankünfte`;
      marker.append(title,svg("circle",{r:10,class:"map-hit"}),svg("circle",{r:4+Math.min(5,Math.log2(1+point.departures+point.arrivals)),class:"map-point"}),svgText(point.iata,{x:11,y:4}));
      marker.addEventListener("click",()=>openAirport(point.iata));marker.addEventListener("keydown",e=>{if(["Enter"," "].includes(e.key)){e.preventDefault();openAirport(point.iata);}});graph.append(marker);
    }
    let zoom=S.mapView?.zoom || 1,cx=S.mapView?.cx ?? 500,cy=S.mapView?.cy ?? 250,drag;
    const frame=()=>{const w=1000/zoom,h=500/zoom;cx=Math.max(w/2,Math.min(1000-w/2,cx));cy=Math.max(h/2,Math.min(500-h/2,cy));graph.setAttribute("viewBox",`${cx-w/2} ${cy-h/2} ${w} ${h}`);S.mapView={zoom,cx,cy};};
    const zoomBy=(factor,anchor)=>{const before=zoom;zoom=Math.max(1,Math.min(8,zoom*factor));if(anchor){cx+=anchor.x*(1000/before-1000/zoom);cy+=anchor.y*(500/before-500/zoom);}frame();};
    frame();
    graph.addEventListener("wheel",e=>{e.preventDefault();const r=graph.getBoundingClientRect();zoomBy(Math.exp(-Math.max(-120,Math.min(120,e.deltaY))*.003),{x:(e.clientX-r.left)/r.width-.5,y:(e.clientY-r.top)/r.height-.5});},{passive:false});
    graph.addEventListener("keydown",e=>{if(e.target!==graph)return;if(["+","=","-","ArrowLeft","ArrowRight","ArrowUp","ArrowDown"].includes(e.key)){e.preventDefault();if(["+","="].includes(e.key))zoomBy(1.5);else if(e.key==="-")zoomBy(1/1.5);else{cx+=(e.key==="ArrowRight"?1:e.key==="ArrowLeft"?-1:0)*80/zoom;cy+=(e.key==="ArrowDown"?1:e.key==="ArrowUp"?-1:0)*40/zoom;frame();}}});
    graph.addEventListener("pointerdown",e=>{if(zoom>1&&!e.target.closest(".map-airport")){drag={x:e.clientX,y:e.clientY,cx,cy};graph.setPointerCapture(e.pointerId);}});
    graph.addEventListener("pointermove",e=>{if(drag){const bounds=graph.getBoundingClientRect();cx=drag.cx-(e.clientX-drag.x)*(1000/zoom)/bounds.width;cy=drag.cy-(e.clientY-drag.y)*(500/zoom)/bounds.height;frame();}});
    graph.addEventListener("pointerup",()=>{drag=null;});graph.addEventListener("pointercancel",()=>{drag=null;});
    const buttons=el("div",{class:"map-controls"},el("button",{class:"btn","aria-label":"Karte vergrößern",onclick:()=>zoomBy(1.5)},"+"),el("button",{class:"btn","aria-label":"Karte verkleinern",onclick:()=>zoomBy(1/1.5)},"−"),el("button",{class:"btn",onclick:()=>{zoom=1;cx=500;cy=250;frame();}},"Weltansicht"));
    const missing=points.filter(p=>!airports[p.iata]);
    card.querySelector(".map-loading")?.remove();card.append(...[buttons,graph,
      !points.length?el("p",{class:"caption"},"Flughäfen erscheinen, sobald eine Route erkannt wurde."):null,
      missing.length?el("p",{class:"caption"},`${missing.length} Flughäfen haben noch keine bestätigten Koordinaten.`):null,
      el("p",{class:"caption map-source"},el("a",{href:"https://github.com/topojson/world-atlas",target:"_blank",rel:"noopener noreferrer"},"Natural Earth / World Atlas")," · ",el("a",{href:"https://github.com/mwgg/Airports",target:"_blank",rel:"noopener noreferrer"},"Flughafendaten mwgg/Airports"))].filter(Boolean));
  } catch {const loading=card.querySelector(".map-loading");if(loading)loading.textContent="Die Karte ist momentan nicht verfügbar. Bitte erneut laden.";}
}
function openAirport(iata) {S.q="";$("#global-q").value="";S.requestStatus="";S.requestKind="rfq";S.requestDays=S.metricDays ?? 1;S.requestAirport=iata;S.requestOffset=0;show("requests");}

async function loadRequests() {
  if(isSelectOpen())return;
  if(!S.airportOptions){S.airportOptions=await airportOptions();if(S.page!=="requests")return;}
  const version=S.requestsVersion=(S.requestsVersion||0)+1;
  const params=new URLSearchParams({q:S.q||"",status:S.requestStatus||"",kind:S.requestKind||"",airport:S.requestAirport||"",days:S.requestDays||0,sort:S.requestSort||"created_at",direction:S.requestAsc?"asc":"desc",offset:S.requestOffset||0,limit:50});
  const data=await api("/api/requests?"+params).catch(()=>null);
  if(S.page!=="requests"||version!==S.requestsVersion)return;
  if(!data){$("#requests").replaceChildren(emptyState("alert","Anfragen konnten nicht geladen werden."),el("button",{class:"btn",onclick:loadRequests},"Erneut versuchen"));return;}
  if(data.total && data.offset>=data.total){S.requestOffset=Math.floor((data.total-1)/50)*50;loadRequests();return;}
  const resetOffset=callback=>e=>{callback(e.target.value);S.requestOffset=0;loadRequests();};
  const filters=el("div",{class:"request-filters"},
    crmSelect("Status",[["","Alle Status"],...Object.entries(STATUS).map(([key,[label]])=>[key,label])],S.requestStatus||"",resetOffset(v=>S.requestStatus=v)),
    crmSelect("Art",[["","Alle Anfragen"],["rfq","Frachtanfragen"],["tracking","Trackinganfragen"]],S.requestKind||"",resetOffset(v=>S.requestKind=v)),
    crmSelect("Zeitraum",[[0,"Gesamter Zeitraum"],[1,"Letzte 24 Stunden"],[7,"Letzte 7 Tage"],[30,"Letzte 30 Tage"]],S.requestDays||0,resetOffset(v=>S.requestDays=Number(v))),
    crmSelect("Flughafen",S.airportOptions||[["","Alle Flughäfen"]],S.requestAirport||"",resetOffset(v=>S.requestAirport=v)),
    el("button",{class:"btn ghost",onclick:()=>{S.q="";$("#global-q").value="";S.requestStatus="";S.requestKind="";S.requestDays=0;S.requestAirport="";S.requestSort="created_at";S.requestAsc=false;S.requestOffset=0;loadRequests();}},"Filter zurücksetzen"));
  const columns=[["id","Anfrage"],["created_at","Eingang"],["customer","Kunde"],["origin","Abflug"],["destination","Ziel"],["weight_kg","Gewicht"],["total_minor","Angebot"],["status","Status"]];
  const head=el("thead",{},el("tr",{},...columns.map(([key,label])=>el("th",{"aria-sort":(S.requestSort||"created_at")===key?(S.requestAsc?"ascending":"descending"):"none"},el("button",{class:"sort-button",onclick:()=>{S.requestAsc=(S.requestSort||"created_at")===key?!S.requestAsc:true;S.requestSort=key;S.requestOffset=0;loadRequests();}},label,(S.requestSort||"created_at")===key?(S.requestAsc?" ↑":" ↓"):" ↕")))));
  const rows=data.items.map(row=>el("tr",{tabindex:0,onclick:()=>select(row.id),onkeydown:e=>{if(["Enter"," "].includes(e.key)){e.preventDefault();select(row.id);}}},
    el("td",{},el("strong",{},"#"+row.id),el("div",{class:"caption"},row.kind==="tracking"?"Tracking":"Frachtanfrage")),el("td",{},dateTime(row.created_at)),el("td",{},who(row.customer)),el("td",{},row.origin||"Noch offen"),el("td",{},row.destination||"Noch offen"),el("td",{class:"num"},row.weight_kg==null?"–":row.weight_kg.toLocaleString("de-DE")+" kg"),el("td",{class:"num"},money(row.total_minor,row.currency)),el("td",{},badge(row.status),row.paused?el("span",{class:"badge-s orange"},"Pausiert"):null)));
  $("#requests").replaceChildren(el("section",{class:"card s12"},el("div",{class:"card-h"},el("h2",{},"Alle Anfragen"),el("span",{class:"aside caption",role:"status"},`${data.total} ${data.total===1?"Anfrage":"Anfragen"}${S.q?" für „"+S.q+"“":""}`)),filters,
    rows.length?el("div",{class:"table-scroll",role:"region","aria-label":"Alle Anfragen",tabindex:0},el("table",{class:"list request-table"},head,el("tbody",{},...rows))):emptyState("search","Keine Anfragen für diese Filter."),
    el("div",{class:"table-footer"},el("span",{class:"caption"},data.total?`${data.offset+1} bis ${Math.min(data.offset+50,data.total)} von ${data.total}`:"0 Anfragen"),el("div",{class:"actions"},el("button",{class:"btn",disabled:!data.offset,onclick:()=>{S.requestOffset=Math.max(0,data.offset-50);loadRequests();}},"Zurück"),el("button",{class:"btn",disabled:data.offset+50>=data.total,onclick:()=>{S.requestOffset=data.offset+50;loadRequests();}},"Weiter")))));
}
