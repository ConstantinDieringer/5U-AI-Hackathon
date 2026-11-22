"use strict";
function dashboardAmounts(data,key) { return data.amounts[key].length ? data.amounts[key].map(a=>money(a.minor,a.currency)).join(" · ") : "0 €"; }
async function loadOverview() {
  if(isSelectOpen())return;
  const version=S.dashboardVersion=(S.dashboardVersion||0)+1;
  const [data,volume]=await Promise.all([api("/api/business-metrics?days="+(S.metricDays ?? 1)).catch(()=>null),api("/api/booking-volume").catch(()=>null)]);
  if(S.page!=="overview" || version!==S.dashboardVersion) return;
  if(!data) {$("#overview").replaceChildren(el("div",{class:"card s12"},"Dashboard konnte nicht geladen werden.",el("button",{class:"btn",onclick:loadOverview},"Erneut versuchen")));return;}
  const kpis=[["Gebuchter Auftragswert",dashboardAmounts(data,"booked"),"Vereinbarte Kundenbeträge","hero"],
    ["Offenes Angebotsvolumen",dashboardAmounts(data,"pipeline"),"Aktuelle Angebote ohne Buchung"],
    ["Buchungen",data.booked, data.awaiting_price+" weitere Preisfreigaben offen"],
    ["Buchungsquote",pct(data.booked,data.requests),`${data.booked} Buchungen aus ${data.requests} Anfragen`]];
  $("#view-controls").replaceChildren(crmSelect("Zeitraum",[[1,"Letzte 24 Stunden"],[7,"Letzte 7 Tage"],[30,"Letzte 30 Tage"],[0,"Gesamter Zeitraum"]],S.metricDays ?? 1,e=>{S.metricDays=Number(e.target.value);loadOverview();}));
  const map=airportMapCard(data.airports||[],data.routes||[]);
  $("#overview").replaceChildren(...kpis.map(([label,value,sub,tone])=>el("div",{class:"card kpi s3 "+(tone||"")},el("div",{class:"label"},label),el("div",{class:"value"},String(value)),el("div",{class:"caption"},sub))),
    ...(data.unknown_booked_value ? [el("p",{class:"s12 caption"},`${data.unknown_booked_value} Buchungen ohne gesicherten Kundenbetrag fehlen im Auftragswert.`)] : []), volumeChart(volume),map);
}
function chartBuckets(events,from,to) {
  const end=new Date(to).getTime(),start=new Date(from).getTime(),step=3600000;
  const buckets=Array.from({length:24},(_,i)=>({at:start+i*step,minor:0}));
  for(const event of events){const at=new Date(event.at).getTime();if(at<start||at>end)continue;const i=Math.min(23,Math.floor((at-start)/step));if(i>=0)buckets[i].minor+=event.minor;}
  return {buckets,step,start,end};
}
function volumeChart(data) {
  if(!data)return el("section",{class:"card s12"},el("h2",{},"Gebuchtes Auftragsvolumen"),el("p",{class:"caption"},"Das 24 Stunden Diagramm ist momentan nicht verfügbar."));
  const currencies=data.amounts.map(a=>a.currency), currency=currencies.includes(S.chartCurrency)?S.chartCurrency:currencies.includes("EUR")?"EUR":currencies[0]||"EUR";
  const events=data.events.filter(e=>e.currency===currency), {buckets,step,start,end}=chartBuckets(events,data.from,data.to);
  const card=el("section",{class:"card s12 volume-chart"},el("div",{class:"card-h"},el("div",{},el("h2",{},"Gebuchtes Auftragsvolumen"),el("p",{class:"caption"},"Letzte 24 Stunden nach Buchungszeitpunkt. Kundenbeträge je Stunde.")),
    currencies.length>1 ? crmSelect("Währung",currencies.map(c=>[c,c]),currency,e=>{S.chartCurrency=e.target.value;loadOverview();}) : el("span",{class:"badge-s aside"},currency==="EUR"?"€":currency)));
  if(!events.length)card.append(el("p",{class:"caption"},"In den letzten 24 Stunden wurden noch keine Aufträge gebucht."));
  const small=matchMedia("(max-width:600px)").matches;
  const width=small?720:1000,height=small?340:280,left=small?155:85,right=20,top=20,bottom=45,inner=width-left-right,plot=height-top-bottom;
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
  });
  for(let i=0;i<=6;i++){const at=start+(end-start)*i/6;chart.append(svgText(label(at),{x:left+inner*i/6,y:height-12,"text-anchor":i===0?"start":i===6?"end":"middle",class:"chart-label"}));}
  const table=el("details",{class:"chart-data"},el("summary",{},"Diagrammdaten anzeigen"),el("div",{class:"table-scroll"},el("table",{class:"list"},el("thead",{},el("tr",{},el("th",{},"Zeitpunkt"),el("th",{},"Auftragsvolumen"))),el("tbody",{},...buckets.map(b=>el("tr",{},el("td",{},new Date(b.at).toLocaleString("de-DE")),el("td",{},money(b.minor,currency))))))));
  card.append(chart,table);return card;
}
function svgText(text,attrs) {const node=svg("text",attrs);node.textContent=text;return node;}
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
