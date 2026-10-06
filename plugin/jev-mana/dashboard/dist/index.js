/**
 * Jev — Hermes dashboard plugin page. Read-only: GET /api/plugins/jev-mana/summary only.
 * Plain IIFE, no build step. React and the UI primitives come from the host SDK; fetchJSON is the
 * host's authenticated fetch, so this page adds no credential handling of its own. It never calls
 * Jev and never starts a health check. Every value rendered is a number, a boolean, or a fixed
 * enum word the backend already checked; React escapes the text either way.
 */
(function () {
  "use strict";
  const SDK = window.__HERMES_PLUGIN_SDK__;
  if (!SDK) return;
  const { React } = SDK;
  const h = React.createElement;
  const { Card, CardHeader, CardTitle, CardContent, Badge } = SDK.components;
  const { useState, useEffect, useCallback } = SDK.hooks;
  const BASE = "/api/plugins/jev-mana";

  const num = (v, d) => (v === null || v === undefined ? "unknown" : Number(v).toFixed(d || 0));
  const pct = (v) => (v === null || v === undefined ? "n/a" : (v * 100).toFixed(1) + "%");

  function Tile(props) {
    return h("div", { className: "jev-tile" },
      h("div", { className: "l" }, props.label), h("div", { className: "v" }, String(props.value)),
      props.sub ? h("div", { className: "s" }, props.sub) : null);
  }

  function Bars(props) {
    const max = Math.max.apply(null, props.values.concat([1]));
    return h("div", null,
      h("div", { className: "jev-bars" + (props.err ? " err" : "") },
        props.values.map((v, i) => h("i", { key: i, title: String(v), style: { height: Math.round(100 * v / max) + "%" } }))),
      h("div", { className: "jev-muted" }, props.note));
  }

  function Section(props) {
    return h(Card, null, h(CardHeader, null, h(CardTitle, null, props.title)),
      h(CardContent, null, props.children));
  }

  function Table(props) {
    return h("div", { className: "jev-scroll" }, h("table", null,
      h("thead", null, h("tr", null, props.head.map((c) => h("th", { key: c }, c)))),
      h("tbody", null, props.rows.map((r, i) => h("tr", { key: i }, r.map((c, j) => h("td", { key: j }, c)))))));
  }

  function healthFailed(k, hl) {
    const by = k.health_failed_by_point || {};
    const points = Object.keys(by);
    const n = points.reduce(function (a, p) { return a + by[p]; }, 0);
    const codes = hl && hl.reason_codes ? hl.reason_codes : {};
    const why = Object.keys(codes).map(function (p) { return p + ": " + codes[p].join(", "); });
    if (!n && !why.length) return null;
    return h("div", { className: "jev-banner bad" },
      "health_failed: " + n + " decision" + (n === 1 ? "" : "s") + " fell back to original" +
      (points.length ? " (" + points.map(function (p) { return p + " " + by[p]; }).join(", ") + ")" : "") +
      (why.length ? " \u00b7 why: " + why.join("; ") : ""));
  }

  function healthBanner(hl) {
    if (!hl || hl.status === "unavailable") {
      return h("div", { className: "jev-banner warn" }, "Health: no report yet. A health producer has not written one on this machine.");
    }
    const bad = hl.status === "unhealthy";
    const cls = bad ? "bad" : hl.status === "healthy" || hl.status === "off" ? "ok" : "warn";
    const text = "Health: " + hl.status + (hl.stale ? " (report is stale)" : "") +
      (hl.unhealthy_points.length ? " · falling back to original: " + hl.unhealthy_points.join(", ") : "");
    return h("div", { className: "jev-banner " + cls }, text);
  }

  function JevPage() {
    const [data, setData] = useState(null);
    const [err, setErr] = useState("");
    const [profile, setProfile] = useState("");
    const [point, setPoint] = useState("");
    const [windowSel, setWindow] = useState("24h");
    const load = useCallback(function () {
      const q = new URLSearchParams({ window: windowSel });
      if (profile) q.set("profile", profile);
      if (point) q.set("point", point);
      SDK.fetchJSON(BASE + "/summary?" + q.toString())
        .then(function (r) { setData(r); setErr(""); })
        .catch(function (e) { setErr(String((e && e.message) || e)); });
    }, [profile, point, windowSel]);
    useEffect(function () { load(); const t = setInterval(load, 15000); return function () { clearInterval(t); }; }, [load]);

    if (err) return h("div", { className: "jev-page" }, h("div", { className: "jev-banner bad" }, "Jev page unavailable: " + err));
    if (!data) return h("div", { className: "jev-page jev-muted" }, "Loading…");
    const k = data.kpis, tk = k.tokens;
    const options = function (items, value, set, label) {
      return h("select", { value: value, "aria-label": label, onChange: function (e) { set(e.target.value); } },
        [h("option", { key: "", value: "" }, label)].concat(items.map((n) => h("option", { key: n, value: n }, n))));
    };
    return h("div", { className: "jev-page" },
      h("div", { className: "jev-filters" },
        options(Object.keys(data.profiles), profile, setProfile, "All profiles"),
        options(data.points, point, setPoint, "All points"),
        h("select", { value: windowSel, "aria-label": "Time", onChange: function (e) { setWindow(e.target.value); } },
          [["1h", "Last hour"], ["24h", "Last 24 hours"], ["7d", "Last 7 days"], ["all", "All time"]]
            .map((o) => h("option", { key: o[0], value: o[0] }, o[1])))),
      healthBanner(data.health),
      healthFailed(k, data.health),
      data.warnings.map((w, i) => h("div", { key: i, className: "jev-banner warn" }, w)),
      h("div", { className: "jev-banner " + (data.state === "off" ? "warn" : "ok") },
        "Decisions: " + data.state.toUpperCase() + (data.state === "off" ? " — Jev is not being called" : "")),
      h("div", { className: "jev-tiles" },
        h(Tile, { label: "Current Jev calls", value: k.calls_current, sub: data.state === "off" ? "off: none are made" : "network evaluations" }),
        h(Tile, { label: "Historical calls", value: k.calls_historical, sub: "from profiles now off" }),
        h(Tile, { label: "Decisions", value: k.decisions, sub: k.skipped_not_calls + " skipped or excluded (not calls)" }),
        h(Tile, { label: "Applied", value: pct(k.rates.applied), sub: k.outcomes.applied + " of " + k.decisions }),
        h(Tile, { label: "Original (fallback)", value: pct(k.rates.fallback), sub: k.outcomes.fallback + " of " + k.decisions }),
        h(Tile, { label: "Latency p50 / p95", value: num(k.latency_ms.p50) + " / " + num(k.latency_ms.p95) + " ms", sub: k.latency_ms.n + " calls timed" }),
        h(Tile, { label: "Jev tokens", value: tk.total === null ? "unknown" : tk.total, sub: tk.recorded_calls + " recorded, " + tk.unrecorded_calls + " not" +
          (tk.incomplete_batches ? " \u00b7 " + tk.incomplete_batches + " incomplete batch(es)" : "") +
          (tk.attribution.batch_unknown ? " \u00b7 " + tk.attribution.batch_unknown + " batch unknown" : "") }),
        h(Tile, { label: "Avoided LLM calls (est.)", value: k.avoided_llm_calls.estimate, sub: k.avoided_llm_calls.unknown_original + " applied, original unknown" })),
      h(Section, { title: "Fell back to original, by reason" },
        h(Table, { head: ["Reason", "Count", "Rate"],
          rows: Object.keys(k.fallback_by_reason).map((n) => [n, k.fallback_by_reason[n].count, pct(k.fallback_by_reason[n].rate)]) })),
      h(Section, { title: "Create vs maintain (receipt activity_class)" },
        h(Table, { head: ["Class", "Decisions", "Calls", "Applied", "Original"],
          rows: ["create", "maintain", "other"].map(function (n) { const a = k.activity[n];
            return [n, a.decisions, a.calls, a.applied, a.fallback]; }) }),
        h("div", { className: "jev-muted" }, k.activity.work_executor_without_class + " work.executor decision(s) carry no activity_class")),
      h("div", { className: "jev-cols" },
        h(Section, { title: "Confidence" }, h(Bars, { values: k.confidence.bins, note: "tenths 0 to 1; n=" + k.confidence.n + ", " + k.confidence.missing + " without" })),
        h(Section, { title: "Margin" }, h(Bars, { values: k.margin.bins, note: "tenths 0 to 1; n=" + k.margin.n + ", " + k.margin.missing + " without" })),
        h(Section, { title: "Errors over time" }, h(Bars, { err: true, values: k.timeline.map((b) => b.error + b.timeout + b.invalid),
          note: k.timeline.length ? "error + timeout + invalid per " + (k.bucket_seconds / 60) + " min" : "no calls in this window" }))),
      h(Section, { title: "Per profile" },
        h(Table, { head: ["Profile", "Master", "Margin", "Timeout s", "Turn s", "Receipts", "Excluded"],
          rows: Object.keys(data.profiles).map((n) => { const p = data.profiles[n], c = p.config;
            return [n, p.master, c.margin, c.timeout_seconds, c.turn_budget_seconds, c.receipts ? "on" : "off", c.exclude.join(", ") || "none"]; }) })),
      h(Section, { title: "Patch status" },
        h("div", null, data.patch && data.patch.state === "not_applicable" ? "not applicable" : (data.patch ? data.patch.state : "not applicable"))),
      h(Section, { title: "Recent decisions" },
        h(Table, { head: ["Time", "Id", "Profile", "Point", "Class", "Call", "Outcome", "Chosen", "Reason", "Conf", "Margin", "Score", "ms"],
          rows: data.recent.map((e) => [e.ts ? new Date(e.ts * 1000).toLocaleString() : "", e.id || "", e.profile || "", e.point, e.activity_class || "", e.call ? "yes" : "no",
            e.outcome, e.chosen === null ? "" : String(e.chosen),
            e.reason || "", e.confidence === null ? "" : num(e.confidence, 2), e.margin === null ? "" : num(e.margin, 2),
            e.score === null ? "" : num(e.score, 2), e.latency_ms === null ? "" : num(e.latency_ms)]) })),
      h("div", { className: "jev-muted" }, "Read-only local summaries: " + data.sources.rows + " decision rows read, " + data.sources.ignored_rows +
        " other rows ignored. Numbers and fixed words only; goals, state and arguments are never shown, and an id appears only if it is a valid UUID."));
  }

  if (window.__HERMES_PLUGINS__ && typeof window.__HERMES_PLUGINS__.register === "function") {
    window.__HERMES_PLUGINS__.register("jev-mana", JevPage);
  }
})();
