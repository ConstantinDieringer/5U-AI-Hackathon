"use strict";

function metricCards(data) {
  if (!data) return el("div", {class:"card s12"}, "Kennzahlen sind gerade nicht verfügbar.");
  const amounts = key => data.amounts[key].length ? data.amounts[key].map(a => money(a.minor, a.currency)).join(" · ") : "0 €";
  const values = [
    ["Gebuchter Auftragswert", amounts("booked"), "Vereinbarte Kundenbeträge, kein fakturierter Umsatz", "hero"],
    ["Offenes Angebotsvolumen", amounts("pipeline"), "Aktuelle Angebote ohne Buchung"],
    ["Angebotsvolumen", amounts("offered"), "Ein aktuelles Angebot je Anfrage"],
    ["Buchungen", data.booked, data.awaiting_price + " weitere Preisfreigaben offen"],
    ["Anfragen", data.requests, data.open + " noch in Bearbeitung"],
    ["Angebote", data.quoted, pct(data.quoted, data.requests) + " der Anfragen mit Angebot"],
    ["Buchungsquote", pct(data.booked, data.requests), "Gebuchte Aufträge je Anfrage"],
    ["Kunden", data.customers, "Unterschiedliche anfragende Kunden"],
    ["Ø Antwortzeit", secs(data.avg_response_s), "Bis zum ersten Angebot"],
    ["Angebot in unter 2 Minuten", pct(data.answered_within_120s, data.answered), "Anteil der beantworteten Anfragen"],
    ["Gebuchtes Frachtgewicht", data.booked_weight_kg == null ? "Noch unbekannt" : data.booked_weight_kg.toLocaleString("de-DE") + " kg", "Gewicht für " + data.weight_known_bookings + " Buchungen erfasst"],
    ["Offene Preisfreigaben", data.awaiting_price, "Nicht im gebuchten Auftragswert enthalten"],
  ];
  return el("section", {class:"s12", "aria-label":"Geschäftskennzahlen"},
    el("div", {class:"section-heading"}, el("div", {}, el("h2", {}, "Geschäft im Überblick"), el("p", {class:"caption"}, "Nach Eingang der Anfrage im gewählten Zeitraum. Beträge bleiben nach Währung getrennt.")),
      el("div", {class:"metric-controls"},
        el("button", {class:"btn", onclick:e => copyMail(values.map(([label,value,sub]) => `${label}\n${value}\n${sub}`).join("\n\n"),e.currentTarget)}, icon("inbox"), "Kennzahlen kopieren"),
        el("select", {"aria-label":"Zeitraum der Kennzahlen", onchange:e => { S.metricDays = Number(e.target.value); loadOverview(); }},
          ...[[0,"Gesamter Zeitraum"],[1,"Letzte 24 Stunden"],[7,"Letzte 7 Tage"],[30,"Letzte 30 Tage"]].map(([v,label]) => el("option", {value:v, selected:v === (S.metricDays || 0)}, label))))),
    data.unknown_booked_value ? el("p", {class:"caption"}, data.unknown_booked_value + " Buchungen haben keinen gesicherten Kundenbetrag und fehlen im Auftragswert.") : null,
    el("div", {class:"business-grid"}, ...values.map(([label,value,sub,tone]) => el("div", {class:"card kpi " + (tone || "")},
      el("div", {class:"label"}, label), el("div", {class:"value"}, String(value)), el("div", {class:"caption"}, sub)))));
}

function renderBreadcrumbs() {
  const node = $("#breadcrumbs"); if (!node) return;
  const crumbs = [el("button", {class:"linklike", onclick:() => show("overview")}, "Übersicht")];
  const separator = () => el("span", {"aria-hidden":"true"}, "/");
  if (S.page === "session") {
    if (["crm", "errors"].includes(S.returnPage)) crumbs.push(separator(), el("button", {class:"linklike", onclick:() => show(S.returnPage)}, PAGES[S.returnPage][0]));
    const detail = S.detail?.id === S.selected ? S.detail : null;
    crumbs.push(separator(), el("span", {"aria-current":"page", class:"crumb-current", title:detail?.subject || ""}, "Anfrage #" + S.selected + (detail?.subject ? " · " + detail.subject : "")));
  } else if (S.page !== "overview") crumbs.push(separator(), el("span", {"aria-current":"page"}, PAGES[S.page][0]));
  node.replaceChildren(...crumbs);
}

