"use strict";


function renderBreadcrumbs() {
  const node = $("#breadcrumbs"); if (!node) return;
  node.hidden = S.page !== "session";
  const crumbs = [el("button", {class:"linklike", onclick:() => show("overview")}, "Dashboard")];
  const separator = () => el("span", {"aria-hidden":"true"}, "/");
  if (S.page === "session") {
    if (["requests", "crm", "rates", "errors"].includes(S.returnPage)) crumbs.push(separator(), el("button", {class:"linklike", onclick:() => show(S.returnPage)}, PAGES[S.returnPage][0]));
    const detail = S.detail?.id === S.selected ? S.detail : null;
    crumbs.push(separator(), el("span", {"aria-current":"page", class:"crumb-current", title:detail?.subject || ""}, "Request #" + S.selected + (detail?.subject ? " · " + detail.subject : "")));
  } else if (S.page !== "overview") crumbs.push(separator(), el("span", {"aria-current":"page"}, PAGES[S.page][0]));
  node.replaceChildren(...crumbs);
}

async function copyPlainText(text, button) {
  try {
    if (navigator.clipboard?.writeText) await navigator.clipboard.writeText(text);
    else throw new Error("Clipboard not available");
  } catch {
    const field = el("textarea", {style:"position:fixed;opacity:0", "aria-label":"Text to copy"});
    field.value = text; document.body.append(field); field.select();
    try { if (!document.execCommand("copy")) throw new Error("The browser did not allow copying."); }
    finally { field.remove(); button?.focus(); }
  }
  if (button) { button.title = "Copied"; button.setAttribute("aria-label", "Text copied"); }
}
async function copyMail(body, button) {
  try { await copyPlainText(body, button); button.textContent = "Copied"; }
  catch (e) { bufferMessage("Copy failed. " + e.message); }
}
function mailKind(mail, session) {
  const types = {phishing:"Quarantine", rfq:mail.in_reply_to ? "Customer reply" : "Freight request", tracking:"Tracking request", tracking_notice:"Tracking notice", tracking_answer:"Shipment status", bid:"Carrier offer", carrier_bid:"Carrier offer", bid_request:"Rate request", tariff:"Tariff update", booking:"Booking", booking_request:"Booking order", cancellation:"Cancellation", price_change:"Price change", notice:"Notice", followup:"Follow-up", clarification:"Clarification", quote:"Quote", booking_confirmation:"Booking confirmation"};
  return types[mail.category] || (mail.direction === "out" ? "Sent reply" : isCustomerMail(mail, session) ? "Customer reply" : "Partner message");
}
function conversationMail(mail, session) {
  return el("details", {class:"mail " + mail.direction, "data-mail-id":mail.id, "data-fold":"mail-" + mail.id},
    el("summary", {class:"mail-summary"},
      el("span", {class:"badge-s " + (mail.direction === "out" ? "blue" : "indigo")}, mailKind(mail, session)),
      el("span", {class:"mail-summary-text"}, el("strong", {}, mail.subject || "No subject"),
        el("span", {class:"caption"}, (mail.direction === "in" ? "From " + (mail.sender || "unknown") : "To " + (mail.recipient || "unknown")))),
      el("time", {class:"caption", datetime:mail.received_at}, dateTime(mail.received_at))),
    el("div", {class:"mail-tools"}, el("button", {class:"btn ghost", onclick:e => copyMail(mail.body || "", e.currentTarget)}, icon("inbox"), "Copy mail text")),
    el("div", {class:"mail-body"}, mail.body || "No message text."));
}

