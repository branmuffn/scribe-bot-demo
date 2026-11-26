/* Gmail-style redesign (Brian's picks, 2026-11-23). Vanilla JS, event delegation (works after htmx swaps).
   A1/A2 sidebar fold + peek · A3 Quick settings (density, reading pane) · A1 reading pane with a drag line ·
   C1 phone drawer (☰ or a swipe in from the left edge). Swipe-to-go-back was removed (Brian, 2026-11-23). */
(function () {
  var html = document.documentElement;
  var wide = window.matchMedia("(min-width: 960px)");
  function cookie(k, v) { document.cookie = k + "=" + v + "; path=/; max-age=31536000; samesite=lax"; }
  function store(k, v) { try { if (v == null) localStorage.removeItem(k); else localStorage.setItem(k, v); } catch (_) {} }
  function load(k) { try { return localStorage.getItem(k); } catch (_) { return null; } }
  function closest(t, sel) { return t && t.closest ? t.closest(sel) : null; }
  var inPane = html.classList.contains("in-pane");

  /* ---------- A2: fold the sidebar to icons; hovering the rail shows it in full for a moment ---------- */
  var peekT = null;
  document.addEventListener("click", function (e) {
    if (!closest(e.target, "[data-side-toggle]")) return;
    var side = document.querySelector(".sidebar");
    var rail = !html.classList.contains("side-rail") || (side && side.classList.contains("peek") && false);
    if (side && side.classList.contains("peek")) { rail = false; side.classList.remove("peek"); }
    html.classList.toggle("side-rail", rail); cookie("side", rail ? "rail" : "open");
  });
  document.addEventListener("mouseover", function (e) {
    var side = closest(e.target, ".sidebar");
    if (!side || !html.classList.contains("side-rail") || side.classList.contains("peek") || closest(e.target, "[data-side-toggle]")) return;
    clearTimeout(peekT); peekT = setTimeout(function () { side.classList.add("peek"); }, 120);   // B2: was 280 ms; felt laggy
  });
  document.addEventListener("mouseout", function (e) {
    var side = closest(e.target, ".sidebar"); if (!side) return;
    if (e.relatedTarget && side.contains(e.relatedTarget)) return;
    if (side.querySelector("details[open] > .fx-body")) return;
    clearTimeout(peekT); side.classList.remove("peek");
  });

  /* ---------- A3: Quick settings ---------- */
  function paintQs() {
    document.querySelectorAll("[data-density-set]").forEach(function (b) { var on = b.dataset.densitySet === html.dataset.density; b.classList.toggle("on", on); b.setAttribute("aria-pressed", on); });
    document.querySelectorAll("[data-pane-set]").forEach(function (b) { var on = b.dataset.paneSet === html.dataset.pane; b.classList.toggle("on", on); b.setAttribute("aria-pressed", on); });
    document.querySelectorAll("[data-pnotes-set]").forEach(function (b) { var on = b.dataset.pnotesSet === html.dataset.pnotes; b.classList.toggle("on", on); b.setAttribute("aria-pressed", on); });
  }
  document.addEventListener("click", function (e) {
    var d = closest(e.target, "[data-density-set]");
    if (d) { html.dataset.density = d.dataset.densitySet; cookie("density", d.dataset.densitySet); paintQs();
      // F68: the reading pane follows a density change at once too
      document.querySelectorAll("iframe").forEach(function (f) { try { var fd = f.contentDocument && f.contentDocument.documentElement; if (fd) fd.dataset.density = d.dataset.densitySet; } catch (_) {} });
      return; }
    var pn = closest(e.target, "[data-pnotes-set]");   // B13: people notes as bullets, a paragraph or lines
    if (pn) { html.dataset.pnotes = pn.dataset.pnotesSet; cookie("pnotes", pn.dataset.pnotesSet); paintQs();
      // F68: and so does the people-notes look
      document.querySelectorAll("iframe").forEach(function (f) { try { var fd = f.contentDocument && f.contentDocument.documentElement; if (fd) fd.dataset.pnotes = pn.dataset.pnotesSet; } catch (_) {} });
      return; }
    var p = closest(e.target, "[data-pane-set]");
    if (p) { html.dataset.pane = p.dataset.paneSet; cookie("pane", p.dataset.paneSet); paintQs(); paneMode(); return; }
    var clr = closest(e.target, ".adv-form [type=reset]");   // Clear empties every field (reset would refill the current search)
    if (clr) { e.preventDefault(); clr.form.querySelectorAll("input").forEach(function (i) { i.value = ""; });
      clr.form.querySelectorAll("select").forEach(function (s) { s.selectedIndex = 0; }); var q = clr.form.querySelector("[name=q]"); if (q) q.focus(); }
  });

  /* ---------- A1: reading pane ---------- */
  function pane() { return document.getElementById("rpane"); }
  function frame() { var p = pane(); return p && p.querySelector("[data-rpane-frame]"); }
  function paneOn() { return !!pane() && wide.matches && html.dataset.pane !== "none"; }
  function sizes() {
    var w = parseInt(load("paneW"), 10), h = parseInt(load("paneH"), 10);
    if (w) html.style.setProperty("--pane-w", clampW(w) + "px");
    if (h) html.style.setProperty("--pane-h", clampH(h) + "px");
  }
  function sideW() { var s = document.querySelector(".sidebar"); return s ? s.getBoundingClientRect().width : 0; }
  function clampW(w) { return Math.max(300, Math.min(w, innerWidth - sideW() - 460)); }   // F14: the list keeps ~460 px
  function clampH(h) { return Math.max(200, Math.min(h, innerHeight - 220)); }
  function paneMode() {
    var p = pane(); if (!p) { document.body && document.body.classList.remove("pane-on"); return; }
    var on = paneOn();
    document.body.classList.toggle("pane-on", on); p.hidden = !on;
    if (on && !p.dataset.ready) { p.dataset.ready = "1"; p.classList.add("empty"); sizes(); }
  }
  function rowOf(a) { return closest(a, "#conv-list .item, #people-list .item"); }
  function openIn(href, row) {
    var p = pane(), f = frame(); if (!p || !f) return;
    document.querySelectorAll("#conv-list .item.current, #people-list .item.current").forEach(function (x) { x.classList.remove("current"); });
    if (row) { row.classList.add("current"); row.querySelectorAll(".badge-new").forEach(function (x) { x.remove(); }); }   // F101: opened, so no longer New
    p.classList.remove("empty"); f.src = href; p.dataset.href = href;
    var full = p.querySelector("[data-rpane-full]"); if (full) full.href = href;
  }
  function closePane() {
    var p = pane(), f = frame(); if (!p) return;
    p.classList.add("empty"); if (f) f.removeAttribute("src"); delete p.dataset.href;
    document.querySelectorAll(".item.current").forEach(function (x) { x.classList.remove("current"); });
  }
  document.addEventListener("click", function (e) {
    if (!paneOn() || e.defaultPrevented || e.button || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    var a = closest(e.target, '#conv-list .item a.item-link[href^="/conversations/"], #people-list .item a.item-link[href^="/people/"]');
    var list = closest(e.target, "#conv-list, #people-list");
    if (!a || !list || (list.classList.contains("picking") && !matchMedia("(pointer: fine)").matches)) return;
    e.preventDefault(); e.stopPropagation();
    openIn(a.getAttribute("href"), rowOf(a));
  }, true);
  document.addEventListener("click", function (e) {
    if (closest(e.target, "[data-rpane-close]")) { closePane(); return; }
    var full = closest(e.target, "[data-rpane-full]");
    if (full) { e.preventDefault(); var p = pane(); if (p && p.dataset.href) location.href = p.dataset.href; }
  });
  /* F106 (1024 pass): Esc closed the pane only while every <details> on the page was shut, so with the sidebar's More
     places open (or a folded list group) it never closed. Only a pop-up that is open holds it back now.
     F107: Esc pressed inside the pane (after a click there) closes it too, and the keys go back to the list. */
  var POPS = ["speaker-chip", "tag-add", "line-edit", "edit", "filters", "row-menu", "chip-menu", "ts-row", "fx-sheet"]
    .map(function (k) { return "details." + k + "[open]"; }).join(", ") +
    ", details.in-row[open], details.task-sheet[open], details.mention-sheet[open], dialog[open], .picking, .ctx-menu, .pp-sheet:not([hidden]), .sp-sheet:not([hidden]), .pk2:not([hidden])";
  /* F114 (1024 pass): app.js loads first, so its Esc handlers had already shut Quick settings (or Filter, Export, a ⋯
     menu) before this one looked, and one Esc closed the menu and the pane together. What was open is now noted as the
     key goes down (capture on window runs before every other handler), so the first Esc closes only the menu. */
  /* F115: Esc while typing in a box on the list (the top search, Search talks) closed the pane too; inside the pane a box
     already held it back. A box with the cursor in it now counts like an open menu. */
  function typing() { var a = document.activeElement; return !!(a && (a.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(a.tagName))); }
  var popOpen = false;
  window.addEventListener("keydown", function (e) { if (e.key === "Escape") popOpen = !!document.querySelector(POPS) || typing(); }, true);
  function escClose() {
    if (!paneOn() || !pane() || pane().classList.contains("empty") || popOpen || document.querySelector(POPS)) return;
    var at = document.querySelector("#conv-list .item.current a.item-link, #people-list .item.current a.item-link");
    closePane(); if (at) at.focus();
  }
  document.addEventListener("keydown", function (e) { if (e.key === "Escape") escClose(); });
  if (inPane) document.addEventListener("keydown", function (e) {   // capture: before app.js shuts what's open
    if (e.key !== "Escape" || document.querySelector(POPS) || typing()) return;
    try { parent.postMessage({ sbPaneEsc: 1 }, location.origin); } catch (_) {}
  }, true);
  if (!inPane) window.addEventListener("message", function (e) {
    if (e.origin === location.origin && e.data && e.data.sbPaneEsc) { popOpen = false; escClose(); }   // the key went down in the pane, not here
  });
  // a page inside the pane that leaves its talk or person (a delete, a merge): the list may have changed, so reload it
  document.addEventListener("load", function (e) {
    if (e.target !== frame()) return;
    var f = frame(), path;
    try { path = f.contentWindow.location.pathname; } catch (_) { return; }
    if (!f.getAttribute("src") || path === "blank") return;
    if (!/^\/(conversations|people)\/\d+$/.test(path)) { closePane(); location.reload(); return; }
    var p = pane(), again = p.dataset.seen === path; p.dataset.href = path; p.dataset.seen = path;
    var full = p.querySelector("[data-rpane-full]"); if (full) full.href = path;
    if (again) { var mm = /^\/(conversations|people)\/(\d+)$/.exec(path); refreshRow(mm[1], mm[2]); }   // the same talk reloaded after a change: refresh its row
  }, true);
  // drag the line between the list and the pane
  var drag = null;
  document.addEventListener("pointerdown", function (e) {
    var d = closest(e.target, "[data-rpane-drag]"); if (!d) return;
    e.preventDefault(); drag = { below: html.dataset.pane === "below" }; pane().classList.add("dragging"); document.body.classList.add("pane-dragging");
    try { d.setPointerCapture(e.pointerId); } catch (_) {}
  });
  document.addEventListener("pointermove", function (e) {
    if (!drag) return;
    if (drag.below) { var h = clampH(innerHeight - e.clientY); html.style.setProperty("--pane-h", h + "px"); drag.v = h; }
    else { var w = clampW(innerWidth - e.clientX); html.style.setProperty("--pane-w", w + "px"); drag.v = w; }
  });
  ["pointerup", "pointercancel"].forEach(function (ev) { document.addEventListener(ev, function () {
    if (!drag) return; if (drag.v) store(drag.below ? "paneH" : "paneW", String(Math.round(drag.v)));
    drag = null; var p = pane(); if (p) p.classList.remove("dragging"); document.body.classList.remove("pane-dragging"); }); });
  addEventListener("resize", function () { if (paneOn()) sizes(); });
  wide.addEventListener ? wide.addEventListener("change", paneMode) : wide.addListener(paneMode);

  /* B10 (2026-11-25): naming a speaker (or any other change) in the pane left the list beside it stale ("3 to name"
     stayed). After a change inside the pane, the list re-reads that one row from the server and swaps it in place. */
  if (inPane) document.addEventListener("htmx:afterRequest", function (e) {
    var cfg = e.detail && e.detail.requestConfig, m = /^\/(conversations|people)\/(\d+)/.exec(location.pathname);
    if (!m || !e.detail.successful || !cfg || (cfg.verb || "").toLowerCase() === "get") return;
    try { parent.postMessage({ sbRowChanged: m[2], kind: m[1] }, location.origin); } catch (_) {}
  });
  if (!inPane) window.addEventListener("message", function (e) {
    if (e.origin !== location.origin || !e.data || !e.data.sbRowChanged) return;
    refreshRow(e.data.kind, e.data.sbRowChanged);
  });
  function refreshRow(kind, rid) {
    var id = String(rid).replace(/\D/g, ""), sel = (kind === "people" ? "#people-list" : "#conv-list") + ' .item[data-id="' + id + '"]';
    var row = document.querySelector(sel); if (!row) return;
    fetch(location.href, { credentials: "same-origin" }).then(function (r) { return r.ok ? r.text() : ""; }).then(function (t) {
      if (!t) return;
      var fresh = new DOMParser().parseFromString(t, "text/html").querySelector(sel), cur = document.querySelector(sel);
      if (!fresh || !cur) return;
      if (cur.classList.contains("current")) fresh.classList.add("current");
      if (cur.classList.contains("picked")) fresh.classList.add("picked");
      cur.replaceWith(fresh); if (window.htmx) htmx.process(fresh);
    }).catch(function () {});
  }

  /* Inside the pane: links to other pages open in the whole window (the pane only ever shows one talk or person) */
  if (inPane) document.addEventListener("click", function (e) {
    var a = closest(e.target, "a[href]"); if (!a || e.defaultPrevented || a.hasAttribute("download") || a.target) return;
    var u = new URL(a.href, location.href);
    if (u.origin !== location.origin || (u.pathname === location.pathname && u.search === location.search)) return;   // same page: its own jump links
    e.preventDefault(); e.stopPropagation(); top.location.href = u.href;
  }, true);

  /* ---------- C1: phone drawer ---------- */
  function drawer() { return document.getElementById("drawer"); }
  function setDp(v, anim) { var d = drawer(); if (!d) return; d.classList.toggle("anim", !!anim); d.style.setProperty("--dp", String(v)); }
  function openDrawer() { var d = drawer(); if (!d) return; d.hidden = false; setDp(0, false); requestAnimationFrame(function () { requestAnimationFrame(function () { setDp(1, true); }); }); html.classList.add("pop-lock"); }
  function closeDrawer() { var d = drawer(); if (!d || d.hidden) return; setDp(0, true); html.classList.remove("pop-lock"); setTimeout(function () { if (d.style.getPropertyValue("--dp") === "0") d.hidden = true; }, 260); }
  document.addEventListener("click", function (e) {
    if (closest(e.target, "[data-drawer-open]")) { openDrawer(); return; }
    if (closest(e.target, "[data-drawer-close]") || closest(e.target, ".drawer a")) closeDrawer();
  });
  document.addEventListener("keydown", function (e) { if (e.key === "Escape") closeDrawer(); });
  document.addEventListener("htmx:beforeSwap", function () { var d = drawer(); if (d) { d.hidden = true; setDp(0); html.classList.remove("pop-lock"); } });

  /* ---------- Touch gestures: edge swipe = drawer (swipe-to-go-back removed, Brian 2026-11-23) ---------- */
  var EDGE = 28, g = null, bubble = null;
  function busy() { return document.querySelector("details.fx-sheet[open], details.row-menu[open], details.chip-menu[open], dialog[open], .pp-sheet:not([hidden]), .picking, .pk2:not([hidden]), .ctx-menu, .sp-sheet:not([hidden])"); }
  function scrollsX(el) {
    for (; el && el !== document.body; el = el.parentElement) {
      if (el.scrollWidth > el.clientWidth + 2) { var o = getComputedStyle(el).overflowX; if (o === "auto" || o === "scroll") return true; }
    }
    return false;
  }
  function excluded(t) {
    return closest(t, "[data-sw], .sw-host, .sw-layer, input, textarea, select, audio, video, .lines, .rail, .rail-out, .player, .talk, .sp-bar, .drawer, .up-card, .undo-bar, .tabs, [contenteditable], .no-swipe") || scrollsX(t);
  }
  function canForward() { return !(window.navigation && navigation.canGoForward === false); }
  function canBack() { return !(window.navigation && navigation.canGoBack === false) && history.length > 1; }
  document.addEventListener("touchstart", function (e) {
    if (wide.matches || inPane || e.touches.length !== 1) { g = null; return; }
    var t = e.touches[0], d = drawer();
    if (d && !d.hidden) { g = closest(e.target, ".drawer-body") || closest(e.target, ".drawer-back") ? { kind: "close", x0: t.clientX, y0: t.clientY } : null; return; }
    if (busy()) { g = null; return; }
    if (t.clientX <= EDGE && d) { g = { kind: "edge", x0: t.clientX, y0: t.clientY }; return; }
    g = null;   // no swipe-to-go-back on empty space (Brian, 2026-11-23: too many other swipes; it got confused)
  }, { passive: true });
  document.addEventListener("touchmove", function (e) {
    if (!g || e.touches.length !== 1) return;
    var t = e.touches[0], dx = t.clientX - g.x0, dy = t.clientY - g.y0;
    if (!g.dir) {
      if (Math.abs(dx) < 10 && Math.abs(dy) < 10) return;
      if (Math.abs(dy) > Math.abs(dx) * (g.kind === "nav" ? 0.8 : 1.5)) { if (g.kind === "edge") closeDrawer(); g = null; hideBubble(); return; }   // a scroll
      g.dir = dx > 0 ? 1 : -1;
      if (g.kind === "edge" && g.dir < 0) { g = null; return; }
      if (g.kind === "edge") { var d = drawer(); d.hidden = false; setDp(0); html.classList.add("pop-lock"); }
      if (g.kind === "nav" && (g.dir > 0 ? !canBack() : !canForward())) { g = null; return; }
    }
    var db = document.querySelector(".drawer-body"), w = (db && db.offsetWidth) || Math.min(272, innerWidth * 0.76);
    if (g.kind === "edge") { g.v = Math.max(0, Math.min(1, dx / w)); setDp(g.v); }
    else if (g.kind === "close") { g.v = Math.max(0, Math.min(1, 1 + dx / w)); setDp(g.v); }
    else { showBubble(g.dir, Math.abs(dx)); g.go = Math.abs(dx) > Math.min(110, innerWidth * 0.28) && g.dir === (dx > 0 ? 1 : -1); }
    // speed over the last ~100 ms (not since the start), so a quick flick at the end counts (Brian: it snapped shut)
    (g.pts = g.pts || []).push([e.timeStamp, t.clientX]); while (g.pts.length > 2 && e.timeStamp - g.pts[0][0] > 100) g.pts.shift();
  }, { passive: true });
  document.addEventListener("touchend", function (e) {
    if (!g) return;
    var P = g.pts || [], speed = P.length > 1 && P[P.length - 1][0] > P[0][0] ? (P[P.length - 1][1] - P[0][1]) / (P[P.length - 1][0] - P[0][0]) : 0;   // px per ms
    // open past a quarter, or on any rightward flick; a leftward flick always closes
    if (g.kind === "edge") { if (speed < -0.15 || ((g.v || 0) < 0.25 && speed < 0.2)) closeDrawer(); else setDp(1, true); }
    else if (g.kind === "close" && g.dir) { if (speed < -0.2 || ((g.v || 1) < 0.75 && speed < 0.15)) closeDrawer(); else setDp(1, true); }
    else if (g.kind === "nav" && g.dir) { var go = g.go; hideBubble(go); if (go) { if (g.dir > 0) history.back(); else history.forward(); } }
    g = null;
  });
  document.addEventListener("touchcancel", function () { if (g && g.kind === "edge") closeDrawer(); hideBubble(); g = null; });
  function showBubble(dir, dist) {
    if (!bubble) { bubble = document.createElement("div"); bubble.className = "nav-swipe"; bubble.setAttribute("aria-hidden", "true");
      bubble.innerHTML = '<svg class="icon"><use href="#i-chev"/></svg>'; document.body.appendChild(bubble); }
    var p = Math.min(1, dist / Math.min(110, innerWidth * 0.28));
    bubble.style.transition = "none";
    bubble.style.left = dir > 0 ? (-44 + p * 64) + "px" : "auto";
    bubble.style.right = dir < 0 ? (-44 + p * 64) + "px" : "auto";
    bubble.style.opacity = String(Math.min(1, p * 1.4));
    bubble.firstChild.style.transform = dir > 0 ? "rotate(180deg)" : "none";
    bubble.classList.toggle("go", p >= 1);
  }
  function hideBubble(went) {
    if (!bubble) return; bubble.style.transition = "opacity .2s, left .2s, right .2s";
    bubble.style.opacity = "0"; if (!went) { bubble.style.left = bubble.style.left !== "auto" ? "-44px" : "auto"; bubble.style.right = bubble.style.right !== "auto" ? "-44px" : "auto"; }
  }

  function ready() { paneMode(); paintQs(); }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", ready); else ready();
  document.addEventListener("htmx:afterSettle", ready);
  addEventListener("pageshow", function () { closeDrawer(); hideBubble(); });
})();

/* Talk page (Brian's picks D, E2, F2, 2026-11-23) */
(function () {
  function closest(t, sel) { return t && t.closest ? t.closest(sel) : null; }
  var html = document.documentElement;
  /* D: the speaker bar opens the full list (tap outside, ✕ or Esc closes it) */
  function sheet() { return document.getElementById("sp-sheet"); }
  document.addEventListener("click", function (e) {
    var opener = closest(e.target, "[data-sp-open]");
    if (opener) { var s = sheet(); if (!s) return; s.hidden = false; html.classList.add("pop-lock");
      // B5 (2026-11-25): a tap on one color opens that speaker's row; "N to name" opens the first one still to name.
      var seg = closest(e.target, ".sp-seg[data-sp-id]"), row = null;
      if (seg) row = s.querySelector('.speaker-chip[data-sp-id="' + seg.dataset.spId + '"]');
      else if (opener.hasAttribute("data-sp-first-open")) row = s.querySelector(".speaker-chip.sp-unnamed");
      s.querySelectorAll(".speaker-chip[open]").forEach(function (d) { if (d !== row) d.open = false; });
      if (row) { row.open = true; setTimeout(function () { row.scrollIntoView({ block: "nearest" }); }, 0); }
      return; }
    if (closest(e.target, "[data-sp-close]")) { var s2 = sheet(); if (s2) { s2.hidden = true; html.classList.remove("pop-lock"); } }
  });
  document.addEventListener("keydown", function (e) { var s = sheet(); if (e.key === "Escape" && s && !s.hidden && !document.querySelector(".pp-sheet:not([hidden])")) { s.hidden = true; html.classList.remove("pop-lock"); } });
  // after a fix the page part is swapped in: a sheet that was open stays open
  document.addEventListener("htmx:beforeSwap", function () { var s = sheet(); window.__spWasOpen = !!(s && !s.hidden); });
  document.addEventListener("htmx:afterSettle", function () { var s = sheet(); if (s && window.__spWasOpen) s.hidden = false; else html.classList.toggle("pop-lock", !!(s && !s.hidden)); fold(); });

  /* E2: area, type and tags on one line; the rest behind "+N" (tap it to see them all, "Less" folds them) */
  function fitLabels() {   // a name that doesn't fit its color shows just the %, or nothing (never cut off)
    document.querySelectorAll(".sp-bar .sp-lab").forEach(function (l) {
      if (!l.dataset.full) l.dataset.full = l.textContent;
      l.style.visibility = ""; l.textContent = l.dataset.full;
      if (l.scrollWidth <= l.clientWidth + 1) return;
      l.textContent = l.dataset.full.split(" ").pop();
      if (l.scrollWidth > l.clientWidth + 1) l.style.visibility = "hidden";
    });
  }
  function fold() {
    fitLabels();
    document.querySelectorAll("[data-pill-fold]").forEach(function (box) {
      var more = box.querySelector(".pf-more");
      if (!more) { more = document.createElement("button"); more.type = "button"; more.className = "pf-more"; box.appendChild(more); }
      var kids = [].filter.call(box.children, function (k) { return k !== more; });
      kids.forEach(function (k) { k.classList.remove("pf-hide"); }); more.hidden = true;
      if (box.classList.contains("pf-open")) { more.hidden = false; more.textContent = "Hide"; return; }
      if (kids.some(function (k) { return k.matches("details[open]"); })) return;
      var top = kids.length ? kids[0].offsetTop : 0, cut = -1;
      for (var i = 0; i < kids.length; i++) if (kids[i].offsetTop > top + 4) { cut = i; break; }
      if (cut < 0) return;
      more.hidden = false;
      var n = kids.length - cut; more.textContent = "+" + n;
      for (var j = cut; j < kids.length; j++) kids[j].classList.add("pf-hide");
      while (cut > 0 && more.offsetTop > top + 4) { cut--; kids[cut].classList.add("pf-hide"); more.textContent = "+" + (kids.length - cut); }   // the +N itself fits on the line
    });
  }
  function foldNow(box) {   // close "+ Tag" and fold in the same frame, so "Less" never moves before it goes away
    [].forEach.call(box.querySelectorAll("details[open]"), function (d) { d.open = false; });
    delete box.dataset.autoOpen; box.classList.remove("pf-open"); fold();
  }
  // C7 (Brian, 2026-11-23): "Less" jumped up for a split second before going away. The tap first closed "+ Tag" (the
  // pills reflowed and "Less" moved), and the fold only came a frame later. Now the press itself folds, at once.
  document.addEventListener("pointerdown", function (e) {
    var m = closest(e.target, ".pf-more"); if (!m || e.button > 0) return;
    var box = m.parentElement; if (!box.classList.contains("pf-open")) return;
    foldNow(box); box.dataset.skipClick = "1"; setTimeout(function () { delete box.dataset.skipClick; }, 600);
  }, true);
  document.addEventListener("click", function (e) {
    var m = closest(e.target, ".pf-more"); if (!m) return;
    var box = m.parentElement;
    if (box.dataset.skipClick) { delete box.dataset.skipClick; return; }   // already folded on the press
    if (box.classList.contains("pf-open")) foldNow(box); else { delete box.dataset.autoOpen; box.classList.add("pf-open"); fold(); }
  });
  var ft = 0; addEventListener("resize", function () { clearTimeout(ft); ft = setTimeout(fold, 80); });
  // "+ Tag" opens the pills to make room; closing it (✕, Esc, a tap outside or on "Less") folds them back. Round 2 bug
  // (Brian, 2026-11-23): a tap on "Less" only closed "+ Tag", so the pills stayed open with "Less" still showing.
  document.addEventListener("toggle", function (e) {
    var b = closest(e.target, "[data-pill-fold]"); if (!b || !e.target.matches("details")) return;
    if (e.target.open) { if (!b.classList.contains("pf-open")) { b.classList.add("pf-open"); b.dataset.autoOpen = "1"; } fold(); }
    else if (b.dataset.autoOpen) { delete b.dataset.autoOpen; b.classList.remove("pf-open"); fold(); }
  }, true);

  /* F2: press and hold the summary to copy it all; double-tap selects a word (then the phone's own Copy works) */
  function note(t) {
    var n = document.querySelector(".copy-note"); if (!n) { n = document.createElement("div"); n.className = "copy-note"; n.setAttribute("role", "status"); document.body.appendChild(n); }
    n.textContent = t; requestAnimationFrame(function () { n.classList.add("on"); }); clearTimeout(n._t); n._t = setTimeout(function () { n.classList.remove("on"); }, 1600);
  }
  window.ScribeToast = note;
  function copyText(card) {
    var det = card.querySelector(".notes-detailed:not([hidden]) .detail-notes"), recap = document.getElementById("recap");
    var text = det ? det.innerText : recap ? recap.value : card.innerText;
    var ok = function () { note(det ? "Detailed notes copied" : "Summary copied"); };
    if (navigator.clipboard && window.isSecureContext) navigator.clipboard.writeText(text.trim()).then(ok, function () { note("Couldn't copy"); });
    else { var ta = document.createElement("textarea"); ta.value = text.trim(); document.body.appendChild(ta); ta.select(); try { document.execCommand("copy"); ok(); } catch (_) { note("Couldn't copy"); } ta.remove(); }
  }
  var hold = null, held = 0;
  document.addEventListener("pointerdown", function (e) {
    var card = closest(e.target, "[data-hold-copy]");
    if (!card || e.button || closest(e.target, "a, button, input, textarea, select, form, summary, .pencil") || card.querySelector("form.inline-edit")) return;
    var x = e.clientX, y = e.clientY;
    hold = { card: card, x: x, y: y, t: setTimeout(function () { held = Date.now(); card.classList.remove("holding"); copyText(card);
      var s = getSelection && getSelection(); if (s) s.removeAllRanges(); hold = null; }, 520) };
    card.classList.add("holding");
  });
  function stopHold(e) { if (!hold) return; if (e && e.type === "pointermove" && Math.hypot(e.clientX - hold.x, e.clientY - hold.y) < 9) return;
    clearTimeout(hold.t); hold.card.classList.remove("holding"); hold = null; }
  ["pointerup", "pointercancel", "pointermove", "scroll"].forEach(function (ev) { document.addEventListener(ev, stopHold, true); });
  document.addEventListener("contextmenu", function (e) { if (closest(e.target, "[data-hold-copy]") && (hold || Date.now() - held < 900)) e.preventDefault(); });
  document.addEventListener("selectionchange", function () { if (Date.now() - held < 900) { var s = getSelection(); if (s && !s.isCollapsed) s.removeAllRanges(); } });
  // double-tap on a touch screen: select the word under the finger (computers already do this)
  var lastTap = null;
  document.addEventListener("pointerup", function (e) {
    if (e.pointerType !== "touch" || !closest(e.target, "[data-hold-copy] .summary-text, [data-hold-copy] .takeaways, [data-hold-copy] .detail-notes")) { lastTap = null; return; }
    var now = Date.now();
    if (lastTap && now - lastTap.t < 350 && Math.hypot(e.clientX - lastTap.x, e.clientY - lastTap.y) < 24) {
      lastTap = null;
      var r = document.caretRangeFromPoint ? document.caretRangeFromPoint(e.clientX, e.clientY) : null;
      var s = getSelection(); if (!r || !s) return;
      s.removeAllRanges(); s.addRange(r);
      if (s.modify) { s.modify("move", "backward", "word"); s.modify("extend", "forward", "word"); }
      return;
    }
    lastTap = { t: now, x: e.clientX, y: e.clientY };
  });

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", fold); else fold();
  addEventListener("load", fold);
})();

/* B2 (Brian, 2026-11-23): right-click any row on a computer for a menu. Round 2 (same day): on every list, not just
   Talks. The menu holds Open (the full page, one column) and Open in new tab, then the row's own swipe actions (the
   same Done / Delete / Summarize / Transcribe a phone swipe does), so every page works the same way. No "Select" (the
   checkboxes do that). Icons in one column, labels on one left edge. Closes on a click outside, Esc, scrolling or a pick. */
(function () {
  function closest(t, sel) { return t && t.closest ? t.closest(sel) : null; }
  var menu = null;
  function close() { if (menu) { menu.remove(); menu = null; } }
  function ic(n) { return '<span class="ctx-ic"><svg class="icon" aria-hidden="true"><use href="#i-' + n + '"/></svg></span>'; }
  function item(icon, label, fn, cls) { var b = document.createElement("button"); b.type = "button"; b.className = "ctx-item" + (cls ? " " + cls : "");
    b.innerHTML = ic(icon) + "<span></span>"; b.lastChild.textContent = label; b.addEventListener("click", function () { close(); fn(); }); return b; }
  function sep() { return document.createElement("hr"); }
  var ROWS = "#conv-list .conv-row, #people-list .person-row, #inbox-list .in-row, .task-row, main .item[data-sw], main .item.tap-row, main a.item, main .overnight-card";
  function submit(f) { if (!f) return; if (f.requestSubmit) f.requestSubmit(); else f.submit(); }
  document.addEventListener("contextmenu", function (e) {
    if (!matchMedia("(pointer: fine)").matches) return;   // phones: long-press keeps selecting
    var inner = closest(e.target, "input, textarea, select, .fx-body, .menu-panel, .chip-pop, [contenteditable], .lines, .summary, .detail-notes, .sp-sheet, .cv-pills");
    var row = !inner && closest(e.target, ROWS); if (!row) { other(e); return; }
    var sheet = row.matches("details.fx-sheet") ? row : row.querySelector("details.fx-sheet");
    var link = row.matches("a[href]") ? row : row.querySelector("a.item-link[href], a.ts-link[href]");
    var href = link && link.getAttribute("href");
    var sw = null; try { sw = row.dataset.sw ? JSON.parse(row.dataset.sw) : null; } catch (_) {}
    if (!href && !sheet && !sw) { other(e); return; }
    e.preventDefault(); close();
    menu = document.createElement("div"); menu.className = "ctx-menu"; menu.setAttribute("role", "menu");
    var task = row.classList.contains("task-row"), talk = row.classList.contains("conv-row");
    // U13: a task's tap already opens its details, and its circle already marks it done: neither is repeated here
    if (sheet && !task) menu.appendChild(item("chev", "Open", function () { sheet.open = true; }));
    if (href) {
      var to = href.split("#")[0], what = task ? "talk" : "";
      if (!sheet) menu.appendChild(item("chev", "Open", function () { location.href = href; }));   // F42: its own icon, not the new-tab one
      else if (to) menu.appendChild(item("chat", "Open " + (what || "page"), function () { location.href = href; }));
      if (to) menu.appendChild(item("open", "Open in new tab", function () { window.open(href, "_blank"); }));
    }
    var acts = [], seen = {};
    if (sw) [].concat(sw.r || [], sw.l || []).forEach(function (a) { if (!seen[a.t] && !(task && a.t === "Done") && row.querySelector(a.f)) { seen[a.t] = 1; acts.push(a); } });
    if (task) { var tt = row.querySelector(".item-title"); if (tt) acts.unshift({ t: "Copy task", i: "copy", x: function () { copy(tt.textContent.trim(), "Task"); } }); }
    acts.sort(function (x, y) { return (x.c === "red") - (y.c === "red"); });   // Delete always last, after a line
    // D-SP-6 (Brian, 2026-11-26: 6A): both exports, named the same as in the talk's own ⋯ menu
    function dl(ext) { return function () { var l = document.createElement("a"); l.href = "/conversations/" + row.dataset.id + "/export." + ext;
      l.download = ""; l.setAttribute("hx-boost", "false"); document.body.appendChild(l); l.click(); l.remove(); }; }
    if (talk) acts.splice(acts.length - (seen.Delete ? 1 : 0), 0, { t: "Export as text file (.md)", i: "download", x: dl("md") },
                          { t: "Export as PDF", i: "download", x: dl("pdf") });
    if (acts.length && menu.children.length) menu.appendChild(sep());
    acts.forEach(function (a) {
      if (a.t === "Delete" && menu.lastChild && menu.lastChild.tagName !== "HR" && acts.length > 1) menu.appendChild(sep());
      menu.appendChild(item(a.i || "dots", a.t === "Delete" && (talk || row.classList.contains("in-row")) ? "Move to Trash" : a.t === "Tomorrow" ? "Remind me tomorrow morning" : a.t,
        a.x || function () { submit(row.querySelector(a.f)); }, a.c === "red" ? "danger" : ""));
    });
    if (!menu.children.length) { menu = null; return; }
    document.body.appendChild(menu);
    var w = menu.offsetWidth, h = menu.offsetHeight;
    menu.style.left = Math.max(8, Math.min(e.clientX, innerWidth - w - 8)) + "px";
    menu.style.top = Math.max(8, e.clientY + h > innerHeight - 8 ? e.clientY - h : e.clientY) + "px";
    var first = menu.querySelector("button"); if (first) first.focus({ preventScroll: true });
  });
  /* C4 round 2 (Brian, 2026-11-25: "various things don't right click"): everything that isn't a list row gets a menu
     that fits it. A transcript line (play, who said it, fix words, copy), a speaker, a tag or pill, any text you can
     edit, the summary, a link, and anywhere else on a page that has a ⋯ menu (its items, mirrored). Selected text,
     fields, the audio player and pages with nothing to offer keep the browser's own menu (Copy, Paste…). */
  function show(e, list) {
    list = list.filter(Boolean);
    while (list.length && list[list.length - 1] === "-") list.pop();
    if (!list.length) return false;
    e.preventDefault(); close();
    menu = document.createElement("div"); menu.className = "ctx-menu"; menu.setAttribute("role", "menu");
    list.forEach(function (x) {
      if (x === "-") { if (menu.lastChild && menu.lastChild.tagName !== "HR") menu.appendChild(sep()); return; }
      var b = item(x.i || "dots", x.t, x.fn, x.cls); if (x.on) b.setAttribute("aria-checked", "true"); menu.appendChild(b);
    });
    document.body.appendChild(menu);
    var w = menu.offsetWidth, h = menu.offsetHeight;
    menu.style.left = Math.max(8, Math.min(e.clientX, innerWidth - w - 8)) + "px";
    menu.style.top = Math.max(8, e.clientY + h > innerHeight - 8 ? e.clientY - h : e.clientY) + "px";
    var first = menu.querySelector("button"); if (first) first.focus({ preventScroll: true });
    return true;
  }
  function copy(text, what) {
    var done = function () { if (window.ScribeToast) window.ScribeToast(what + " copied"); };
    if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(text).then(done, function () {});
  }
  function editIt(x) { if (!x || !window.ScribeEdit) return; delete x.dataset.wrapped; if (x.dataset.editValue) x.textContent = x.dataset.editValue; window.ScribeEdit(x); }
  function mirror(panel) {   // a ⋯ menu's own items, so the right-click and the ⋯ do exactly the same thing
    var out = [];
    [].forEach.call(panel.querySelectorAll(".menu-item"), function (m) {
      if (m.closest("[hidden]") || m.disabled) return;
      var t = m.textContent.replace(/\s+/g, " ").trim(); if (!t) return;
      var u = m.querySelector("use"), ic2 = u ? (u.getAttribute("href") || "").replace("#i-", "") : "dots";
      var danger = m.classList.contains("danger");
      if (danger) out.push("-");
      out.push({ i: ic2, t: t, cls: danger ? "danger" : "", fn: function () { m.click(); } });
    });
    return out;
  }
  function lineMenu(line) {
    var seek = line.querySelector("[data-seek]"), who = line.querySelector(".who"), words = line.querySelector("[data-edit-url]"),
        fix = line.querySelector("details.line-edit"), ts = (line.querySelector(".t") || {}).textContent || "";
    var text = words ? (words.dataset.editValue || words.textContent).trim() : "", name = who ? who.textContent.trim() : "";
    return [seek && { i: "play", t: "Play from " + ts.trim(), fn: function () { seek.click(); } },
      who && { i: "users", t: "Who said this…", fn: function () { who.click(); } },
      words && { i: "pencil", t: "Fix words", fn: function () { editIt(words); } },
      fix && { i: "dots", t: "More fixes (who, words, join)", fn: function () { fix.open = true; } }, "-",
      text && { i: "copy", t: "Copy line", fn: function () { copy((ts ? ts.trim() + " " : "") + (name ? name + ": " : "") + text, "Line"); } },
      { i: "copy", t: "Copy link to this line", fn: function () { copy(location.origin + location.pathname + "?tx=1#" + line.id, "Link");   /* R4-10: ?tx=1 sends the transcript with the page, so the link opens at once */ } }];
  }
  function speakerMenu(id, seg) {
    var chip = document.querySelector('.speaker-chip[data-sp-id="' + id + '"]'); if (!chip) return [];
    var name = (chip.querySelector(".speaker-name") || {}).textContent || "this speaker", named = !chip.classList.contains("sp-unnamed") && !chip.classList.contains("sp-idk");
    var au = chip.querySelector("audio"), page = chip.querySelector('.sp-links a[href^="/people/"]'), notX = chip.querySelector('.sp-links form button');
    var lines = function () { return document.querySelectorAll('.lines .line[data-sp="' + id + '"]'); };
    return [{ i: "users", t: named ? "Change who " + name + " is…" : "Name " + name + "…", fn: function () { seg.click(); } },
      au && { i: "play", t: "Play their voice", fn: function () { au.play(); } },
      lines().length && { i: "chat", t: "Go to their first line", fn: function () {
        var l = lines()[0]; if (l) { l.scrollIntoView({ block: "center", behavior: "smooth" }); l.classList.add("flash"); setTimeout(function () { l.classList.remove("flash"); }, 1600); } } },
      lines().length && { i: "copy", t: "Copy what they said", fn: function () {
        copy([].map.call(lines(), function (l) { var w = l.querySelector("[data-edit-url]"); return w ? (w.dataset.editValue || w.textContent).trim() : ""; }).filter(Boolean).join("\n"), name + "'s lines"); } },
      page && { i: "chev", t: page.textContent.replace("›", "").trim(), fn: function () { location.href = page.href; } },
      notX && "-", notX && { i: "x", t: notX.textContent.trim(), cls: "danger", fn: function () { notX.click(); } }];
  }
  function other(e) {
    var t = e.target, sel = window.getSelection && String(window.getSelection()).trim();
    if (sel && closest(t, "main")) return;                       // selected words: the browser's Copy
    if (!closest(t, "main, .sp-sheet")) return;
    var pill = closest(t, "select.pill-sel");
    if (pill) return show(e, [].map.call(pill.options, function (o) {
      if (o.disabled) return null;
      return { i: o.selected ? "check" : "dots", t: o.textContent.trim(), on: o.selected, fn: function () { pill.value = o.value; pill.dispatchEvent(new Event("change", { bubbles: true })); } };
    }));
    if (closest(t, "input, textarea, select, audio, video, [contenteditable], .menu-panel, .chip-pop, .ctx-menu")) return;
    var line = closest(t, ".lines .line"); if (line) return show(e, lineMenu(line));
    // U12 (Brian, 2026-11-25): a right-click on one color of the speaker bar is about that speaker, not the talk
    var seg = closest(t, ".sp-bar .sp-seg[data-sp-id]");
    if (seg) return show(e, speakerMenu(seg.dataset.spId, seg));
    if (closest(t, ".sp-bar, .sp-todo")) return show(e, [{ i: "users", t: "Show all speakers", fn: function () { t.closest("[data-sp-open]").click(); } }]);
    var tag = closest(t, ".tag-chip:not(.tag-plus)");
    if (tag) { var ta = tag.querySelector("a[href]"), keep = tag.querySelector('form[action$="/keep"]'), rm = tag.querySelector('form[action$="/remove"]');
      return show(e, [ta && { i: "search", t: "Talks with " + ta.textContent.trim(), fn: function () { location.href = ta.href; } },
        keep && { i: "check", t: "Keep this tag", fn: function () { submit(keep); } }, "-",
        rm && { i: "x", t: "Remove tag", cls: "danger", fn: function () { submit(rm); } }]); }
    var chip = closest(t, "details.speaker-chip");
    if (chip) { var au = chip.querySelector("audio"), pl = chip.querySelector('a[href^="/people/"]');
      return show(e, [{ i: "users", t: "Who is it…", fn: function () { chip.open = true; var s = chip.querySelector("select"); if (s) s.focus(); } },
        au && { i: "play", t: "Play their voice", fn: function () { au.play(); } },
        { i: "dots", t: "Color, merge and more", fn: function () { chip.open = true; } },
        pl && { i: "chev", t: pl.textContent.replace("›", "").trim(), fn: function () { location.href = pl.href; } }]); }
    var sb = closest(t, ".fx-body"), btns = sb && sb.querySelector(".ts-btns");
    if (btns) {   // U13 (Brian, 2026-11-25): inside a task's or note's details, every row and button is already on screen,
      // so the right-click no longer repeats them: it offers what isn't there (copy, one-click reminders, the talk in a new tab)
      var ti = sb.querySelector(".sh-head [data-edit-url]"), title = ti ? (ti.dataset.editValue || ti.textContent).trim() : "";
      var tl = sb.querySelector("a.ts-link[href]"), when = function (v) { var i = sb.querySelector('input[value="' + v + '"]'); return i && i.form; };
      var tom = when("tomorrow"), mon = when("monday"), isTask = !!sb.querySelector('form[action$="/done"], form[action$="/reopen"]');
      return show(e, [title && { i: "copy", t: isTask ? "Copy task" : "Copy note", fn: function () { copy(title, isTask ? "Task" : "Note"); } },
        tom && { i: "clock", t: "Remind me tomorrow morning", fn: function () { submit(tom); } },
        mon && { i: "clock", t: "Remind me next Monday", fn: function () { submit(mon); } }, "-",
        tl && { i: "open", t: "Open the talk in a new tab", fn: function () { window.open(tl.href, "_blank"); } },
        tl && { i: "copy", t: "Copy link to the talk", fn: function () { copy(tl.href, "Link"); } }]);
    }
    var ed = closest(t, "[data-edit-url]");
    if (ed) { var v = (ed.dataset.editValue || ed.textContent).trim(), lab = ed.dataset.editLabel || "text";
      return show(e, [{ i: "pencil", t: "Edit " + lab, fn: function () { editIt(ed); } },
        v && { i: "copy", t: "Copy " + lab, fn: function () { copy(v, lab.charAt(0).toUpperCase() + lab.slice(1)); } }]); }
    var link = closest(t, "a[href]:not([href^='#']):not([download])");
    if (link && !closest(link, ".fx-body")) {
      var hr = link.href;
      return show(e, [{ i: "chev", t: "Open", fn: function () { location.href = hr; } },
        { i: "open", t: "Open in new tab", fn: function () { window.open(hr, "_blank"); } },
        { i: "copy", t: "Copy link", fn: function () { copy(hr, "Link"); } }]);
    }
    var box = t.closest(".card, section, details.section, .fx-body, header, .conv-head"), panel = null;
    while (box && !panel) { panel = box.querySelector(".menu-panel"); if (!panel) box = box.parentElement && box.parentElement.closest(".card, section, details.section, .fx-body, header, .conv-head"); }
    if (!panel && closest(t, ".summary, .detail-notes, #recap, .conv-head")) panel = document.querySelector("main .cv-bar .menu-panel");
    if (panel) return show(e, mirror(panel));
  }
  document.addEventListener("pointerdown", function (e) { if (menu && !menu.contains(e.target)) close(); }, true);
  document.addEventListener("keydown", function (e) {
    if (!menu) return;
    if (e.key === "Escape") { close(); return; }
    if (e.key === "ArrowDown" || e.key === "ArrowUp") { e.preventDefault(); var bs = [].slice.call(menu.querySelectorAll("button")), i = bs.indexOf(document.activeElement);
      bs[(i + (e.key === "ArrowDown" ? 1 : bs.length - 1)) % bs.length].focus(); }
  });
  addEventListener("scroll", close, true); addEventListener("resize", close); addEventListener("blur", close);
  document.addEventListener("htmx:beforeSwap", close);
})();

/* Shift-click ticks a range on the whole row, not only its checkbox (Brian's bug list): on Talks, Inbox and People a
   Shift-click anywhere on a row acts as a Shift-click on that row's checkbox, so it never opens the row in a new window.
   Runs on window, before the lists' own click handlers. Computers only (the checkbox must be showing). */
var PICK_ROW = "#conv-list .item, #inbox-list .in-row, [data-picker] [data-pick-id]", PICK_CB = "[data-row-cb], [data-in-cb], [data-pk-cb]";
function pickRowOf(e) {
  if (!e.shiftKey || e.button || e.metaKey || e.ctrlKey || e.altKey || !e.target.closest) return null;
  var row = e.target.closest(PICK_ROW); if (!row || e.target.closest(PICK_CB + ", .fx-body, .sw-layer, .show-more, input, select, textarea")) return null;
  var cb = row.querySelector(PICK_CB); return cb && cb.offsetParent !== null ? cb : null;
}
window.addEventListener("click", function (e) {
  var cb = pickRowOf(e); if (!cb) return;
  e.preventDefault(); e.stopImmediatePropagation();
  cb.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, shiftKey: true, view: window }));
}, true);
/* ...and the browser doesn't also highlight the text in between */
document.addEventListener("mousedown", function (e) {
  if (e.shiftKey && e.target.closest && (e.target.closest(".row-cb") || pickRowOf(e))) e.preventDefault();
});