async function copyPlainText(text, button) {
  try {
    if (navigator.clipboard?.writeText) await navigator.clipboard.writeText(text);
    else throw new Error("Clipboard not available");
  } catch {
    const field = el("textarea", {style:"position:fixed;opacity:0", "aria-label":"Text zum Kopieren"});
    field.value = text; document.body.append(field); field.select();
    try { if (!document.execCommand("copy")) throw new Error("Kopieren wurde vom Browser nicht erlaubt."); }
    finally { field.remove(); button?.focus(); }
  }
  if (button) { button.title = "Kopiert"; button.setAttribute("aria-label", "Text kopiert"); }
}
async function copyMail(body, button) {
  try { await copyPlainText(body, button); button.textContent = "Kopiert"; }
  catch (e) { bufferMessage("Kopieren fehlgeschlagen. " + e.message); }
}
function mailKind(mail, session) {
  const types = {phishing:"Quarantäne", rfq:mail.in_reply_to ? "Kundenantwort" : "Frachtanfrage", tracking:"Sendungsverfolgung", tracking_notice:"Trackinghinweis", tracking_answer:"Sendungsstatus", bid:"Transportangebot", carrier_bid:"Transportangebot", bid_request:"Preisanfrage", tariff:"Tarifinformationen", booking:"Buchung", booking_request:"Buchungsauftrag", cancellation:"Stornierung", price_change:"Preisänderung", notice:"Information", followup:"Nachfrage", clarification:"Rückfrage", quote:"Angebot", booking_confirmation:"Buchungsbestätigung"};
  return types[mail.category] || (mail.direction === "out" ? "Gesendete Antwort" : isCustomerMail(mail, session) ? "Kundenantwort" : "Partnernachricht");
}
function conversationMail(mail, session) {
  return el("details", {class:"mail " + mail.direction, "data-mail-id":mail.id, "data-fold":"mail-" + mail.id},
    el("summary", {class:"mail-summary"},
      el("span", {class:"badge-s " + (mail.direction === "out" ? "blue" : "indigo")}, mailKind(mail, session)),
      el("span", {class:"mail-summary-text"}, el("strong", {}, mail.subject || "Ohne Betreff"),
        el("span", {class:"caption"}, (mail.direction === "in" ? "Von " + (mail.sender || "Unbekannt") : "An " + (mail.recipient || "Unbekannt")))),
      el("time", {class:"caption", datetime:mail.received_at}, dateTime(mail.received_at))),
    el("div", {class:"mail-tools"}, el("button", {class:"btn ghost", onclick:e => copyMail(mail.body || "", e.currentTarget)}, icon("inbox"), "Mailtext kopieren")),
    el("div", {class:"mail-body"}, mail.body || "Kein Nachrichtentext."));
}

