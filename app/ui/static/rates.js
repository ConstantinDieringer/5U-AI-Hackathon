"use strict";
// Rates tab: the airline tariff sheet pricing uses, and which airlines the portal offers per lane.
let RATES = null;
const rateMoney = (amount, currency) => (currency === "EUR" ? "€" : currency + " ") + Number(amount).toLocaleString("en-GB", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const kgLabel = kg => Number(kg) === 0 ? "Base" : "+" + Number(kg).toLocaleString("en-GB") + " kg";
async function loadRates() {
  if (isSelectOpen()) return;
  if (!RATES) {
    $("#rates").replaceChildren(emptyState("clock", "Loading rates …"));
    RATES = await api("/api/rates").catch(() => null);
    if (S.page !== "rates") return;
    if (!RATES) { $("#rates").replaceChildren(emptyState("alert", "Rates could not be loaded."), el("button", { class: "btn", onclick: loadRates }, "Try again")); return; }
  }
  renderRates();
}
function rateRows() {
  const tab = S.ratesTab || "tariff", q = (S.ratesQ || "").toLocaleLowerCase("en"), airline = S.ratesAirline || "";
  if (tab === "tariff") return RATES.tariff.lanes.filter(l => (!airline || l.airline === airline) &&
    (!q || [l.airline, l.origin, l.destination, l.origin + " " + l.destination].some(v => v.toLocaleLowerCase("en").includes(q))));
  return RATES.portal.lanes.filter(l => (!airline || l.airlines.some(a => a.name === airline)) &&
    (!q || [l.origin, l.destination, ...l.airlines.flatMap(a => [a.code, a.name])].some(v => v.toLocaleLowerCase("en").includes(q))));
}
function renderRates() {
  const tab = S.ratesTab || "tariff";
  const airlines = [...new Set(tab === "tariff" ? RATES.tariff.lanes.map(l => l.airline) : RATES.portal.lanes.flatMap(l => l.airlines.map(a => a.name)))].sort();
  if (S.ratesAirline && !airlines.includes(S.ratesAirline)) S.ratesAirline = "";
  const tabs = el("div", { class: "seg", role: "tablist", "aria-label": "Rate sources" }, ...[["tariff", `Tariff sheet ${RATES.tariff.lanes.length}`], ["portal", `Portal lanes ${RATES.portal.lanes.length}`]].map(([key, label]) =>
    el("button", { role: "tab", "aria-selected": tab === key, class: tab === key ? "active" : "", onclick: () => { S.ratesTab = key; S.ratesAirline = ""; renderRates(); } }, label)));
  const filters = el("div", { class: "crm-filters" },
    el("label", { class: "search" }, icon("search"), el("input", { "aria-label": "Search rates", placeholder: "Airline, origin or destination", value: S.ratesQ || "", oninput: e => { S.ratesQ = e.target.value; renderRateTable(); } })),
    crmSelect("Airline", [["", "All airlines"], ...airlines.map(a => [a, a])], S.ratesAirline || "", e => { S.ratesAirline = e.target.value; renderRateTable(); }),
    el("button", { class: "btn ghost", onclick: () => { S.ratesQ = ""; S.ratesAirline = ""; renderRates(); } }, "Reset filters"));
  const t = RATES.tariff.lanes[0];
  const note = tab === "tariff"
    ? (t ? `Rates per kg in ${t.currency}, valid ${dateOnly(t.valid_from)} to ${dateOnly(t.valid_to)}. Fuel and security are charged per chargeable kg on top. ${RATES.tariff.source === "runtime" ? "Latest tariff mail applied." : "Seed tariff from the start kit."}` : "No tariff sheet loaded.")
    : `Airlines the 5U portal offers per lane, with their weight brackets. Prices come live from the portal per request.${RATES.portal.synced_at ? " Synced " + dateTime(RATES.portal.synced_at) + "." : ""}`;
  $("#rates").replaceChildren(el("section", { class: "card s12" },
    el("div", { class: "card-h" }, el("div", {}, el("h2", {}, "Airline rates"), el("p", { class: "caption" }, note)),
      el("button", { class: "btn aside", onclick: () => { RATES = null; loadRates(); } }, icon("undo"), "Refresh")),
    tabs, filters, el("div", { id: "rate-table" })));
  renderRateTable();
}
const dateOnly = iso => new Date(iso).toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" });
function renderRateTable() {
  const tab = S.ratesTab || "tariff", rows = rateRows();
  let table;
  if (tab === "tariff") {
    const breaks = [...new Set(rows.flatMap(l => l.breaks.map(b => Number(b.from_kg))))].sort((a, b) => a - b);
    const cheapest = new Map();
    for (const l of rows) { const key = l.origin + "-" + l.destination, base = Number(l.breaks[0]?.rate ?? Infinity); if (!cheapest.has(key) || base < cheapest.get(key)) cheapest.set(key, base); }
    table = el("table", { class: "list rate-table" },
      el("thead", {}, el("tr", {}, el("th", {}, "Airline"), el("th", {}, "Lane"), el("th", { class: "r" }, "Transit"), el("th", { class: "r" }, "Minimum"),
        ...breaks.map(kg => el("th", { class: "r" }, kgLabel(kg))), el("th", { class: "r" }, "Fuel / kg"), el("th", { class: "r" }, "Security / kg"))),
      el("tbody", {}, ...rows.map(l => {
        const byKg = new Map(l.breaks.map(b => [Number(b.from_kg), b.rate])), best = Number(l.breaks[0]?.rate) === cheapest.get(l.origin + "-" + l.destination);
        return el("tr", { class: "static" }, el("td", {}, el("strong", {}, l.airline), best ? el("span", { class: "badge-s green rate-best", title: "Lowest base rate on this lane" }, "Best") : null),
          el("td", {}, l.origin + " → " + l.destination), el("td", { class: "r num" }, l.transit_days + " d"), el("td", { class: "r num" }, rateMoney(l.minimum, l.currency)),
          ...breaks.map(kg => el("td", { class: "r num" }, byKg.has(kg) ? rateMoney(byKg.get(kg), l.currency) : "–")),
          el("td", { class: "r num" }, rateMoney(l.fuel, l.currency)), el("td", { class: "r num" }, rateMoney(l.security, l.currency)));
      })));
  } else {
    table = el("table", { class: "list rate-table" },
      el("thead", {}, el("tr", {}, el("th", {}, "Lane"), el("th", {}, "Airlines"), el("th", {}, "Weight brackets"))),
      el("tbody", {}, ...rows.map(l => el("tr", { class: "static" }, el("td", {}, el("strong", {}, l.origin + " → " + l.destination)),
        el("td", { class: "wrap" }, el("div", { class: "rate-airlines" }, ...l.airlines.map(a => el("span", { class: "badge-s blue", title: a.code }, a.name)))),
        el("td", { class: "wrap caption" }, l.airlines.map(a => a.code + " " + a.brackets_kg.map(kg => kg ? "+" + kg : "base").join(" · ")).join("   "))))));
  }
  $("#rate-table").replaceChildren(rows.length ? el("div", { class: "table-scroll", role: "region", "aria-label": "Rate table", tabindex: "0" }, table) : emptyState("search", "No rates for these filters."),
    el("div", { class: "table-footer" }, el("span", { class: "caption" }, rows.length + (rows.length === 1 ? " lane" : " lanes"))));
}