/* The short note after Summarize or Transcribe (?note=...) shows once: a reload or Back doesn't bring it back */
(function () {
  if (!/[?&]note=/.test(location.search) || !history.replaceState) return;
  var u = new URL(location.href); u.searchParams.delete("note");
  history.replaceState(history.state, "", u.pathname + (u.searchParams.toString() ? "?" + u.searchParams : "") + u.hash);
})();

/* Reading pane on Inbox, Tasks and Today (computers): one recording's or task's details at a time, so clicking another
   row switches the panel (the list beside it stays usable; see app.css) */
document.addEventListener("toggle", function (e) {
  var d = e.target, SEL = "#inbox-list details.in-row, .task-row details.task-sheet";
  if (!d.open || !d.matches || !d.matches(SEL) || !matchMedia("(min-width: 960px)").matches || document.documentElement.dataset.pane === "none") return;
  document.querySelectorAll("#inbox-list details.in-row[open], .task-row details.task-sheet[open]").forEach(function (x) { if (x !== d) x.open = false; });
}, true);

/* Talk ⋯ › Edit key points (Brian, 2026-11-23: one pencil by the summary; key points are edited from ⋯) */
document.addEventListener("click", function (e) {
  var b = e.target.closest && e.target.closest("[data-edit-kp]"); if (!b) return;
  var d = b.closest("details"); if (d) d.open = false;
  var short = document.querySelector('[data-notes="short"]'); if (short && !short.classList.contains("on")) short.click();
  var ul = document.querySelector(".takeaways[data-edit-url]");
  if (ul && window.ScribeEdit) { window.ScribeEdit(ul); var f = ul.nextElementSibling; if (f && f.scrollIntoView) f.scrollIntoView({ block: "center", behavior: "smooth" }); }
});