const CRM_COLUMNS = {
  customers:[["customer","Customer"],["requests","Requests"],["quoted","Quotes"],["won","Booked"],["lost","Lost"],["open","Open"],["last_contact","Last contact"]],
  quotes:[["sent_at","Date"],["customer","Customer"],["route","Route"],["airline","Airline"],["total_minor","Quote value"],["outcome","Outcome"]],
  bookings:[["booking_reference","Reference"],["customer","Customer"],["airline","Airline"],["price_minor","Airline cost"],["status","Status"],["created_at","Date"]],
  pricing:[["created_at","Date"],["customer","Customer"],["route","Route"],["airline","Airline"],["quote_minor","Quote value"],["options","Options compared"],["verdict","Outcome"]],
};
const CRM_TITLES = {customers:["Customers","Every customer with requests, quotes, bookings and last contact."],quotes:["Quotes","First quote per request with its outcome."],bookings:["Bookings","Airline bookings with reference, cost and status."],pricing:["Price comparison","The options compared for each quote and the 5U verdict."]};
const CRM_STATUS = {won:"Booked",lost:"Lost",open:"Open",booked:"Booked",confirmed:"Confirmed",failed:"Failed",active:"Active customers",new:"No booking",accepted:"Accepted",pending:"Not rated yet"};
function crmValue(row, key) {
  if (key === "route") return [row.origin,row.destination].filter(Boolean).join(" → ");
  if (key === "options") return row.options?.length || 0;
  if (key === "booking_reference") return row.booking_reference || row.team_reference || "";
  if (key === "airline") return row.airline || row.selected?.airline || "";
  return row[key] ?? "";
}
function crmRows() {
  const q = (S.crmQ || "").toLocaleLowerCase("en"), tab = S.crmTab;
  const since = S.crmDays ? Date.now() - S.crmDays * 86400000 : 0;
  const rows = (S.crm?.[tab] || []).filter(row => {
    const date = row.sent_at || row.last_contact || row.created_at;
    const status = tab === "customers" ? row.open > 0 ? "active" : row.won > 0 ? "won" : "new" : row.outcome || row.status || row.verdict || "pending";
    const statusMatches = !S.crmStatus || (tab === "customers" ? S.crmStatus === "active" ? row.open > 0 : S.crmStatus === "won" ? row.won > 0 : row.won === 0 : S.crmStatus === status);
    return (!q || [row.customer,row.subject,row.airline,row.selected?.airline,row.booking_reference,row.team_reference,row.origin,row.destination].some(v => String(v || "").toLocaleLowerCase("en").includes(q))) &&
      (!since || new Date(date).getTime() >= since) && statusMatches;
  });
  const key = S.crmSort || (tab === "customers" ? "last_contact" : tab === "quotes" ? "sent_at" : "created_at");
  const direction = S.crmAsc ? 1 : -1;
  return rows.sort((a,b) => {
    const av = crmValue(a,key), bv = crmValue(b,key);
    if (["total_minor","price_minor","quote_minor"].includes(key) && a.currency !== b.currency)
      return String(a.currency || "EUR").localeCompare(String(b.currency || "EUR"));
    return direction * (typeof av === "number" && typeof bv === "number" ? av-bv : String(av).localeCompare(String(bv), "en", {numeric:true}));
  });
}
function crmSelect(label, values, current, change) {
  return el("label", {class:"filter-control"}, el("span", {class:"caption"}, label),
    el("select", {"aria-label":label, onchange:change}, ...values.map(([v,l]) => el("option", {value:v, selected:String(v) === String(current)}, l))));
}
function renderCrm() {
  if (!S.crm) return;
  const statuses = S.crmTab === "customers" ? ["active","won","new"] : S.crmTab === "quotes" ? ["open","won","lost"] : S.crmTab === "bookings" ? ["booked","confirmed","failed"] : ["accepted","pending"];
  const filters = el("div", {class:"crm-filters"},
    el("div", {class:"crm-view-narrow"}, crmSelect("View", Object.entries(CRM_TITLES).map(([key,[label]]) => [key,label]), S.crmTab, e => openCrmTab(e.target.value))),
    el("label", {class:"search"}, icon("search"), el("input", {"aria-label":"Search CRM", placeholder:"Customer, route or reference", value:S.crmQ || "", oninput:e => {S.crmQ=e.target.value; S.crmPage=0; renderCrmTable();}})),
    crmSelect("Status", [["","All statuses"],...statuses.map(k => [k,CRM_STATUS[k]])], S.crmStatus || "", e => {S.crmStatus=e.target.value; S.crmPage=0; renderCrmTable();}),
    crmSelect("Period", [[0,"All time"],[1,"Last 24 hours"],[7,"Last 7 days"],[30,"Last 30 days"]], S.crmDays || 0, e => {S.crmDays=Number(e.target.value); S.crmPage=0; renderCrmTable();}),
    el("button", {class:"btn ghost", onclick:() => {S.crmQ=""; S.crmStatus=""; S.crmDays=0; S.crmSort=""; S.crmAsc=false; S.crmPage=0; renderCrm();}}, "Reset filters"),
    el("button", {class:"btn", onclick:e => copyCrmRows(e.currentTarget)}, icon("inbox"), "Copy list"));
  $("#crm").replaceChildren(el("section", {class:"card s12"},
    el("div", {class:"card-h"}, el("div", {}, el("h3", {}, CRM_TITLES[S.crmTab][0]), el("p", {class:"caption"}, CRM_TITLES[S.crmTab][1] + " Column headers sort the list."))),
    filters, el("div", {id:"crm-table"})));
  renderCrmTable();
}
async function copyCrmRows(button) {
  const cols = CRM_COLUMNS[S.crmTab], safe = value => {
    const text = String(value ?? "").replace(/[\t\r\n]/g," ");
    return /^[=+@-]/.test(text) ? "'" + text : text;
  };
  const value = (row,key) => {
    const raw = crmValue(row,key);
    if (["total_minor","price_minor","quote_minor"].includes(key)) return money(raw === "" ? null : raw,row.currency || "EUR");
    if (["status","outcome","verdict"].includes(key)) return CRM_STATUS[raw] || raw || "Pending";
    return raw;
  };
  const text = [cols.map(([,label]) => label), ...crmRows().map(row => cols.map(([key]) => value(row,key)))].map(row => row.map(safe).join("\t")).join("\n");
  try { await copyPlainText(text, button); }
  catch(e) { bufferMessage("CRM could not be copied. " + e.message); }
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
    if (["status","outcome","verdict"].includes(field)) return CRM_STATUS[value] || value || "Pending";
    return String(value);
  };
  const table = rows.length ? el("table", {class:"list"},
    el("thead", {}, el("tr", {}, ...CRM_COLUMNS[tab].map(([field,label]) => el("th", {"aria-sort":key === field ? S.crmAsc ? "ascending" : "descending" : "none"},
      el("button", {class:"sort-button", onclick:() => {S.crmAsc=key === field ? !S.crmAsc : true; S.crmSort=field; S.crmPage=0; renderCrmTable();}}, label, key === field ? S.crmAsc ? " ↑" : " ↓" : " ↕"))))),
    el("tbody", {}, ...rows.map(row => el("tr", {tabindex:"0", onclick:() => tab === "customers" ? openCustomer(row) : select(row.session_id), onkeydown:e => {if (["Enter"," "].includes(e.key)) {e.preventDefault(); tab === "customers" ? openCustomer(row) : select(row.session_id);}}},
      ...CRM_COLUMNS[tab].map(([field]) => el("td", {class:["requests","quoted","won","lost","open","total_minor","price_minor","quote_minor"].includes(field) ? "num" : ""}, cell(row,field))))))) : emptyState("search", "No entries for these filters.");
  $("#crm-table").replaceChildren(el("div", {class:"table-scroll", tabindex:"0", role:"region", "aria-label":"CRM table"}, table),
    el("div", {class:"table-footer"}, el("span", {class:"caption"}, all.length ? `${S.crmPage*pageSize+1} to ${Math.min((S.crmPage+1)*pageSize,all.length)} of ${all.length} entries` : "0 entries"),
      el("div", {class:"actions"}, el("button", {class:"btn", disabled:S.crmPage === 0, onclick:() => {S.crmPage--; renderCrmTable();}}, "Previous"),
        el("button", {class:"btn", disabled:S.crmPage === max, onclick:() => {S.crmPage++; renderCrmTable();}}, "Next"))),
    el("p", {class:"caption"}, `The list loads up to ${S.crm.limit || 200} of the newest records per tab. Dashboard figures include all requests.`));
}
