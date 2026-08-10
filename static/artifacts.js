/* Artifact panels: fetch spec by id/version, render table/markdown (charts M2,
   maps M4), version chips with restore, quick filter + sort + paging
   (client-side, cosmetic — server-side transforms come from the agent), CSV +
   XLSX export. Loaded BEFORE app.js. */
"use strict";

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

const Artifacts = (() => {
  const PAGE_SIZE = 100;
  const panels = {};   // artifact_id -> {card, spec, pinned, page, filter, sort}

  const httpUrl = (v) => (v && /^https?:\/\//i.test(String(v)) ? String(v) : null);

  const FMT = {
    money: (v) => v == null || v === "" ? "" : "$" + Math.round(Number(v)).toLocaleString(),
    int: (v) => v == null || v === "" ? "" : Number(v).toLocaleString(),
    pct: (v) => v == null || v === "" ? "" : (Number(v) * 100).toFixed(0) + "%",
    date: (v) => (v ? String(v).slice(0, 10) : ""),
    link: (v) => (httpUrl(v) ? `<a href="${esc(v)}" target="_blank" rel="noopener">open</a>` : esc(v || "")),
    score: (v) => v == null || v === "" ? "" : Number(v).toFixed(1),
  };

  function fmtCell(col, v, row, keyIdx) {
    let out;
    if (col.format && FMT[col.format]) out = FMT[col.format](v);
    else if (typeof v === "number" && !Number.isInteger(v)) out = v.toFixed(2);
    else out = esc(v);
    // a column may point at a (usually hidden) sibling column holding a URL:
    // the value renders as a link; a blank value still gets a "verify" link
    if (col.link_col && row && keyIdx && keyIdx[col.link_col] != null) {
      const url = httpUrl(row[keyIdx[col.link_col]]);
      if (url) {
        const a = (t) => `<a href="${esc(url)}" target="_blank" rel="noopener">${t}</a>`;
        out = out ? a(out) : a(`<span class="cell-verify">verify&nbsp;&#8599;</span>`);
      }
    }
    return out;
  }

  function isNum(col) {
    return col.type === "number" || ["money", "int", "score", "pct"].includes(col.format);
  }

  function reset() {
    document.querySelector("#panels").innerHTML = "";
    for (const k of Object.keys(panels)) delete panels[k];
    for (const k of Object.keys(JOBS)) {
      if (JOBS[k].es) JOBS[k].es.close();
      delete JOBS[k];
    }
    updateEmpty();
  }

  function updateEmpty() {
    document.querySelector("#ws-empty").classList
      .toggle("hidden", Object.keys(panels).length > 0);
  }

  async function fetchSpec(aid, version) {
    const url = `/api/artifacts/${aid}` + (version ? `?version=${version}` : "");
    const res = await fetch(url, { credentials: "same-origin" });
    if (!res.ok) throw new Error(`artifact fetch failed (${res.status})`);
    return res.json();
  }

  async function onArtifactEvent(data) {
    try {
      const spec = await fetchSpec(data.artifact_id, data.version);
      upsertPanel(spec);
    } catch (e) {
      console.error("artifact render failed", data, e);
    }
  }

  function upsertPanel(spec) {
    let p = panels[spec.artifact_id];
    if (!p) {
      const card = document.createElement("div");
      card.className = "panel-card";
      card.id = `panel-${spec.artifact_id}`;
      p = panels[spec.artifact_id] = { card, pinned: false, page: 0, filter: "", sort: null,
                                       tblH: savedHeight("t", spec.artifact_id),
                                       mapH: savedHeight("m", spec.artifact_id),
                                       colW: savedWidths(spec.artifact_id) };
      const host = document.querySelector("#panels");
      const firstUnpinned = [...host.children].find((el) => !el.classList.contains("pinned"));
      host.insertBefore(card, firstUnpinned || null);
    }
    p.spec = spec;
    p.page = 0;
    renderPanel(p);
    updateEmpty();
  }

  // human words for ops in version-chip tooltips
  const OP_WORDS = {
    to_map: "turned into a map", to_chart: "turned into a chart",
    to_table: "turned into a table", set_styling: "color rules",
    filter: "filtered", sort: "sorted", groupby: "grouped",
    select: "columns picked", limit: "trimmed", rename: "renamed columns",
    revert: "older version restored", append_rows: "rows added",
    concat: "another table stacked on", join: "columns joined in",
  };

  async function applyChipTitles(p, s, latest) {
    try {
      if (!p.versions || p.versionsFor !== latest) {
        const res = await fetch(`/api/artifacts/${s.artifact_id}/versions`,
                                { credentials: "same-origin" });
        if (!res.ok) return;
        p.versions = (await res.json()).versions || [];
        p.versionsFor = latest;
      }
      const byV = {};
      p.versions.forEach((r) => (byV[r.version] = r));
      p.card.querySelectorAll(".vchip").forEach((el) => {
        const r = byV[Number(el.dataset.v)];
        if (!r) return;
        const bits = [];
        if (r.version === 1) {
          bits.push(`original ${s.created_by || "tool"} output`);
        } else {
          const prev = byV[r.version - 1];
          if (prev && prev.kind !== r.kind) bits.push(`${prev.kind} → ${r.kind}`);
          let ops = [];
          try {
            ops = (JSON.parse(r.ops_json || "[]") || [])
              .map((o) => OP_WORDS[o.op] || o.op).filter(Boolean);
          } catch (e) {}
          bits.push(ops.join(", ") || "edited");
        }
        el.title = `v${r.version}: ${bits.join(" · ")} — ` +
          `${(r.row_count ?? 0).toLocaleString()} rows. ` +
          `Click to view this version; every edit keeps the older ones.`;
      });
    } catch (e) {}
  }

  function renderPanel(p) {
    const s = p.spec;
    const latest = s.latest_version || s.version;
    const viewingOld = s.version < latest;
    let chips = "";
    if (latest > 1) {
      for (let v = 1; v <= latest; v++) {
        chips += `<span class="vchip ${v === s.version ? "active" : ""}" data-v="${v}">v${v}</span>`;
      }
    }
    p.card.classList.toggle("pinned", p.pinned);
    p.card.innerHTML = `
      <div class="panel-head">
        <span class="panel-title" title="${esc(s.title)}">${esc(s.title || s.artifact_id)}</span>
        <span class="vchips">${chips}</span>
        ${viewingOld ? `<button class="icon restore" title="Restore this version">restore</button>` : ""}
        <button class="icon pin" title="Pin">${p.pinned ? "&#9733;" : "&#9734;"}</button>
        <button class="icon csv" title="Download CSV">CSV</button>
        <button class="icon xlsx" title="Download Excel">XLSX</button>
        <button class="icon collapse" title="Collapse">&#8722;</button>
      </div>
      <div class="panel-body"></div>
      <div class="panel-note">${s.row_count != null ? `${s.row_count.toLocaleString()} rows · ` : ""}v${s.version} · ${esc(s.created_by || "")}</div>`;

    p.card.querySelectorAll(".vchip").forEach((el) => {
      el.title = "version history — hover a chip for what changed, click to view";
      el.onclick = async () => upsertPanel(await fetchSpec(s.artifact_id, Number(el.dataset.v)));
    });
    if (latest > 1) applyChipTitles(p, s, latest);
    const restoreBtn = p.card.querySelector(".restore");
    if (restoreBtn) restoreBtn.onclick = async () => {
      const res = await fetch(`/api/artifacts/${s.artifact_id}/transform`, {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ops: [{ op: "revert", to_version: s.version }] }),
      });
      if (res.ok) {
        const art = await res.json();
        upsertPanel(await fetchSpec(s.artifact_id, art.version));
      }
    };
    p.card.querySelector(".pin").onclick = () => {
      p.pinned = !p.pinned;
      const host = document.querySelector("#panels");
      if (p.pinned) host.prepend(p.card);
      renderPanel(p);
    };
    p.card.querySelector(".csv").onclick = () => exportCsv(s);
    p.card.querySelector(".xlsx").onclick = () => {
      window.open(`/api/artifacts/${s.artifact_id}/export.xlsx`, "_blank");
    };
    p.card.querySelector(".collapse").onclick = () => {
      p.card.querySelector(".panel-body").classList.toggle("hidden");
    };

    renderBody(p);
  }

  function renderBody(p) {
    const mount = p.card.querySelector(".panel-body");
    const s = p.spec;
    if (s.kind === "markdown") return renderMarkdown(mount, s);
    if (s.kind === "chart") return renderChart(mount, s);
    if (s.kind === "map") return renderMap(p, mount, s);
    return renderTable(p, mount, s);
  }

  /* ---------- drag-to-resize (tables + maps), height kept per artifact --- */
  function watchResize(el, p, key, aid, tag, onChange) {
    if (!el || typeof ResizeObserver === "undefined") return;
    let last = null;
    const ro = new ResizeObserver(() => {
      const h = Math.round(el.offsetHeight);
      if (last === null) { last = h; return; }   // initial layout tick
      if (h > 0 && h !== last) {                 // height actually dragged
        last = h;
        p[key] = h;
        try { localStorage.setItem(`sa_h_${tag}_${aid}`, String(h)); } catch (e) {}
        if (onChange) onChange(h);
      }
    });
    ro.observe(el);
  }

  function savedHeight(tag, aid) {
    try {
      const v = Number(localStorage.getItem(`sa_h_${tag}_${aid}`));
      return v >= 90 && v <= 4000 ? v : null;
    } catch (e) { return null; }
  }

  function savedWidths(aid) {
    try {
      const o = JSON.parse(localStorage.getItem(`sa_w_${aid}`) || "null");
      return o && typeof o === "object" ? o : null;
    } catch (e) { return null; }
  }

  // default column width in px from header + first rows (~7.2px/char at
  // 12.5px font); the 340 cap is what makes long text WRAP instead of
  // stretching the column — drag the header edge to widen
  function defaultColWidth(c, rows) {
    let m = String(c.label || c.key).length;
    const n = Math.min(rows.length, 50);
    for (let i = 0; i < n; i++) {
      const v = rows[i][c.idx];
      if (v != null && v !== "") m = Math.max(m, String(v).length);
    }
    return Math.max(64, Math.min(Math.round(m * 7.2) + 22, 340));
  }

  /* ---------- table ---------- */
  function visibleCols(s) {
    const cols = [];
    s.columns.forEach((c, i) => { if (!c.hidden) cols.push({ ...c, idx: i }); });
    return cols;
  }

  const TIER_CLASSES = { hot: 1, warm: 1, now: 1, std: 1 };   // whitelist (goes into HTML)

  function ruleMatches(rule, cell) {
    // ANY present test passing matches the rule — mirror of xlsx.py._rule_matches
    const num = (x) => {
      const n = parseFloat(String(x).replace(/[$,]/g, ""));
      return Number.isNaN(n) ? null : n;
    };
    const s = String(cell ?? "").trim().toLowerCase();
    if ("eq" in rule) {
      const t = String(rule.eq ?? "").trim().toLowerCase();
      if (s === t) return true;
      const a = num(s), b = num(t);
      if (a !== null && b !== null && a === b) return true;
    }
    if ("contains" in rule) {
      const needle = String(rule.contains ?? "").toLowerCase();
      if (needle && s.includes(needle)) return true;
    }
    const v = num(s);
    if (v !== null) {
      const g = num(rule.gte), l = num(rule.lte);
      if (g !== null && v >= g) return true;
      if (l !== null && v <= l) return true;
    }
    return false;
  }

  function tierRule(s, row) {
    // index of the first matching valid rule, -1 if none (first hit wins)
    const st = s.styling;
    if (!st || !st.tier_rules) return -1;
    const keyIdx = {};
    s.columns.forEach((c, i) => (keyIdx[c.key] = i));
    for (let k = 0; k < st.tier_rules.length; k++) {
      const rule = st.tier_rules[k];
      const i = keyIdx[rule.column];
      if (i == null || !TIER_CLASSES[rule.class]) continue;
      if (ruleMatches(rule, row[i])) return k;
    }
    return -1;
  }

  function tierClass(s, row) {
    const k = tierRule(s, row);
    return k < 0 ? "" : `tier-${s.styling.tier_rules[k].class}`;
  }

  /* ---------- legend (maps + tables share it) ---------- */
  function ruleLabel(s, rule) {
    if (rule.label && String(rule.label).trim()) return String(rule.label).trim();
    const colDef = s.columns.find((c) => c.key === rule.column);
    const col = ((colDef && (colDef.label || colDef.key)) || rule.column || "")
      .replace(/_/g, " ");
    const fmtN = (v) => {
      const n = parseFloat(String(v).replace(/[$,]/g, ""));
      return Number.isNaN(n) ? String(v) : n.toLocaleString();
    };
    const parts = [];
    if ("gte" in rule) parts.push(`${col} ≥ ${fmtN(rule.gte)}`);
    if ("lte" in rule) parts.push(`${col} ≤ ${fmtN(rule.lte)}`);
    if ("eq" in rule) parts.push(`${col} = ${rule.eq}`);
    if ("contains" in rule) parts.push(`${col} has “${rule.contains}”`);
    return parts.join(" or ") || col;
  }

  function legendEntries(s) {
    // [{cls:"hot"|""|.., label, count}] from tier_rules over the FULL row set;
    // "" = rows no rule matched (default color). null when nothing is styled.
    const st = s.styling;
    if (!st || !st.tier_rules || !st.tier_rules.length) return null;
    const counts = new Array(st.tier_rules.length).fill(0);
    let other = 0;
    for (const r of s.rows) {
      const k = tierRule(s, r);
      if (k < 0) other++;
      else counts[k]++;
    }
    const out = [], seen = {};
    st.tier_rules.forEach((rule, k) => {
      if (!TIER_CLASSES[rule.class]) return;
      const label = ruleLabel(s, rule);
      const key = `${rule.class}|${label}`;
      if (key in seen) { out[seen[key]].count += counts[k]; return; }
      seen[key] = out.length;
      out.push({ cls: rule.class, label, count: counts[k] });
    });
    if (!out.length) return null;
    if (other) out.push({ cls: "", label: "other", count: other });
    return out;
  }

  function renderTable(p, mount, s) {
    const cols = visibleCols(s);
    let rows = s.rows;
    if (p.filter) {
      const f = p.filter.toLowerCase();
      rows = rows.filter((r) => cols.some((c) => String(r[c.idx] ?? "").toLowerCase().includes(f)));
    }
    if (p.sort) {
      const { idx, dir } = p.sort;
      const mul = dir === "desc" ? -1 : 1;
      rows = [...rows].sort((a, b) => {
        const av = a[idx], bv = b[idx];
        const an = parseFloat(String(av).replace(/[$,]/g, ""));
        const bn = parseFloat(String(bv).replace(/[$,]/g, ""));
        if (!Number.isNaN(an) && !Number.isNaN(bn)) return (an - bn) * mul;
        return String(av ?? "").localeCompare(String(bv ?? "")) * mul;
      });
    }
    const pages = Math.max(1, Math.ceil(rows.length / PAGE_SIZE));
    p.page = Math.min(p.page, pages - 1);
    const slice = rows.slice(p.page * PAGE_SIZE, (p.page + 1) * PAGE_SIZE);

    const widths = cols.map((c) =>
      (p.colW && p.colW[c.key]) || defaultColWidth(c, s.rows));
    const colgroup = `<colgroup>${widths.map((w) =>
      `<col style="width:${w}px">`).join("")}</colgroup>`;
    const thead = cols.map((c, j) => {
      const arrow = p.sort && p.sort.idx === c.idx ? (p.sort.dir === "desc" ? " ▾" : " ▴") : "";
      return `<th data-j="${j}">${esc(c.label || c.key)}${arrow}<span class="col-grip" data-j="${j}"></span></th>`;
    }).join("");
    const keyIdx = {};
    s.columns.forEach((c, i) => (keyIdx[c.key] = i));
    const body = slice.map((r) => {
      const cls = tierClass(s, r);
      const tds = cols.map((c) => {
        const num = isNum(c) ? ' class="num"' : "";
        return `<td${num}>${fmtCell(c, r[c.idx], r, keyIdx)}</td>`;
      }).join("");
      return `<tr${cls ? ` class="${cls}"` : ""}>${tds}</tr>`;
    }).join("");

    const entries = legendEntries(s);
    const legend = entries ? `<div class="tbl-legend">${entries.map((e) =>
      `<span class="lg-item"><span class="lg-swatch${e.cls ? ` tier-${e.cls}` : ""}"></span>${esc(e.label)} <span class="lg-n">(${e.count})</span></span>`
    ).join("")}</div>` : "";

    mount.innerHTML = `
      <div class="tbl-controls">
        <input type="search" placeholder="quick filter…" value="${esc(p.filter)}">
        <span class="tbl-pager">
          <button class="prev" ${p.page === 0 ? "disabled" : ""}>&larr;</button>
          ${p.page + 1}/${pages} (${rows.length.toLocaleString()})
          <button class="next" ${p.page >= pages - 1 ? "disabled" : ""}>&rarr;</button>
        </span>
      </div>
      <div class="tbl-scroll" style="${p.tblH ? `height:${p.tblH}px;` : "max-height:420px;"}">
        <table class="art" style="width:${widths.reduce((a, b) => a + b, 0)}px">${colgroup}<thead><tr>${thead}</tr></thead><tbody>${body}</tbody></table>
      </div>${legend}`;

    const sc = mount.querySelector(".tbl-scroll");
    if (p.tblH == null) {
      // freeze the natural height as an explicit one — CSS resize can't
      // drag past a max-height cap, an explicit height it can
      requestAnimationFrame(() => {
        if (sc.isConnected && p.tblH == null) {
          sc.style.height = sc.offsetHeight + "px";
          sc.style.maxHeight = "none";
        }
      });
    }
    watchResize(sc, p, "tblH", s.artifact_id, "t");

    const colEls = mount.querySelectorAll("colgroup col");
    const tableEl = mount.querySelector("table.art");
    mount.querySelectorAll(".col-grip").forEach((g) => {
      g.addEventListener("click", (e) => e.stopPropagation());
      g.onmousedown = (e) => {
        e.preventDefault();
        e.stopPropagation();
        const j = Number(g.dataset.j);
        const startX = e.pageX, startW = widths[j];
        g.classList.add("dragging");
        const move = (ev) => {
          widths[j] = Math.max(46, Math.min(900, startW + ev.pageX - startX));
          colEls[j].style.width = widths[j] + "px";
          tableEl.style.width = widths.reduce((a, b) => a + b, 0) + "px";
        };
        const up = () => {
          document.removeEventListener("mousemove", move);
          document.removeEventListener("mouseup", up);
          g.classList.remove("dragging");
          p.colW = p.colW || {};
          p.colW[cols[j].key] = widths[j];
          try { localStorage.setItem(`sa_w_${s.artifact_id}`, JSON.stringify(p.colW)); } catch (err) {}
        };
        document.addEventListener("mousemove", move);
        document.addEventListener("mouseup", up);
      };
    });

    const search = mount.querySelector("input[type=search]");
    let deb;
    search.oninput = () => {
      clearTimeout(deb);
      deb = setTimeout(() => { p.filter = search.value; p.page = 0; renderBody(p); }, 200);
    };
    mount.querySelector(".prev").onclick = () => { p.page--; renderBody(p); };
    mount.querySelector(".next").onclick = () => { p.page++; renderBody(p); };
    mount.querySelectorAll("th").forEach((th) => {
      th.onclick = () => {
        const c = cols[Number(th.dataset.j)];
        const dir = p.sort && p.sort.idx === c.idx && p.sort.dir === "desc" ? "asc" : "desc";
        p.sort = { idx: c.idx, dir };
        renderBody(p);
      };
    });
  }

  /* ---------- markdown (escape FIRST, then a small subset) ----------
     Also used for chat bubbles (app.js) — headings, ul/ol lists, bold,
     italic, inline code, links, hr. Never raw innerHTML of LLM text. */
  function mdToHtml(text) {
    const inline = (s) => s
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>")
      .replace(/(^|[^*])\*(\S(?:[^*\n]*\S)?)\*(?!\*)/g, "$1<i>$2</i>")
      .replace(/\[([^\]]+)\]\((\/[^)\s]+|https?:[^)\s]+)\)/g,
               '<a href="$2" target="_blank" rel="noopener">$1</a>');
    const lines = String(text).split("\n");
    let html = "", list = null;   // "ul" | "ol" | null
    const closeList = () => { if (list) { html += `</${list}>`; list = null; } };
    const isSepRow = (t) => /^\s*\|?[\s:|-]+\|?\s*$/.test(t || "") &&
                            (t || "").includes("-") && (t || "").includes("|");
    const splitCells = (t) => t.trim().replace(/^\|/, "").replace(/\|$/, "")
      .split("|").map((c) => inline(esc(c.trim())));
    for (let li_ = 0; li_ < lines.length; li_++) {
      const raw = lines[li_];
      // | pipe | table | with a |---|---| separator on the next line
      if (raw.includes("|") && isSepRow(lines[li_ + 1])) {
        closeList();
        let t = `<div class="md-tablewrap"><table class="mdt"><thead><tr>` +
                splitCells(raw).map((c) => `<th>${c}</th>`).join("") +
                `</tr></thead><tbody>`;
        li_ += 2;
        for (; li_ < lines.length && lines[li_].includes("|") &&
               lines[li_].trim(); li_++) {
          t += "<tr>" + splitCells(lines[li_]).map((c) => `<td>${c}</td>`).join("") + "</tr>";
        }
        li_--;
        html += t + "</tbody></table></div>";
        continue;
      }
      const line = esc(raw);
      let m;
      if ((m = line.match(/^(#{1,4}) +(.*)$/))) {
        closeList();
        const lvl = Math.min(Math.max(m[1].length + 1, 3), 5);  // ## -> h3
        html += `<h${lvl}>${inline(m[2])}</h${lvl}>`;
      } else if (/^\s*(-{3,}|\*{3,})\s*$/.test(line)) {
        closeList(); html += "<hr>";
      } else if ((m = line.match(/^\s*[-*] +(.*)$/))) {
        if (list !== "ul") { closeList(); html += "<ul>"; list = "ul"; }
        html += `<li>${inline(m[1])}</li>`;
      } else if ((m = line.match(/^\s*\d{1,3}[.)] +(.*)$/))) {
        if (list !== "ol") { closeList(); html += "<ol>"; list = "ol"; }
        html += `<li>${inline(m[1])}</li>`;
      } else {
        closeList();
        if (line.trim()) html += `<p>${inline(line)}</p>`;
      }
    }
    closeList();
    return html;
  }

  function renderMarkdown(mount, s) {
    const text = s.rows && s.rows[0] ? s.rows[0][0] : "";
    mount.innerHTML = `<div class="mdbody">${mdToHtml(text)}</div>`;
  }

  /* ---------- chart (Chart.js 4, vendored) ---------- */
  const CHARTS = {};   // artifact_id -> Chart instance (destroy before re-create)
  const PALETTE = ["#2f6fab", "#c00000", "#3a7d44", "#9c6500", "#6b4fa1",
                   "#1f4e79", "#b05097", "#4f8f8f", "#8a8a3d", "#595959"];
  // strong variants of the table tiers so chart colors can speak the same
  // language ("hot"/"warm"/"now"/"std") as tier_rules
  const TIER_STRONG = { hot: "#3a7d44", warm: "#e0a100", now: "#c00000", std: "#8a99ab" };

  function aggregate(s, c) {
    const keyIdx = {};
    s.columns.forEach((col, i) => (keyIdx[col.key] = i));
    const xi = keyIdx[c.x], yi = keyIdx[c.y], si = c.series ? keyIdx[c.series] : null;
    if (xi == null) return null;
    const agg = c.agg || (yi == null ? "count" : "sum");
    const groups = {};   // series -> x -> value
    for (const r of s.rows) {
      const x = String(r[xi] ?? "(blank)");
      const ser = si != null ? String(r[si] ?? "(blank)") : "_";
      groups[ser] = groups[ser] || {};
      const g = groups[ser];
      if (agg === "count") g[x] = (g[x] || 0) + 1;
      else {
        const v = parseFloat(String(r[yi]).replace(/[$,]/g, ""));
        if (!Number.isNaN(v)) {
          if (agg === "avg") {
            g[x] = g[x] || { s: 0, n: 0 };
            g[x].s += v; g[x].n += 1;
          } else g[x] = (g[x] || 0) + v;
        }
      }
    }
    if (agg === "avg") {
      for (const ser of Object.keys(groups))
        for (const x of Object.keys(groups[ser]))
          groups[ser][x] = groups[ser][x].s / groups[ser][x].n;
    }
    // label order: by total desc, top_n cap
    const totals = {};
    for (const ser of Object.keys(groups))
      for (const [x, v] of Object.entries(groups[ser]))
        totals[x] = (totals[x] || 0) + v;
    let labels = Object.keys(totals).sort((a, b) => totals[b] - totals[a]);
    if (c.top_n) labels = labels.slice(0, c.top_n);

    // agent-set colors: keys are series names (when series is set), else
    // x values (bars/slices). Values: #hex/css color or hot|warm|now|std.
    const cmap = {};
    Object.entries(c.colors || {}).forEach(([k, v]) => {
      cmap[String(k).trim().toLowerCase()] = TIER_STRONG[v] || String(v);
    });
    const hasColors = Object.keys(cmap).length > 0;
    const resolve = (name, fb) => cmap[String(name).trim().toLowerCase()] || fb;

    const datasets = Object.entries(groups).map(([ser, g], i) => {
      const fb = PALETTE[i % PALETTE.length];
      const base = ser === "_" ? fb : resolve(ser, fb);
      const perPoint = c.kind === "pie" ||
                       (hasColors && ser === "_" && c.kind !== "line");
      return {
        label: ser === "_" ? (c.y || "count") : ser,
        data: labels.map((x) => g[x] ?? 0),
        backgroundColor: perPoint
          ? labels.map((x, j) =>
              resolve(x, c.kind === "pie" ? PALETTE[j % PALETTE.length] : base))
          : base,
        borderColor: base,
      };
    });
    return { labels, datasets };
  }

  function renderChart(mount, s) {
    if (typeof Chart === "undefined") {
      mount.innerHTML = `<div class="ws-empty">Chart.js not loaded</div>`;
      return;
    }
    const c = s.chart || {};
    const data = aggregate(s, c);
    if (!data) {
      mount.innerHTML = `<div class="ws-empty">chart spec missing x column</div>`;
      return;
    }
    mount.innerHTML = `<div style="height:300px"><canvas></canvas></div>`;
    const canvas = mount.querySelector("canvas");
    if (CHARTS[s.artifact_id]) CHARTS[s.artifact_id].destroy();
    CHARTS[s.artifact_id] = new Chart(canvas, {
      type: c.kind === "pie" ? "pie" : c.kind === "line" ? "line" : "bar",
      data,
      options: {
        responsive: true, maintainAspectRatio: false,
        plugins: { legend: { display: c.kind === "pie" || !!c.series } },
        scales: c.kind === "pie" ? {} : { y: { beginAtZero: true } },
      },
    });
  }

  /* ---------- map (Leaflet, vendored; circleMarker only — no icon assets) */
  const MAPS = {};
  // pin colors: strong tier variants + default blue for unmatched rows —
  // the in-map legend reads from this same table
  const MAP_TIER = { hot: "#006100", warm: "#9c6500", now: "#9c0006", std: "#8a99ab" };
  const MAP_DEFAULT = "#2f6fab";
  function renderMap(p, mount, s) {
    if (typeof L === "undefined") {
      mount.innerHTML = `<div class="ws-empty">Leaflet not loaded</div>`;
      return;
    }
    const m = s.map || { lat: "lat", lng: "lng", label: "name" };
    const keyIdx = {};
    s.columns.forEach((c, i) => (keyIdx[c.key] = i));
    const li = keyIdx[m.lat], gi = keyIdx[m.lng];
    if (li == null || gi == null) {
      mount.innerHTML = `<div class="ws-empty">map spec missing lat/lng columns</div>`;
      return;
    }
    mount.innerHTML = `<div class="mapbox" style="height:${(p && p.mapH) || 340}px"></div>`;
    if (MAPS[s.artifact_id]) { MAPS[s.artifact_id].remove(); delete MAPS[s.artifact_id]; }
    const box = mount.querySelector(".mapbox");
    const map = L.map(box);
    watchResize(box, p || {}, "mapH", s.artifact_id, "m",
                () => requestAnimationFrame(() => map.invalidateSize()));
    MAPS[s.artifact_id] = map;
    L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 18,
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
    }).addTo(map);
    const pts = [];
    for (const r of s.rows) {
      const lat = parseFloat(r[li]), lng = parseFloat(r[gi]);
      if (Number.isNaN(lat) || Number.isNaN(lng)) continue;
      pts.push([lat, lng]);
      const cls = tierClass(s, r);
      const color = MAP_TIER[cls.slice(5)] || MAP_DEFAULT;
      const marker = L.circleMarker([lat, lng], {
        radius: 6, color, weight: 1.5, fillOpacity: 0.65,
      }).addTo(map);
      const popupCols = m.popup_cols || [m.label || "name"];
      const html = popupCols.map((k) => {
        const i = keyIdx[k];
        return i != null && r[i] ? `<div><b>${esc(k)}:</b> ${esc(r[i])}</div>` : "";
      }).join("");
      marker.bindPopup(html || "(no details)");
    }
    if (!pts.length) {
      mount.innerHTML = `<div class="ws-empty">no mappable rows (missing lat/lng)</div>`;
      return;
    }
    map.fitBounds(pts, { padding: [20, 20], maxZoom: 11 });
    const entries = legendEntries(s);
    if (entries) {
      const ctl = L.control({ position: "bottomleft" });
      ctl.onAdd = () => {
        const div = L.DomUtil.create("div", "map-legend");
        div.innerHTML = entries.map((e) => `
          <div class="lg-row">
            <span class="lg-dot" style="background:${e.cls ? MAP_TIER[e.cls] : MAP_DEFAULT}"></span>
            <span>${esc(e.label)}</span><span class="lg-n">(${e.count})</span>
          </div>`).join("");
        return div;
      };
      ctl.addTo(map);
    }
  }

  /* ---------- CSV ---------- */
  function exportCsv(s) {
    const cols = visibleCols(s);
    const escCsv = (v) => {
      const str = String(v ?? "");
      return /[",\n]/.test(str) ? `"${str.replace(/"/g, '""')}"` : str;
    };
    const head = cols.map((c) => escCsv(c.label || c.key)).join(",");
    const lines = s.rows.map((r) => cols.map((c) => escCsv(r[c.idx])).join(","));
    const blob = new Blob(["﻿" + [head, ...lines].join("\r\n")],
                          { type: "text/csv;charset=utf-8" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `${(s.title || s.artifact_id).replace(/[^\w-]+/g, "_")}.csv`;
    a.click();
    URL.revokeObjectURL(a.href);
  }

  /* ---------- job progress panels ---------- */
  const JOBS = {};   // job_id -> {card, es}
  function onJobEvent(ev, data) {
    if (ev === "job_started") {
      let j = JOBS[data.job_id];
      if (!j) {
        const card = document.createElement("div");
        card.className = "panel-card";
        card.innerHTML = `
          <div class="panel-head"><span class="panel-title">&#9203; ${esc(data.title || data.tool)}</span></div>
          <div class="panel-body job-chip">
            <div class="job-msg">queued…</div>
            <progress max="100"></progress>
            <div class="job-log" style="font-size:11px;color:var(--muted);max-height:90px;overflow:auto"></div>
          </div>`;
        document.querySelector("#panels").prepend(card);
        j = JOBS[data.job_id] = { card };
        updateEmpty();
        // live progress via job SSE
        const es = new EventSource(`/api/jobs/${data.job_id}/events`);
        j.es = es;
        es.addEventListener("log", (e) => {
          const d = JSON.parse(e.data);
          const box = card.querySelector(".job-log");
          box.insertAdjacentHTML("beforeend", `<div>${esc(d.msg)}</div>`);
          box.scrollTop = box.scrollHeight;
        });
        es.addEventListener("job_update", (e) => {
          const d = JSON.parse(e.data);
          card.querySelector(".job-msg").textContent =
            `${d.status}${d.message ? " — " + d.message : ""}`;
          const pr = card.querySelector("progress");
          if (d.progress_total) { pr.max = d.progress_total; pr.value = d.progress_done || 0; }
        });
        es.addEventListener("job_done", async (e) => {
          const d = JSON.parse(e.data);
          es.close();
          if (d.artifact_id) {
            card.remove();
            delete JOBS[data.job_id];
            const spec = await fetchSpec(d.artifact_id);
            upsertPanel(spec);
          } else {
            card.querySelector(".job-msg").textContent = "done (no artifact)";
          }
        });
        es.addEventListener("error", (e) => {
          try {
            const d = JSON.parse(e.data || "{}");
            if (d.message) card.querySelector(".job-msg").textContent = "FAILED: " + d.message;
          } catch (err) { /* transport error — leave as-is */ }
        });
      }
    }
  }

  function updateEmptyPublic() { updateEmpty(); }

  function removeArtifacts(ids) {
    for (const id of ids || []) {
      const p = panels[id];
      if (p) { p.card.remove(); delete panels[id]; }
      if (CHARTS[id]) { CHARTS[id].destroy(); delete CHARTS[id]; }
      if (MAPS[id]) { MAPS[id].remove(); delete MAPS[id]; }
    }
    updateEmpty();
  }

  return { reset, onArtifactEvent, onJobEvent, updateEmpty: updateEmptyPublic,
           removeArtifacts,
           mdToHtml, tierClass, aggregate,
           ruleLabel, legendEntries, fmtCell,
           defaultColWidth };   // trailing ones exported for tests
})();
