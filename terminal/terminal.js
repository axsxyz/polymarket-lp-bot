/* qv/Polymarket operator terminal — display only. No order controls. */
(() => {
  const $ = (id) => document.getElementById(id);
  const TYPES = [
    "all", "fill", "post", "cancel", "skip", "exception",
    "seed", "filter", "universe", "cycle", "state", "snapshot",
  ];
  const BUCKET_CLASS = {
    fetched: "b-fetched",
    gamma_matched: "b-gamma",
    gamma_miss: "b-gamma",
    "no end_date": "b-no-end",
    "midpoint out of range": "b-mid",
    "too short": "b-short",
    "too illiquid": "b-illiquid",
    "too liquid": "b-liquid",
    "spread too narrow": "b-spread",
    "low reward": "b-reward",
    "capital too expensive": "b-capital",
    "zero spread or size": "b-zero",
    ranked: "b-ranked",
    selected: "b-selected",
  };
  const dash = (v) => (v === null || v === undefined || v === "" ? "—" : v);
  const n4 = (v) => (typeof v === "number" && Number.isFinite(v) ? v.toFixed(4) : "—");
  const n2 = (v) => (typeof v === "number" && Number.isFinite(v) ? v.toFixed(2) : "—");
  const n0 = (v) => (typeof v === "number" && Number.isFinite(v) ? String(Math.round(v)) : "—");
  const shortCid = (c) => (c ? String(c).slice(0, 8) : "—");
  const usd = (v) => (typeof v === "number" && Number.isFinite(v) ? `$${v.toFixed(2)}` : "—");
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));

  const DISCORD_HINT = {
    startup: "online / offline",
    orders: "order posted",
    cycle: "cycle summary + status dashboard",
    fills: "trade fills",
    skew: "skew warning / pause",
    errors: "send_error",
    daily: "daily LP report",
    alerts: "generic send_alert",
  };

  let digest = null;
  let selectedCid = null;
  let tapeFilter = "all";
  let lastSeq = 0;
  let tapeRows = [];
  const prevMids = {};
  let sampleBucket = null;
  const seenSeq = new Set();
  const freshSeq = new Set();
  let discordBusy = false;

  const PARIS = {
    timeZone: "Europe/Paris",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  };
  const parisHms = (d) => new Intl.DateTimeFormat("fr-FR", PARIS).format(d);

  function badge(label, on, kind) {
    const cls = kind || (on ? "on" : "off");
    return `<span class="badge ${cls}">${esc(label)}</span>`;
  }

  function renderClock() {
    const foot = $("clock-foot");
    if (foot) foot.textContent = parisHms(new Date()) + " PT";
  }

  function setLive(ok, label) {
    const b = $("foot-live");
    if (b) b.innerHTML = `<span class="dot ${ok ? "live" : "dead"}"></span> ${esc(label)}`;
  }

  function focusView(id) {
    const paneId = id || "markets";
    document.body.dataset.view = paneId;
    document.querySelectorAll(".nav-a, .nav-settings").forEach((x) => {
      x.classList.toggle("on", x.dataset.nav === paneId);
    });
    document.querySelectorAll(".pane").forEach((p) => p.classList.remove("focus"));
    const pane = $(paneId);
    if (pane) {
      pane.classList.remove("focus");
      void pane.offsetWidth;
      pane.classList.add("focus");
    }
    const right = $("right");
    if (right && paneId !== "right") right.scrollTop = 0;
    if (paneId === "settings") pullDiscord();
  }

  function setNavCollapsed(collapsed) {
    document.body.classList.toggle("nav-collapsed", collapsed);
    const btn = $("nav-toggle");
    if (btn) {
      const chev = btn.querySelector(".chev");
      if (chev) chev.textContent = collapsed ? "›" : "‹";
      btn.title = collapsed ? "Show nav" : "Hide nav";
      btn.setAttribute("aria-label", collapsed ? "Show navigation" : "Hide navigation");
    }
    try {
      localStorage.setItem("qv_nav_collapsed", collapsed ? "1" : "0");
    } catch (_) { /* ignore */ }
  }

  function toggleNav() {
    setNavCollapsed(!document.body.classList.contains("nav-collapsed"));
  }

  function tickHeartbeat() {
    document.querySelectorAll(".hb.live").forEach((el) => {
      el.classList.remove("tick");
      void el.offsetWidth;
      el.classList.add("tick");
    });
  }

  function renderHeader(d) {
    const h = d.header || {};
    $("badges").innerHTML = [
      `<span class="hb ${h.clob_ok ? "live" : "dead"}" title="CLOB"></span>`,
      `<span class="hb ${h.ws_connected ? "live" : "dead"}" title="WS"></span>`,
      badge("DRY_RUN", h.dry_run, h.dry_run ? "amber" : "on"),
      badge(h.mode || "n/a", true, "on"),
      badge(h.clob_ok ? "CLOB" : "CLOB DN", h.clob_ok),
      badge(h.ws_connected ? "WS" : (h.ws_enabled ? "WS DN" : "WS OFF"), h.ws_connected),
      badge(h.tape_ok ? "TAPE" : "TAPE ERR", h.tape_ok),
    ].join("");
    $("hdr-stats").textContent = [
      `cfg ${usd(h.total_capital)}`,
      `max ${dash(h.max_markets)}`,
      `${h.dry_run ? "paper" : "wallet"} ${usd(h.wallet_collateral)}`,
      `cash ${usd(h.spendable)}`,
      `cyc #${dash(h.cycle_n)}`,
      `${n2(h.elapsed_s)}s`,
      `post ${dash(h.posted)} keep ${dash(h.kept)} err ${dash(h.errors)}`,
    ].join("  ·  ");
    const p = (d && d.pnl) || {};
    const pnlEl = $("m-pnl");
    if (pnlEl) {
      const r = p.realised;
      const est = p.est_rewards_usd;
      const note = p.est_note || "ESTIM, not CLOB Q-score";
      pnlEl.textContent = usd(r);
      pnlEl.className = "mv" + (r > 0 ? " pos" : r < 0 ? " neg" : "");
      const estLine = $("m-pnl-est");
      if (estLine) {
        estLine.textContent = `est rewards ${usd(est)} (${note})`;
      }
    }
    const cap = $("m-cap");
    if (cap) cap.textContent = usd(h.total_capital);
    const sel = $("m-sel");
    if (sel) sel.textContent = `${dash(h.selected)} / ${dash(h.max_markets)}`;
    const cyc = $("m-cyc");
    if (cyc) cyc.textContent = `#${dash(h.cycle_n)}  ${dash(h.posted)}/${dash(h.kept)}/${dash(h.errors)}`;
    const allOk = !!(h.clob_ok && h.tape_ok && (h.ws_connected || !h.ws_enabled));
    setLive(allOk, allOk ? "All services live" : "degraded");
    const mid = $("foot-mid");
    if (mid) mid.textContent = `qv/Polymarket · ${h.mode || "—"} · interval ${n0(h.interval_s)}s`;
    tickHeartbeat();
  }

  function renderFunnel(d) {
    const f = d.funnel || {};
    const fetched = f.fetched || 0;
    const gm = f.gamma_matched || 0;
    const ranked = f.ranked || 0;
    const selN = (f.selected || []).length;
    const maxM = f.max_markets || (d.header || {}).max_markets || "—";
    const rc = f.rejection_counts || {};
    const gammaBit = f.gamma_status != null
      ? ` <span class="${f.gamma_status === 200 ? "" : "warn"}">(status ${esc(f.gamma_status)}${f.pages ? `, ${f.pages}p` : ""})</span>`
      : " <span class='muted'>(gamma n/a)</span>";
    const rejBits = [
      "gamma_miss", "no end_date", "midpoint out of range", "too short", "too illiquid",
      "spread too narrow", "capital too expensive", "low reward",
    ].filter((k) => rc[k] != null)
      .map((k) => `${k} <b>${rc[k]}</b>`)
      .join(" · ");
    $("funnel-pipe").innerHTML =
      `CLOB fetched <b>${fetched}</b> → Gamma matched <b>${gm}</b>${gammaBit}` +
      (rejBits ? ` → ${rejBits}` : "") +
      ` → ranked <b>${ranked}</b> → selected <b>${selN}</b> / MAX_MARKETS ${esc(String(maxM))}`;

    const counts = Object.assign(
      { fetched, gamma_matched: gm, ranked, selected: selN },
      rc,
    );
    const max = Math.max(1, fetched || 1);
    const order = [
      "fetched", "gamma_matched", "gamma_miss", "no end_date", "midpoint out of range",
      "too short", "too illiquid", "too liquid", "spread too narrow",
      "low reward", "capital too expensive", "zero spread or size",
      "ranked", "selected",
    ];
    const keys = order.filter((k) => counts[k] !== undefined);
    Object.keys(counts).forEach((k) => { if (!keys.includes(k)) keys.push(k); });

    const host = $("funnel-bars");
    const seen = new Set();
    keys.forEach((k) => {
      seen.add(k);
      const n = counts[k] || 0;
      const pct = Math.min(100, (n / max) * 100);
      let row = host.querySelector(`[data-bucket="${CSS.escape(k)}"]`);
      if (!row) {
        row = document.createElement("div");
        row.className = "bar-row";
        row.dataset.bucket = k;
        row.innerHTML = `<span class="bar-lab"></span><div class="bar-track"><div class="bar-fill"></div></div><span class="bar-n"></span>`;
        host.appendChild(row);
        requestAnimationFrame(() => {
          row.querySelector(".bar-fill").style.width = pct + "%";
        });
      } else {
        row.querySelector(".bar-fill").style.width = pct + "%";
      }
      row.querySelector(".bar-lab").textContent = k;
      row.querySelector(".bar-n").textContent = n;
      const fill = row.querySelector(".bar-fill");
      fill.className = "bar-fill " + (BUCKET_CLASS[k] || "b-other");
      row.classList.toggle("active", sampleBucket === k);
    });
    [...host.querySelectorAll(".bar-row")].forEach((row) => {
      if (!seen.has(row.dataset.bucket)) row.remove();
    });
    host.onclick = (ev) => {
      const row = ev.target.closest(".bar-row");
      if (!row) return;
      sampleBucket = row.dataset.bucket;
      renderFunnel(digest);
    };

    let sampleHtml = "";
    if (!sampleBucket) {
      sampleHtml = `<div class="muted">click a bucket — samples are real rejects / selected, not placeholders</div>`;
    } else if (sampleBucket === "selected" || sampleBucket === "ranked" || sampleBucket === "fetched" || sampleBucket === "gamma_matched") {
      const list = sampleBucket === "selected" ? (f.selected || []) : [];
      if (!list.length) {
        sampleHtml = `<div class="muted">no ${esc(sampleBucket)} samples (pipeline stage, not a reject bucket)</div>`;
      } else {
        sampleHtml = list.slice(0, 8).map((s) => sampleLine(s, "selected")).join("");
      }
    } else {
      const samples = (f.samples || {})[sampleBucket] || [];
      sampleHtml = samples.length
        ? samples.map((s) => sampleLine(s, sampleBucket)).join("")
        : `<div class="muted">no samples for ${esc(sampleBucket)}</div>`;
    }
    $("funnel-samples").innerHTML = sampleHtml;
  }

  function sampleLine(s, bucket) {
    const why = s.why || (bucket === "selected" ? "passed filters + rank cut" : bucket);
    return `<div class="sample-row">
      <div>${esc((s.question || "—").slice(0, 64))}  <span class="muted">${shortCid(s.cid)}</span></div>
      <div class="muted">mid ${n4(s.mid)} · spr ${n4(s.spread)} · $/d ${n2(s.reward != null ? s.reward : s.reward_day)} · min ${n0(s.min_size)} · ${esc(why)}</div>
    </div>`;
  }

  function stateChip(st) {
    return `<span class="chip-st st-${esc(st)}">${esc(st)}</span>`;
  }

  function renderMarkets(d) {
    const rows = d.markets || [];
    if (!selectedCid && rows[0]) selectedCid = rows[0].cid;
    const cnt = $("mkt-count");
    if (cnt) cnt.textContent = rows.length ? `${rows.length} markets` : "";
    const body = $("mkt-body");
    if (!rows.length) {
      body.innerHTML = `<tr><td colspan="17" class="muted">no markets selected this session</td></tr>`;
      return;
    }
    const html = rows.map((m) => {
      const st = m.state || "UNKNOWN";
      let midCls = "";
      const prev = prevMids[m.cid];
      if (typeof prev === "number" && typeof m.mid === "number" && Number.isFinite(prev) && Number.isFinite(m.mid) && m.mid !== prev) {
        midCls = m.mid > prev ? "mid-up" : "mid-dn";
      }
      if (typeof m.mid === "number" && Number.isFinite(m.mid)) prevMids[m.cid] = m.mid;
      const seed = m.seed_ok === false
        ? "fail"
        : (m.seed_ok === true ? (m.seed_dry_run ? "would" : "ok") : "—");
      const sel = m.cid === selectedCid ? "sel" : "";
      const tags = (m.tags || []).slice(0, 3).join(",") || "—";
      return `<tr class="${sel}" data-cid="${esc(m.cid)}">
        <td>${dash(m.rank)}</td>
        <td class="q" title="${esc(m.question || "")}">${esc((m.question || "—").slice(0, 28))}</td>
        <td>${shortCid(m.cid)}</td>
        <td>${stateChip(st)}</td>
        <td class="${midCls}">${n4(m.mid)}</td>
        <td>${n4(m.spread)}</td>
        <td>${n0(m.min_size)}</td>
        <td>${n2(m.score)}</td>
        <td class="q" title="${esc(tags)}">${esc(tags)}</td>
        <td>${n2(m.reward_day)}</td>
        <td class="num-yes">${n4(m.yes_bid != null ? m.yes_bid : m.bid)}</td>
        <td class="num-yes">${n4(m.yes_ask != null ? m.yes_ask : m.ask)}</td>
        <td class="num-no">${n4(m.no_bid)}</td>
        <td class="num-no">${n4(m.no_ask)}</td>
        <td class="num-yes">${n2(m.yes)}</td>
        <td class="num-no">${n2(m.no)}</td>
        <td>${n0(m.alloc)}</td>
        <td>${seed}</td>
        <td>${dash(m.skip_reason)}</td>
      </tr>`;
    }).join("");
    body.innerHTML = html;
    body.onclick = (ev) => {
      const tr = ev.target.closest("tr[data-cid]");
      if (!tr) return;
      selectedCid = tr.dataset.cid;
      renderMarkets(digest);
      renderDetail(digest);
    };
  }

  function renderDetail(d) {
    const m = (d.markets || []).find((x) => x.cid === selectedCid);
    const el = $("detail-body");
    if (!m) {
      el.innerHTML = `<span class="muted">No market highlighted. Empty book is real.</span>`;
      drawSpark([]);
      return;
    }
    el.innerHTML = `<div class="kv">
      <span>question</span><span>${esc(m.question || "—")}</span>
      <span>state</span><span>${stateChip(m.state)}</span>
      <span>rank / score</span><span>${dash(m.rank)} / ${n2(m.score)} · tags ${esc((m.tags || []).join(", ") || "—")}</span>
      <span>intended yes</span><span class="num-yes">${n4(m.intended_bid)} / ${n4(m.intended_ask)}</span>
      <span>last yes</span><span class="num-yes">${n4(m.yes_bid != null ? m.yes_bid : m.bid)} / ${n4(m.yes_ask != null ? m.yes_ask : m.ask)}</span>
      <span>last no</span><span class="num-no">${n4(m.no_bid)} / ${n4(m.no_ask)}</span>
      <span>inv yes/no</span><span><span class="num-yes">${n2(m.yes)}</span> / <span class="num-no">${n2(m.no)}</span></span>
      <span>seed_reason</span><span>${dash(m.seed_reason)}</span>
      <span>skip_reason</span><span>${dash(m.skip_reason)}</span>
      <span>seed $</span><span>Y ${usd(m.seed_yes)}  N ${usd(m.seed_no)}  cap ${usd(m.seed_cap)}</span>
    </div>
    <div class="why">${esc(m.why || (m.state === "SCORING" ? "Two-sided quotes live (or empty skip list)." : "No skip recorded."))}</div>`;
    drawSpark(m.mid_hist || []);
  }

  function drawSpark(hist) {
    const c = $("spark");
    const ctx = c.getContext("2d");
    const w = c.width;
    const h = c.height;
    ctx.fillStyle = "#12151a";
    ctx.fillRect(0, 0, w, h);
    const pts = hist.filter((x) => typeof x === "number");
    if (pts.length < 2) return;
    const min = Math.min(...pts);
    const max = Math.max(...pts);
    const span = max - min || 0.01;
    const xy = pts.map((p, i) => ({
      x: (i / (pts.length - 1)) * (w - 6) + 3,
      y: h - 5 - ((p - min) / span) * (h - 10),
    }));
    ctx.beginPath();
    xy.forEach((p, i) => { if (i === 0) ctx.moveTo(p.x, p.y); else ctx.lineTo(p.x, p.y); });
    ctx.lineTo(xy[xy.length - 1].x, h);
    ctx.lineTo(xy[0].x, h);
    ctx.closePath();
    ctx.fillStyle = "rgba(120, 146, 255, 0.14)";
    ctx.fill();
    ctx.beginPath();
    xy.forEach((p, i) => { if (i === 0) ctx.moveTo(p.x, p.y); else ctx.lineTo(p.x, p.y); });
    ctx.strokeStyle = "#7892FF";
    ctx.lineWidth = 1;
    ctx.stroke();
    const last = xy[xy.length - 1];
    ctx.beginPath();
    ctx.arc(last.x, last.y, 3.2, 0, Math.PI * 2);
    ctx.fillStyle = "#7892FF";
    ctx.fill();
    ctx.beginPath();
    ctx.arc(last.x, last.y, 6, 0, Math.PI * 2);
    ctx.strokeStyle = "rgba(120, 146, 255, 0.45)";
    ctx.stroke();
  }

  function renderPnl(d) {
    const p = d.pnl || {};
    const r = p.realised;
    const cls = r > 0 ? "pos" : (r < 0 ? "neg" : "");
    const est = p.est_rewards_usd;
    const note = p.est_note || "ESTIM, not CLOB Q-score";
    $("pnl-body").innerHTML = `
      <div class="big ${cls}">${usd(r)}</div>
      <div class="muted">realised · mark-to-mid per market</div>
      <div>est rewards ${usd(est)} (${note})</div>
      <div>quoted sess ${usd(p.session_quoted)} · filled sess ${usd(p.session_filled)}</div>
      ${(p.markets || []).map((m) =>
        `<div>${shortCid(m.cid)}  mtm ${usd(m.mark_to_mid)}  <span class="num-yes">yes ${n2(m.yes)}</span> <span class="num-no">no ${n2(m.no)}</span></div>`
      ).join("") || "<div class='muted'>no inventory</div>"}`;
  }

  function renderSeed(d) {
    const seeds = d.seeds || [];
    if (!seeds.length) {
      $("seed-body").innerHTML = `<div class="muted">no seed events this session</div>`;
      return;
    }
    $("seed-body").innerHTML = seeds.map((s) => {
      const tag = s.ok ? (s.dry_run ? "would" : "ok") : "fail";
      const cls = s.ok ? "st-SCORING" : "st-kill";
      return `<div class="seed-row"><span class="chip-st ${cls}">${tag}</span><span>${shortCid(s.cid)}  Y ${usd(s.yes_cost)} N ${usd(s.no_cost)} cap ${usd(s.cap)} ${dash(s.reason)}</span></div>`;
    }).join("");
  }

  function renderCol(d) {
    const c = d.collateral || {};
    const skip = c.last_skip;
    $("col-body").innerHTML = `
      <div>config capital ${usd(c.config_capital)} <span class="muted">${c.cash_source === "paper" ? "cfg, not paper cash" : "not wallet"}</span></div>
      <div>${c.cash_source === "paper" ? "paper" : "wallet"} ${usd(c.wallet)} · spendable ${usd(c.spendable)}</div>
      ${skip ? `<div class="st-kill">last BID skip need ${usd(skip.needed_usd)} vs ${usd(skip.available_usd)}</div>` : `<div class="muted">no collateral skip yet</div>`}
      <div class="muted">${esc(c.note || "")}</div>`;
  }

  function renderChips() {
    $("chips").innerHTML = TYPES.map((t) =>
      `<button class="chip ${tapeFilter === t ? "on" : ""}" data-t="${t}">${t}</button>`
    ).join("");
    $("chips").onclick = (ev) => {
      const b = ev.target.closest("[data-t]");
      if (!b) return;
      tapeFilter = b.dataset.t;
      renderChips();
      renderTape();
    };
  }

  function renderTape() {
    const list = tapeFilter === "all"
      ? tapeRows
      : tapeRows.filter((e) => e.type === tapeFilter);
    $("tape-list").innerHTML = list.slice(0, 200).map((e) => {
      const ts = e.ts ? parisHms(new Date(e.ts)) + " PT" : "—";
      const fresh = e.seq && freshSeq.has(e.seq) ? "fresh" : "";
      const summary = e.type === "snapshot"
        ? `${esc(shortCid(e.cid))} mid ${n4(e.mid)} <span class="num-yes">y ${n2(e.yes)}</span> <span class="num-no">n ${n2(e.no)}</span>`
        : esc(summarize(e));
      return `<div class="tape-row ${fresh}"><span class="muted">${ts}</span><span class="t-${e.type}">${e.type}</span><span>${summary}</span></div>`;
    }).join("") || `<div class="muted" style="padding:8px">no events yet</div>`;
  }

  function summarize(e) {
    const cid = shortCid(e.cid);
    switch (e.type) {
      case "skip": {
        const extra = e.needed_usd != null ? ` need ${usd(e.needed_usd)} vs ${usd(e.available_usd)}` : "";
        return `${cid} ${e.reason || ""}${extra}`;
      }
      case "fill":
      case "post":
      case "cancel":
        return `${cid} ${e.side || ""} ${n4(e.price)} x ${n2(e.size)}`;
      case "seed":
        return `${cid} ${e.ok ? "ok" : "fail"} ${e.reason || ""} Y ${usd(e.yes_cost)} N ${usd(e.no_cost)}`;
      case "cycle":
        return `#${e.n} sel ${e.selected} act ${e.active} post ${e.posted} keep ${e.kept} err ${e.errors} ${n2(e.elapsed_s)}s`;
      case "state":
        return `${cid} ${e.from} → ${e.to} ${e.reason || ""}`;
      case "universe":
        return `fetched ${e.fetched} gamma ${e.gamma_matched} status ${dash(e.gamma_status)} pages ${e.pages}`;
      case "filter": {
        const rc = e.rejection_counts || {};
        const top = Object.entries(rc).sort((a, b) => b[1] - a[1]).slice(0, 3)
          .map(([k, v]) => `${k}:${v}`).join(" ");
        return `ranked ${e.ranked || 0} sel ${(e.selected || []).length} ${top}`;
      }
      case "exception":
        return `${e.where || ""} ${e.exc || ""}`;
      case "snapshot":
        return `${cid} mid ${n4(e.mid)} y ${n2(e.yes)} n ${n2(e.no)}`;
      default:
        return JSON.stringify(e).slice(0, 80);
    }
  }

  function pushEvent(e) {
    if (!e || !e.type) return;
    const seq = e.seq;
    if (seq) {
      if (seenSeq.has(seq)) return;
      seenSeq.add(seq);
      freshSeq.add(seq);
      if (seq > lastSeq) lastSeq = seq;
      setTimeout(() => freshSeq.delete(seq), 800);
    }
    tapeRows.unshift(e);
    if (tapeRows.length > 500) tapeRows.length = 500;
    renderTape();
  }

  function renderDiscord(data) {
    const body = $("discord-body");
    const wh = $("discord-wh");
    const note = $("discord-note");
    if (!body) return;
    if (wh) {
      wh.textContent = data.webhook ? "webhook on" : "webhook off / missing";
      wh.className = "muted tiny " + (data.webhook ? "ok" : "");
    }
    if (note) note.textContent = data.note || "";
    const kinds = data.kinds || [];
    const enabled = data.enabled || {};
    const disabled = !data.webhook;
    const rows = kinds.map((k) => {
      const on = !!enabled[k];
      return `<label class="discord-row ${on ? "on" : ""}">
        <span>
          <div class="discord-lab">${esc(k)}</div>
          <div class="discord-sub">${esc(DISCORD_HINT[k] || "")}</div>
        </span>
        <input type="checkbox" class="discord-tog" data-kind="${esc(k)}"
          ${on ? "checked" : ""} ${disabled ? "disabled" : ""}>
      </label>`;
    }).join("");
    body.innerHTML = `
      <div class="discord-actions">
        <button type="button" class="discord-btn" data-dc="all" ${disabled ? "disabled" : ""}>all on</button>
        <button type="button" class="discord-btn" data-dc="none" ${disabled ? "disabled" : ""}>all off</button>
        <button type="button" class="discord-btn" data-dc="quiet" ${disabled ? "disabled" : ""}>errors+skew only</button>
      </div>
      ${rows || `<div class="muted">No kinds.</div>`}`;
    body.querySelectorAll(".discord-tog").forEach((el) => {
      el.addEventListener("change", () => syncDiscordFromUi());
    });
    body.querySelectorAll("[data-dc]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const mode = btn.dataset.dc;
        const boxes = [...body.querySelectorAll(".discord-tog")];
        boxes.forEach((b) => {
          if (mode === "all") b.checked = true;
          else if (mode === "none") b.checked = false;
          else if (mode === "quiet") {
            b.checked = b.dataset.kind === "errors"
              || b.dataset.kind === "skew"
              || b.dataset.kind === "startup"
              || b.dataset.kind === "alerts";
          }
        });
        syncDiscordFromUi();
      });
    });
  }

  function collectDiscordChannels() {
    return [...document.querySelectorAll("#discord-body .discord-tog")]
      .filter((el) => el.checked)
      .map((el) => el.dataset.kind);
  }

  async function syncDiscordFromUi() {
    if (discordBusy) return;
    discordBusy = true;
    try {
      const r = await fetch("/discord/channels", {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ channels: collectDiscordChannels() }),
      });
      if (!r.ok) return;
      renderDiscord(await r.json());
    } catch (_) { /* keep UI */ }
    finally { discordBusy = false; }
  }

  async function pullDiscord() {
    try {
      const r = await fetch("/discord/channels");
      if (!r.ok) return;
      renderDiscord(await r.json());
    } catch (_) { /* ignore */ }
  }

  function paint(d) {
    digest = d;
    renderHeader(d);
    renderFunnel(d);
    renderMarkets(d);
    renderDetail(d);
    renderPnl(d);
    renderSeed(d);
    renderCol(d);
  }

  async function pullDigest() {
    try {
      const r = await fetch("/digest");
      if (!r.ok) return;
      paint(await r.json());
    } catch (_) { /* keep last paint */ }
  }

  async function pullEvents() {
    try {
      const r = await fetch(`/events?since=${lastSeq}&limit=200`);
      if (!r.ok) return;
      const data = await r.json();
      const evs = (data.events || []).slice().reverse();
      evs.forEach(pushEvent);
      if (data.seq) lastSeq = Math.max(lastSeq, data.seq);
    } catch (_) { /* ignore */ }
  }

  function startSse() {
    if (typeof EventSource === "undefined") {
      setInterval(pullEvents, 400);
      return;
    }
    const es = new EventSource("/stream");
    es.onmessage = (msg) => {
      try { pushEvent(JSON.parse(msg.data)); } catch (_) { /* skip */ }
    };
    setInterval(pullEvents, 2000);
  }

  document.querySelectorAll("[data-nav]").forEach((el) => {
    el.addEventListener("click", () => focusView(el.dataset.nav));
  });
  const navToggle = $("nav-toggle");
  if (navToggle) navToggle.addEventListener("click", (e) => {
    e.stopPropagation();
    toggleNav();
  });
  try {
    if (localStorage.getItem("qv_nav_collapsed") === "1") setNavCollapsed(true);
  } catch (_) { /* ignore */ }
  focusView("markets");

  renderChips();
  renderClock();
  setInterval(renderClock, 250);
  pullDigest();
  setInterval(pullDigest, 1000);
  startSse();
  pullEvents();
})();