const CRM_COLUMNS = {
  customers:[["customer","Kunde"],["requests","Anfragen"],["quoted","Angebote"],["won","Gebucht"],["lost","Verloren"],["open","Offen"],["last_contact","Letzter Kontakt"]],
  quotes:[["sent_at","Datum"],["customer","Kunde"],["route","Strecke"],["airline","Airline"],["total_minor","Angebotswert"],["outcome","Ergebnis"]],
  bookings:[["booking_reference","Referenz"],["customer","Kunde"],["airline","Airline"],["price_minor","Airlinekosten"],["status","Status"],["created_at","Datum"]],
  pricing:[["created_at","Datum"],["customer","Kunde"],["route","Strecke"],["quote_minor","Angebotswert"],["options","Verglichene Optionen"],["verdict","Ergebnis"]],
};
const CRM_STATUS = {won:"Gebucht",lost:"Verloren",open:"Offen",booked:"Gebucht",confirmed:"Bestätigt",failed:"Fehlgeschlagen",active:"Aktive Kunden",new:"Ohne Buchung",accepted:"Akzeptiert",pending:"Noch nicht bewertet"};
function crmValue(row, key) {
  if (key === "route") return [row.origin,row.destination].filter(Boolean).join(" → ");
  if (key === "options") return row.options?.length || 0;
  if (key === "booking_reference") return row.booking_reference || row.team_reference || "";
  return row[key] ?? "";
}
function crmRows() {
  const q = (S.crmQ || "").toLocaleLowerCase("de"), tab = S.crmTab;
  const since = S.crmDays ? Date.now() - S.crmDays * 86400000 : 0;
  const rows = (S.crm?.[tab] || []).filter(row => {
    const date = row.sent_at || row.last_contact || row.created_at;
    const status = tab === "customers" ? row.open > 0 ? "active" : row.won > 0 ? "won" : "new" : row.outcome || row.status || row.verdict || "pending";
    return (!q || [row.customer,row.subject,row.airline,row.booking_reference,row.team_reference,row.origin,row.destination].some(v => String(v || "").toLocaleLowerCase("de").includes(q))) &&
      (!since || new Date(date).getTime() >= since) && (!S.crmStatus || S.crmStatus === status);
  });
  const key = S.crmSort || (tab === "customers" ? "last_contact" : tab === "quotes" ? "sent_at" : "created_at");
  const direction = S.crmAsc ? 1 : -1;
  return rows.sort((a,b) => {
    const av = crmValue(a,key), bv = crmValue(b,key);
    if (["total_minor","price_minor","quote_minor"].includes(key) && a.currency !== b.currency)
      return String(a.currency || "EUR").localeCompare(String(b.currency || "EUR"));
    return direction * (typeof av === "number" && typeof bv === "number" ? av-bv : String(av).localeCompare(String(bv), "de", {numeric:true}));
  });
}
function crmSelect(label, values, current, change) {
  return el("label", {class:"filter-control"}, el("span", {class:"caption"}, label),
    el("select", {"aria-label":label, onchange:change}, ...values.map(([v,l]) => el("option", {value:v, selected:String(v) === String(current)}, l))));
}
function renderCrm() {
  if (!S.crm) return;
  const tabs = el("div", {class:"seg", role:"tablist", "aria-label":"CRM Daten"}, ...[["customers","Kunden"],["quotes","Angebote"],["bookings","Buchungen"],["pricing","Preisvergleich"]].map(([key,label]) =>
    el("button", {role:"tab", "aria-selected":S.crmTab === key, class:S.crmTab === key ? "active" : "", onclick:() => {S.crmTab=key; S.crmStatus=""; S.crmSort=""; S.crmPage=0; renderCrm();}}, label)));
  const statuses = S.crmTab === "customers" ? ["active","won","new"] : S.crmTab === "quotes" ? ["open","won","lost"] : S.crmTab === "bookings" ? ["booked","confirmed","failed"] : ["accepted","pending"];
  const filters = el("div", {class:"crm-filters"},
    el("label", {class:"search"}, icon("search"), el("input", {"aria-label":"CRM durchsuchen", placeholder:"Kunde, Strecke oder Referenz", value:S.crmQ || "", oninput:e => {S.crmQ=e.target.value; S.crmPage=0; renderCrmTable();}})),
    crmSelect("Status", [["","Alle Status"],...statuses.map(k => [k,CRM_STATUS[k]])], S.crmStatus || "", e => {S.crmStatus=e.target.value; S.crmPage=0; renderCrmTable();}),
    crmSelect("Zeitraum", [[0,"Alle"],[1,"Letzte 24 Stunden"],[7,"Letzte 7 Tage"],[30,"Letzte 30 Tage"]], S.crmDays || 0, e => {S.crmDays=Number(e.target.value); S.crmPage=0; renderCrmTable();}),
    el("button", {class:"btn ghost", onclick:() => {S.crmQ=""; S.crmStatus=""; S.crmDays=0; S.crmSort=""; S.crmAsc=false; S.crmPage=0; renderCrm();}}, "Filter zurücksetzen"),
    el("button", {class:"btn", onclick:e => copyCrmRows(e.currentTarget)}, icon("inbox"), "Liste kopieren"));
  $("#crm").replaceChildren(el("section", {class:"card s12"},
    el("div", {class:"card-h"}, el("div", {}, el("h3", {}, "Kunden und Aufträge"), el("p", {class:"caption"}, "Kundenbeziehungen, Angebote und Buchungen an einem Ort. Spaltenüberschriften sortieren die Liste."))),
    tabs, filters, el("div", {id:"crm-table"})));
  renderCrmTable();
}
async function copyCrmRows(button) {
  const cols = CRM_COLUMNS[S.crmTab], safe = value => {
    const text = String(value ?? "").replace(/[\t\r\n]/g," ");
    return /^[=+@-]/.test(text) ? "'" + text : text;
  };
  const text = [cols.map(([,label]) => label), ...crmRows().map(row => cols.map(([key]) => crmValue(row,key)))].map(row => row.map(safe).join("\t")).join("\n");
  try { await copyPlainText(text, button); }
  catch(e) { bufferMessage("CRM konnte nicht kopiert werden. " + e.message); }
}
function renderCrmTable() {
  const all = crmRows(), pageSize = 25, max = Math.max(0, Math.ceil(all.length/pageSize)-1);
  S.crmPage = Math.max(0,Math.min(S.crmPage || 0,max));
  const rows = all.slice(S.crmPage*pageSize,(S.crmPage+1)*pageSize), tab = S.crmTab;
  const key = S.crmSort || (tab === "customers" ? "last_contact" : tab === "quotes" ? "sent_at" : "created_at");
  const cell = (row, field) => {
    const value = crmValue(row,field);
    if (field === "customer") return who(value);
    if (["sent_at","last_contact","created_at"].includes(field)) return dateTime(value);
    if (["total_minor","price_minor","quote_minor"].includes(field)) return money(value === "" ? null : value, row.currency || "EUR");
    if (["status","outcome","verdict"].includes(field)) return CRM_STATUS[value] || value || "Noch offen";
    return String(value);
  };
  const table = rows.length ? el("table", {class:"list"},
    el("thead", {}, el("tr", {}, ...CRM_COLUMNS[tab].map(([field,label]) => el("th", {"aria-sort":key === field ? S.crmAsc ? "ascending" : "descending" : "none"},
      el("button", {class:"sort-button", onclick:() => {S.crmAsc=key === field ? !S.crmAsc : true; S.crmSort=field; S.crmPage=0; renderCrmTable();}}, label, key === field ? S.crmAsc ? " ↑" : " ↓" : " ↕"))))),
    el("tbody", {}, ...rows.map(row => el("tr", {tabindex:"0", onclick:() => tab === "customers" ? openCustomer(row) : select(row.session_id), onkeydown:e => {if (["Enter"," "].includes(e.key)) {e.preventDefault(); tab === "customers" ? openCustomer(row) : select(row.session_id);}}},
      ...CRM_COLUMNS[tab].map(([field]) => el("td", {class:["requests","quoted","won","lost","open","total_minor","price_minor","quote_minor"].includes(field) ? "num" : ""}, cell(row,field))))))) : emptyState("search", "Keine Einträge für diese Filter.");
  $("#crm-table").replaceChildren(el("div", {class:"table-scroll", tabindex:"0", role:"region", "aria-label":"CRM Tabelle"}, table),
    el("div", {class:"table-footer"}, el("span", {class:"caption"}, all.length ? `${S.crmPage*pageSize+1} bis ${Math.min((S.crmPage+1)*pageSize,all.length)} von ${all.length} Einträgen` : "0 Einträge"),
      el("div", {class:"actions"}, el("button", {class:"btn", disabled:S.crmPage === 0, onclick:() => {S.crmPage--; renderCrmTable();}}, "Zurück"),
        el("button", {class:"btn", disabled:S.crmPage === max, onclick:() => {S.crmPage++; renderCrmTable();}}, "Weiter"))),
    el("p", {class:"caption"}, `Die Liste lädt bis zu ${S.crm.limit || 200} der neuesten Datensätze je Bereich. Geschäftskennzahlen in der Übersicht berücksichtigen alle Anfragen.`));
}