/* C2 + C3: search filter pills, each opening a full list (people get their own search box) */
(function () {
  function closest(t, sel) { return t && t.closest ? t.closest(sel) : null; }
  var html = document.documentElement, COLORS = ["#2563eb", "#ea580c", "#16a34a", "#9333ea", "#db2777", "#0891b2", "#ca8a04", "#dc2626"];
  function esc(t) { return String(t).replace(/[&<>"]/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]; }); }
  function go(params) {
    var u = new URL(location.href); u.searchParams.delete("n"); u.searchParams.delete("conv");
    Object.keys(params).forEach(function (k) { if (params[k]) u.searchParams.set(k, params[k]); else u.searchParams.delete(k); });
    location.href = u.pathname + u.search;
  }
  function ymd(d) { return d.getFullYear() + "-" + ("0" + (d.getMonth() + 1)).slice(-2) + "-" + ("0" + d.getDate()).slice(-2); }
  var box = null, back = null;
  function close() { if (box) { box.remove(); box = null; } if (back) { back.remove(); back = null; } html.classList.remove("pop-lock"); }
  function row(v, label, sub, av, on) {
    return '<button type="button" class="sf-row' + (on ? " on" : "") + '" data-sf-v="' + esc(v) + '">' + av + '<span class="sf-t"><b>' + esc(label) + "</b>" +
      (sub ? "<small>" + esc(sub) + "</small>" : "") + '</span><span class="sf-tick" aria-hidden="true"></span></button>';
  }
  function avatar(name, i) {
    var ini = name.split(" ").filter(Boolean).slice(0, 2).map(function (w) { return w[0]; }).join("").toUpperCase();
    return '<span class="sf-av" style="background:' + COLORS[i % COLORS.length] + '">' + esc(ini || "?") + "</span>";
  }
  function open(d) {
    close(); html.classList.add("pop-lock");
    back = document.createElement("div"); back.className = "sp-full-back"; document.body.appendChild(back); back.addEventListener("click", close);
    box = document.createElement("div"); box.className = "sp-full"; box.setAttribute("role", "dialog"); box.setAttribute("aria-label", d.title);
    var body = "";
    if (d.dates) {
      var presets = [["", "Any time"], ["7", "Past week"], ["31", "Past month"], ["92", "Past 3 months"], ["366", "Past year"]];
      body = presets.map(function (p) { return row("d" + p[0], p[1], "", '<span class="sf-av" style="background:var(--surface-2);color:var(--ink-2)"><svg class="icon"><use href="#i-calendar"/></svg></span>', false); }).join("") +
        '<div class="sf-sec">Pick dates</div><div class="sf-dates"><input type="date" data-sf-from aria-label="From"><i>to</i><input type="date" data-sf-to aria-label="To"></div>' +
        '<div class="sf-apply"><button type="button" class="btn btn-primary" data-sf-apply>Apply</button></div>';
    } else {
      body = (d.people ? '<div class="sf-sec">Everyone A-Z</div>' : "") + d.opts.map(function (o, i) {
        return row(o.v, o.label, o.sub || "", d.people ? avatar(o.label, i) : "", d.cur === o.v || (!d.cur && d.key === "sort" && o.v === "best"));
      }).join("");
    }
    box.innerHTML = '<div class="sp-full-head"><button type="button" class="icon-btn" data-sf-close aria-label="Close"><svg class="icon"><use href="#i-x"/></svg></button><b>' + esc(d.title) + "</b></div>" +
      (d.people ? '<div class="sp-full-search pp-search"><svg class="icon"><use href="#i-search"/></svg><input class="pp-q" type="search" placeholder="Type a name" aria-label="Type a name" data-sf-q></div>' : "") +
      '<div class="sp-full-list">' + body + "</div>";
    document.body.appendChild(box); box._d = d;
  }
  document.addEventListener("click", function (e) {
    var x = closest(e.target, "[data-spill-clear]");
    if (x) { e.preventDefault(); e.stopPropagation(); var k = x.dataset.spillClear; go(k === "date" ? { start: "", end: "" } : k === "sort" ? { sort: "" } : (function () { var o = {}; o[k] = ""; return o; })()); return; }
    var p = closest(e.target, "[data-spill]");
    if (p) { try { open(JSON.parse(p.dataset.spill)); } catch (_) {} return; }
    if (!box) return;
    if (closest(e.target, "[data-sf-close]")) { close(); return; }
    var r = closest(e.target, ".sf-row");
    if (r && box.contains(r)) {
      var d = box._d, v = r.dataset.sfV;
      if (d.dates) { var days = v.slice(1), st = ""; if (days) { var t = new Date(); t.setDate(t.getDate() - parseInt(days, 10)); st = ymd(t); } go({ start: st, end: "" }); }
      else { var o = {}; o[d.key] = d.key === "sort" && v === "best" ? "" : v; go(o); }
      return;
    }
    if (closest(e.target, "[data-sf-apply]")) { go({ start: box.querySelector("[data-sf-from]").value, end: box.querySelector("[data-sf-to]").value }); }
  });
  document.addEventListener("input", function (e) {
    if (!box || !e.target.matches("[data-sf-q]")) return;
    var q = e.target.value.trim().toLowerCase();
    box.querySelectorAll(".sf-row").forEach(function (r) { r.hidden = q && r.querySelector("b").textContent.toLowerCase().indexOf(q) < 0; });
  });
  document.addEventListener("keydown", function (e) { if (e.key === "Escape" && box) close(); });
  document.addEventListener("htmx:beforeSwap", close);
})();

/* T7 (Brian, 2026-11-25): "‹ Talks" (and every other back link) sometimes took ~5 s. When the page it points to is the
   page you just came from, go back in history instead: the browser shows that page again at once, scrolled where you
   left it, with no new request to the M14. Otherwise (a link opened in a new tab, a different page) it loads normally. */
var movedHere = !!location.hash;   // a jump link (#section) adds history entries, so Back would only scroll
addEventListener("hashchange", function () { movedHere = true; });
document.addEventListener("htmx:pushedIntoHistory", function () { movedHere = true; });
document.addEventListener("click", function (e) {
  var a = e.target.closest && e.target.closest("a.cv-back, a.back-link");
  if (movedHere) return;
  if (!a || e.defaultPrevented || e.button || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || history.length < 2) return;
  var ref; try { ref = new URL(document.referrer); } catch (_) { return; }
  var to = new URL(a.href, location.href);
  if (ref.origin !== location.origin || ref.pathname !== to.pathname) return;
  if (document.documentElement.classList.contains("in-pane")) return;
  e.preventDefault();
  try { sessionStorage.setItem("sbTap", JSON.stringify({ p: ref.pathname + ref.search, at: performance.timeOrigin + e.timeStamp, wall: Date.now() })); } catch (_) {}   // B18: the speed log times Back
  history.back();
});

/* N1 rails (Brian, 2026-11-25): on a computer, the side the mouse is in keeps its rail (light; darker when pointed at),
   the other side's rail fades away until the mouse comes back over it. With a talk open beside the list, the list
   and the talk are two pages: the list hears the mouse go into the talk's frame, the talk hears it leave. */
(function () {
  var html = document.documentElement;
  if (!matchMedia("(hover: hover) and (pointer: fine)").matches) return;
  function away(on) { html.classList.toggle("mouse-away", on); }
  if (html.classList.contains("in-pane")) {
    html.addEventListener("mouseleave", function () { away(true); });
    html.addEventListener("mouseenter", function () { away(false); });
    away(true);   // until the mouse comes over the talk
  } else {
    document.addEventListener("mouseover", function (e) { away(!!(e.target.closest && e.target.closest("#rpane iframe"))); });
    document.addEventListener("mouseleave", function () { away(false); });
  }
})();

/* T8 + C11 (Brian, 2026-11-25): on a computer with Reading pane set to Right or Below, Inbox, Tasks and Today keep a
   pane open like Talks: empty ("pick one") until a row is picked, a drag line to make it wider or taller (remembered),
   and the list beside it scrolls on its own. A click elsewhere in the list doesn't close it; ✕ or Esc does. */
(function () {
  var html = document.documentElement, wide = matchMedia("(min-width: 960px)");
  var SHEETS = "#inbox-list details.in-row, .task-row details.task-sheet";
  /* R4-02 (Brian, 2026-11-25): a talk's "You owe" list also has task rows, so the pane opened over the talk itself.
     Only the list pages get the pane. */
  var LISTS = /^(today|tasks|inbox)$/;
  function store(k, v) { try { localStorage.setItem(k, v); } catch (_) {} }
  function load(k) { try { return localStorage.getItem(k); } catch (_) { return null; } }
  function sideW() { var s = document.querySelector(".sidebar"); return s ? s.getBoundingClientRect().width : 0; }
  function clampW(w) { return Math.max(300, Math.min(w, innerWidth - sideW() - 460)); }   // F14: the list keeps ~460 px
  function clampH(h) { return Math.max(180, Math.min(h, innerHeight - 220)); }
  function on() {
    return wide.matches && (html.dataset.pane === "right" || html.dataset.pane === "below") && !html.classList.contains("in-pane") &&
      LISTS.test(document.body ? document.body.dataset.nav || "" : "") && !!document.querySelector("#inbox-list details, .task-row");   // F4: no pane beside an empty Inbox
  }
  function sizes() {
    var w = parseInt(load("dpW"), 10), h = parseInt(load("dpH"), 10), b = document.body;
    if (w) b.style.setProperty("--dp-w", clampW(w) + "px"); if (h) b.style.setProperty("--dp-h", clampH(h) + "px");
  }
  function paint() {
    if (!document.body) return;
    var o = on(); document.body.classList.toggle("dp-on", o);
    if (!o) { addEverywhere(); return; }
    if (!document.querySelector(".dpane")) {
      var inbox = !!document.getElementById("inbox-list"), p = document.createElement("aside");
      p.className = "dpane"; p.setAttribute("aria-hidden", "true");
      p.innerHTML = '<svg class="icon"><use href="#i-' + (inbox ? "inbox" : "check") + '"/></svg><span></span>';
      p.lastChild.textContent = inbox ? "Pick a recording to see it here." : "Pick a task to see it here.";   // F103: same full stop as Talks and People
      var d = document.createElement("div"); d.className = "dpane-drag"; d.title = "Drag to resize";
      document.body.appendChild(p); document.body.appendChild(d);
    }
    sizes(); split(); addEverywhere();
    document.querySelectorAll(SHEETS.split(", ").map(function (s) { return s + "[open]"; }).join(", ")).forEach(addClose);
  }
  /* N5 Right: the page's top (title, tabs, search row) stays full width and the pane starts level with the first row of
     the list. The pane follows that line as the page scrolls, up to just under the bars that stay at the top. */
  var wides = [];
  function listEl() {
    var l = document.querySelector("#task-list, #inbox-list");
    // F1: the whole section (its "Due" head and "All tasks" link included) sits left of the pane, not just its card
    if (!l) { var t = document.querySelector(".main .task-row"); l = t && (t.closest("section, .section") || t.closest(".card")); }
    return l;
  }
  function split() {
    wides.forEach(function (x) { x.classList.remove("dp-wide"); }); wides = [];
    var b = document.body; if (!b) return;
    if (!b.classList.contains("dp-on") || html.dataset.pane !== "right") { b.style.removeProperty("--dp-top"); return; }
    var l = listEl(), main = document.querySelector(".main"); if (!l || !main || !main.contains(l)) return;
    for (var n = l; n && n !== main; n = n.parentElement)
      for (var p = n.previousElementSibling; p; p = p.previousElementSibling) if (!p.matches("script, template, [hidden]")) { p.classList.add("dp-wide"); wides.push(p); }
    // F40: the pane was measured once, before the page above the list finished growing (fonts, pickers filling in), so
    // it sat ~14 px too high until the first scroll. Re-measure whenever anything above the list changes size.
    if (window.ResizeObserver) { if (ro) ro.disconnect(); else ro = new ResizeObserver(later); wides.forEach(function (x) { ro.observe(x); }); ro.observe(l); }
    place();
  }
  var ro = null;
  function later() { if (!raf) raf = requestAnimationFrame(place); }
  var raf = 0;
  function place() {
    raf = 0; var b = document.body, l = listEl(); if (!b || !l || !b.classList.contains("dp-on") || html.dataset.pane !== "right") return;
    var floor = 0;
    document.querySelectorAll(".dtop, .topbar, .dp-wide").forEach(function (x) {
      var cs = getComputedStyle(x); if (cs.position !== "sticky" && cs.position !== "fixed") return;
      var r = x.getBoundingClientRect(); if (r.height && r.top <= 1 + parseFloat(cs.top || 0)) floor = Math.max(floor, r.bottom);
    });
    b.style.setProperty("--dp-top", Math.round(Math.max(floor, l.getBoundingClientRect().top)) + "px");
  }
  addEventListener("scroll", later, { passive: true });
  addEventListener("resize", later); addEventListener("load", later);
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(later);
  // R4-04: answers from the layout itself, not from the class paint() sets a moment later (in that gap a pop-up fit
  // measured the pane and nudged it to the middle of the screen)
  window.ScribeDp = { on: function () { return !!document.body && on(); }, SHEETS: SHEETS };
  function refit() { if (window.ScribeRefit) window.ScribeRefit(); }
  /* N5: change the layout from the open task or recording itself (the gear sits behind a pop-up): Pop up · Right · Below */
  function group(none) {
    var g = document.createElement("span"); g.className = "dp-layout"; g.setAttribute("role", "group"); g.setAttribute("aria-label", "Layout");
    [["none", none || "Pop up"], ["right", "Right"], ["below", "Below"]].forEach(function (k) {
      var b = document.createElement("button"); b.type = "button"; b.dataset.paneSet = k[0]; b.title = k[1]; b.setAttribute("aria-label", "Layout: " + k[1]);
      b.className = "dp-lay dp-lay-" + k[0] + (html.dataset.pane === k[0] ? " on" : ""); b.setAttribute("aria-pressed", html.dataset.pane === k[0]);   // F102: says which layout is on from the start
      b.innerHTML = "<i></i>"; g.appendChild(b);
    });
    return g;
  }
  function addLayout(body) {
    if (!wide.matches || body.querySelector(".dp-layout")) return;
    var head = body.querySelector(".sh-head, .fx-head"); if (!head) return;
    var x = head.querySelector("[data-fx-close]"); head.insertBefore(group(), x || null);
  }
  /* R4-05 (Brian, 2026-11-25: "I like this button on Today, add it where applicable"): the same Pop up · Right · Below
     switch sits in the empty pane (Inbox, Tasks, Today) and in the Talks / People preview's bar. */
  function addEverywhere() {
    if (!wide.matches) return;
    var p = document.querySelector(".dpane"); if (p && !p.querySelector(".dp-layout")) { p.removeAttribute("aria-hidden"); p.appendChild(group()); }
    var bar = document.querySelector("#rpane .rpane-bar");
    if (bar && !bar.querySelector(".dp-layout")) bar.insertBefore(group("Full page"), bar.querySelector("[data-rpane-close]"));
  }
  document.addEventListener("toggle", function (e) {
    var d = e.target; if (d.open && d.matches && d.matches(SHEETS)) { var b = d.querySelector(":scope > .fx-body"); if (b) addLayout(b); }
  }, true);
  document.addEventListener("click", function (e) {
    var b = e.target.closest && e.target.closest(".dp-lay"); if (!b) return;
    var open = document.querySelector(SHEETS.split(", ").map(function (s) { return s + "[open]"; }).join(", "));
    setTimeout(function () {   // the sheet stays open in its new place
      document.querySelectorAll(".dp-lay").forEach(function (x) { var on = x.dataset.paneSet === html.dataset.pane; x.classList.toggle("on", on); x.setAttribute("aria-pressed", on); });
      if (open) { open.open = true; addClose(open); }
      split(); refit();
    }, 0);
  });
  function addClose(d) {
    var body = d.querySelector(":scope > .fx-body"); if (!body || body.querySelector(":scope > .dp-close")) return;
    addLayout(body);
    // N5 (Brian, 2026-11-25): Right and Below showed two ✕. A sheet that has its own ✕ keeps just that one.
    if (body.querySelector(".sh-head [data-fx-close], .fx-head [data-fx-close]")) return;
    var b = document.createElement("button"); b.type = "button"; b.className = "icon-btn dp-close"; b.setAttribute("aria-label", "Close");
    b.innerHTML = '<svg class="icon"><use href="#i-x"/></svg>'; body.insertBefore(b, body.firstChild);
  }
  document.addEventListener("toggle", function (e) {
    var d = e.target; if (d.open && d.matches && d.matches(SHEETS) && on()) addClose(d);
  }, true);
  document.addEventListener("click", function (e) {
    var c = e.target.closest && e.target.closest(".dp-close");
    if (c) { var d = c.closest("details"); if (d) d.open = false; return; }
    if (e.target.closest && e.target.closest("[data-pane-set]")) setTimeout(function () { paint(); refit(); }, 0);
  });
  document.addEventListener("keydown", function (e) {
    if (e.key !== "Escape" || !on() || document.querySelector("dialog[open], .ctx-menu")) return;
    document.querySelectorAll(SHEETS.split(", ").map(function (s) { return s + "[open]"; }).join(", ")).forEach(function (d) { d.open = false; });
  });
  var drag = null;
  document.addEventListener("pointerdown", function (e) {
    var g = e.target.closest && e.target.closest(".dpane-drag"); if (!g) return;
    e.preventDefault(); drag = { below: html.dataset.pane === "below" }; document.body.classList.add("dp-dragging");
    try { g.setPointerCapture(e.pointerId); } catch (_) {}
  });
  document.addEventListener("pointermove", function (e) {
    if (!drag) return;
    if (drag.below) { drag.v = clampH(innerHeight - e.clientY); document.body.style.setProperty("--dp-h", drag.v + "px"); }
    else { drag.v = clampW(innerWidth - e.clientX); document.body.style.setProperty("--dp-w", drag.v + "px"); }
  });
  ["pointerup", "pointercancel"].forEach(function (ev) { document.addEventListener(ev, function () {
    if (!drag) return; if (drag.v) store(drag.below ? "dpH" : "dpW", String(Math.round(drag.v)));
    drag = null; document.body.classList.remove("dp-dragging"); }); });
  addEventListener("resize", function () { if (on()) sizes(); });
  wide.addEventListener ? wide.addEventListener("change", paint) : wide.addListener(paint);
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", paint); else paint();
  document.addEventListener("htmx:afterSettle", paint);
})();

/* B25 (Brian, 2026-11-25): switching All / Work / Home / School / Other looked like a page reload. It still loads the
   page (swapping pages in place is what made the app lag on 2026-11-22), but the choice lights up at once and the new
   page opens where you were on the old one, so only the list changes. */
(function () {
  var KEY = "area-scroll";
  try {
    var s = JSON.parse(sessionStorage.getItem(KEY) || "null");
    sessionStorage.removeItem(KEY);
    if (s && s.p === location.pathname && !location.hash) {
      var go = function () { scrollTo(0, s.y); };
      if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", go); else go();
    }
  } catch (_) {}
  document.addEventListener("click", function (e) {
    var a = e.target.closest && e.target.closest(".seg-areas a, .area-menu a.menu-item");
    if (!a || e.button || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    try { sessionStorage.setItem(KEY, JSON.stringify({ p: location.pathname, y: scrollY })); } catch (_) {}
    var box = a.parentElement;
    box.querySelectorAll("a").forEach(function (x) { x.classList.toggle("on", x === a); });
  });
})();

/* B31 (Brian, 2026-11-25): a filter box over a long list on the page (Data Ledger, Backups): rows that don't contain
   the words hide as you type. data-rows = the rows' selector. */
document.addEventListener("input", function (e) {
  var box = e.target, sel = box.dataset && box.dataset.rows;
  if (!sel) return;
  var words = box.value.toLowerCase().split(/\s+/).filter(Boolean), shown = 0;
  document.querySelectorAll(sel).forEach(function (r) {
    var t = r.textContent.toLowerCase(), ok = words.every(function (w) { return t.indexOf(w) >= 0; });
    r.hidden = !ok; if (ok) shown++;
  });
  var card = box.closest(".card"), none = card && card.querySelector(".rows-none");
  if (none) none.hidden = shown > 0;
});

/* R4-15 + R4-16 (Brian, 2026-11-25): adding and removing a tag was slow (a whole page load each time) and the tag
   box used the browser's own drop-down (an unstyled ▼) and "Please fill out this field" bubble. Now a tag shows or goes
   at once and saves in the background (the tag row is refreshed from the M14's answer, with Undo for a removal), and the
   suggestions are a list styled like the app's menus. An empty box does nothing. */
(function () {
  function names() { return [].map.call(document.querySelectorAll("#tag-list option"), function (o) { return o.value; }); }
  var list = null, input = null, pick = -1;
  function shut() { if (list) { list.remove(); list = null; } pick = -1; }
  function draw() {
    shut(); if (!input) return;
    var q = input.value.trim().toLowerCase(), have = [].map.call(document.querySelectorAll("#tags .tag-chip a"), function (a) { return a.textContent.replace(/^#/, "").toLowerCase(); });
    var hits = names().filter(function (n) { var l = n.toLowerCase(); return have.indexOf(l) < 0 && (!q || l.indexOf(q) >= 0); }).slice(0, 8);
    if (!hits.length) return;
    list = document.createElement("div"); list.className = "ctx-menu tag-sugg"; list.setAttribute("role", "listbox");
    hits.forEach(function (n) {
      var b = document.createElement("button"); b.type = "button"; b.className = "ctx-item"; b.setAttribute("role", "option"); b.textContent = "#" + n; b.dataset.v = n;
      list.appendChild(b);
    });
    document.body.appendChild(list);
    place();
  }
  // R5-23: the list stays under its box when the page or a pane scrolls or the phone's keyboard opens (it was drawn once)
  function place() {
    if (!list || !input) return;
    var r = input.getBoundingClientRect();
    if (!r.width && !r.height) { shut(); return; }
    list.style.minWidth = r.width + "px";
    list.style.left = Math.max(8, Math.min(r.left, innerWidth - list.offsetWidth - 8)) + "px";
    list.style.top = (r.bottom + list.offsetHeight + 8 > innerHeight ? Math.max(8, r.top - list.offsetHeight - 4) : r.bottom + 4) + "px";
  }
  addEventListener("scroll", function (e) { if (list && !(e.target.nodeType === 1 && list.contains(e.target))) place(); }, { capture: true, passive: true });
  addEventListener("resize", place);
  document.addEventListener("focusin", function (e) { if (e.target.matches && e.target.matches("[data-tag-input]")) { input = e.target; draw(); } });
  document.addEventListener("input", function (e) { if (e.target === input) draw(); });
  document.addEventListener("focusout", function (e) { if (e.target === input) setTimeout(function () { if (document.activeElement !== input) shut(); }, 150); });
  document.addEventListener("keydown", function (e) {
    if (!list || e.target !== input) return;
    var items = list.querySelectorAll(".ctx-item");
    if (e.key === "ArrowDown" || e.key === "ArrowUp") { e.preventDefault(); pick = (pick + (e.key === "ArrowDown" ? 1 : -1) + items.length) % items.length;
      items.forEach(function (x, i) { x.classList.toggle("on", i === pick); }); }
    else if (e.key === "Enter" && pick >= 0) { e.preventDefault(); input.value = items[pick].dataset.v; shut(); input.form.requestSubmit(); }
    else if (e.key === "Escape") shut();
  });
  document.addEventListener("pointerdown", function (e) {
    var b = list && e.target.closest && e.target.closest(".tag-sugg .ctx-item"); if (!b) return;
    e.preventDefault(); input.value = b.dataset.v; shut(); input.form.requestSubmit();
  });
  function chip(name) {
    var s = document.createElement("span"); s.className = "tag-chip tag-pending"; s.title = "Your tag";
    var a = document.createElement("a"); a.textContent = "#" + name; s.appendChild(a); return s;
  }
  function refresh(html, again) {
    var d = new DOMParser().parseFromString(html, "text/html"), was = document.querySelector("#tags");
    var open = was && was.classList.contains("pf-open");
    ["#tags", "#undo-bar"].forEach(function (sel) { var a = document.querySelector(sel), b = d.querySelector(sel); if (a && b) a.replaceWith(b); });
    var t = document.querySelector("#tags"); if (!t) return;
    if (window.htmx) htmx.process(t);
    if (open) t.classList.add("pf-open");
    if (again) { var ta = t.querySelector(".tag-add"); if (ta) { t.dataset.autoOpen = "1"; ta.open = true; var i = ta.querySelector("[data-tag-input]"); if (i) i.focus(); } }
    dispatchEvent(new Event("resize"));   // the pill row folds again ("+N" / Hide) for the new set of tags
  }
  document.addEventListener("submit", function (e) {
    var f = e.target; if (!f.closest || !f.closest("#tags") || !/\/tags(\/\d+\/(remove|keep))?$/.test(f.getAttribute("action") || "")) return;
    e.preventDefault();
    var body = new URLSearchParams(new FormData(f)).toString();   // read before the box is emptied
    var add = /\/tags$/.test(f.getAttribute("action")), inp = f.querySelector("[name=name]");
    if (add) {
      var v = inp.value.trim(); if (!v) { inp.focus(); return; }
      var at = f.closest(".tag-add"); v.split(",").forEach(function (n) { n = n.trim(); if (n) at.before(chip(n)); });
      inp.value = ""; shut();
    } else if (/remove$/.test(f.getAttribute("action"))) { var c = f.closest(".tag-chip"); if (c) c.hidden = true; }
    else { var k = f.closest(".tag-chip"); if (k) k.classList.remove("by-ai"); f.hidden = true; }
    fetch(f.action, { method: "POST", body: body, credentials: "same-origin", headers: { "Content-Type": "application/x-www-form-urlencoded" } }).then(function (r) {
      if (!r.ok) throw new Error(r.status); return r.text();
    }).then(function (html) { refresh(html, add); }).catch(function () { location.reload(); });
  }, true);
})();

/* R4-17 (Brian, 2026-11-25: "make normal dropdowns look like the right-click menu, for all dropdowns"). On a computer a
   click on any drop-down opens the app's own menu (same look as right-click, a ✓ on the current choice) instead of the
   browser's grey list. Phones keep their own picker, which Brian likes. The keyboard keeps the browser's list (Tab,
   arrows, Space), so nothing gets harder to reach. */
(function () {
  var fine = matchMedia("(pointer: fine)"), menu = null, sel = null;
  function close() { if (menu) { menu.remove(); menu = null; } if (sel) { sel.classList.remove("sel-open"); sel = null; } }
  function open(s) {
    close(); sel = s; s.classList.add("sel-open");
    menu = document.createElement("div"); menu.className = "ctx-menu sel-menu"; menu.setAttribute("role", "listbox");
    var label = s.getAttribute("aria-label") || (s.labels && s.labels[0] && s.labels[0].textContent.trim()) || ""; if (label) menu.setAttribute("aria-label", label);
    [].forEach.call(s.options, function (o, i) {
      if (o.hidden) return;
      if (o.parentElement && o.parentElement.tagName === "OPTGROUP" && o === o.parentElement.firstElementChild) {
        var h = document.createElement("div"); h.className = "menu-label"; h.textContent = o.parentElement.label; menu.appendChild(h);
      }
      var b = document.createElement("button"); b.type = "button"; b.className = "ctx-item"; b.setAttribute("role", "option");
      b.innerHTML = '<span class="ctx-ic">' + (o.selected ? '<svg class="icon" aria-hidden="true"><use href="#i-check"/></svg>' : "") + "</span><span></span>";
      b.lastChild.textContent = o.textContent.trim(); b.disabled = o.disabled;
      if (o.selected) b.setAttribute("aria-checked", "true");
      b.addEventListener("click", function () {
        var same = s.selectedIndex === i; close();
        if (same) return;
        s.selectedIndex = i;
        s.dispatchEvent(new Event("input", { bubbles: true })); s.dispatchEvent(new Event("change", { bubbles: true }));
      });
      menu.appendChild(b);
    });
    document.body.appendChild(menu);
    var r = s.getBoundingClientRect(), w = Math.max(menu.offsetWidth, r.width), h = menu.offsetHeight;
    menu.style.minWidth = r.width + "px";
    menu.style.left = Math.max(8, Math.min(r.left, innerWidth - w - 8)) + "px";
    menu.style.top = (r.bottom + h + 8 > innerHeight && r.top - h - 4 > 8 ? r.top - h - 4 : Math.min(r.bottom + 4, Math.max(8, innerHeight - h - 8))) + "px";
    var on = menu.querySelector('[aria-checked="true"]') || menu.querySelector(".ctx-item:not(:disabled)"); if (on) on.focus({ preventScroll: true });
  }
  document.addEventListener("mousedown", function (e) {
    if (!fine.matches || e.button !== 0) return;
    var s = e.target.closest && e.target.closest("select");
    // the person picker (data-people, or a "Someone new" option) has its own sheet (app.js), so it's left to that
    if (!s || e.defaultPrevented || s.multiple || s.size > 1 || s.disabled || s.hasAttribute("data-native") || s.closest("dialog") ||
        s.hasAttribute("data-people") || s.querySelector('option[value="__new"]')) {
      if (menu && !(e.target.closest && e.target.closest(".sel-menu"))) close();
      return;
    }
    e.preventDefault(); s.focus({ preventScroll: true });
    if (sel === s) { close(); return; }
    open(s);
  }, true);
  document.addEventListener("keydown", function (e) {
    if (!menu) return;
    if (e.key === "Escape") { e.preventDefault(); var s = sel; close(); if (s) s.focus(); return; }
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      var items = [].slice.call(menu.querySelectorAll(".ctx-item:not(:disabled)")), i = items.indexOf(document.activeElement);
      var n = items[(i + (e.key === "ArrowDown" ? 1 : -1) + items.length) % items.length]; if (n) n.focus();
    }
  });
  // R5-23: any scroll but the menu's own closes it, a pane's too (on a computer the list, the reading pane and settings
  // scroll on their own, and a fixed menu stayed where it was while its drop-down scrolled away)
  addEventListener("scroll", function (e) { if (menu && !(e.target.nodeType === 1 && menu.contains(e.target))) close(); }, { capture: true, passive: true });
  addEventListener("resize", close);
  document.addEventListener("htmx:beforeSwap", close);
})();
