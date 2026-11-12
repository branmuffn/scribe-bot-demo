// Scribe Bot page helpers (vanilla JS, no framework). Event delegation, so content swapped in by htmx just works.
//   tap a timestamp to play · highlight the line being played · "line N" links open the transcript
//   keep scroll position on boosted form posts · keyboard shortcuts · copy-to-clipboard · in-app voice notes
(function () {
  var lastUserScroll = 0;
  ["wheel", "touchmove"].forEach(function (ev) {
    window.addEventListener(ev, function () { lastUserScroll = Date.now(); }, { passive: true });
  });
  function typing(el) { return el && (el.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName)); }
  function player() { return document.getElementById("player"); }
  function lines() { return Array.prototype.slice.call(document.querySelectorAll(".line[data-start]")); }
  function openTranscript() { var d = document.getElementById("transcript"); if (d) d.open = true; }

  // Theme: Light, Dark or System (Settings › Appearance).
  // Cookie (server renders it, no flash) + localStorage (survives cookie clears).
  var THEMES = ["system", "light", "dark"];
  function applyTheme(t) {
    if (t === "system") document.documentElement.removeAttribute("data-theme");
    else document.documentElement.setAttribute("data-theme", t);
    document.cookie = "theme=" + t + "; path=/; max-age=31536000; samesite=lax";
    try { localStorage.setItem("theme", t); } catch (_) {}
    // Phone status bar: one fixed color for Light/Dark, the light+dark pair for System.
    document.querySelectorAll('meta[name="theme-color"]').forEach(function (m) { m.remove(); });
    (t === "system" ? [["#fafaf9", "(prefers-color-scheme: light)"], ["#1c1917", "(prefers-color-scheme: dark)"]]
                    : [[t === "dark" ? "#1c1917" : "#fafaf9", ""]]).forEach(function (c) {
      var m = document.createElement("meta");
      m.name = "theme-color"; m.content = c[0];
      if (c[1]) m.media = c[1];
      document.head.appendChild(m);
    });
    document.querySelectorAll("[data-theme-set]").forEach(function (b) {   // Settings → Appearance
      var on = b.dataset.themeSet === t;
      b.classList.toggle("on", on);
      b.setAttribute("aria-pressed", on ? "true" : "false");
    });
  }
  try {  // a cookie that was cleared but localStorage kept: restore it
    var saved = localStorage.getItem("theme");
    if (saved && THEMES.indexOf(saved) >= 0 && document.cookie.indexOf("theme=" + saved) < 0) applyTheme(saved);
  } catch (_) {}

  // Tap a timestamp: play from that moment. "line N" links: open the collapsed transcript first.
  document.addEventListener("click", function (e) {
    var ts = e.target.closest("[data-theme-set]");
    if (ts) { applyTheme(ts.dataset.themeSet); return; }
    var seek = e.target.closest("[data-seek]");
    // Tap anywhere on a transcript line plays from it (not only its time); its ⋯ menu, forms and an open edit don't.
    // On the text itself it goes to about the tapped word: the line's time span, split by where in the text you tapped.
    var ln = !seek && e.target.closest(".lines .line");
    if (ln && !e.target.closest(".line-edit, form, a, button, input, textarea, select")) seek = ln.querySelector("[data-seek]");
    if (seek) {
      var a = player(), at = parseFloat(seek.dataset.seek);
      var txt = ln && e.target.closest("[data-edit-url]");
      if (txt && txt.textContent.length > 1) {
        var r = document.caretRangeFromPoint ? document.caretRangeFromPoint(e.clientX, e.clientY) : null;
        var p = !r && document.caretPositionFromPoint ? document.caretPositionFromPoint(e.clientX, e.clientY) : null;
        var off = r ? r.startOffset : p ? p.offset : 0, end = parseFloat(ln.dataset.end) || at;
        if (r ? txt.contains(r.startContainer) : p && txt.contains(p.offsetNode))
          at += (end - at) * Math.min(1, off / txt.textContent.length);
      }
      if (a) { a.currentTime = Math.max(0, at - 0.3); a.play(); }
      return;
    }
    if (e.target.closest('a[href^="#L"]')) openTranscript();
    var cancel = e.target.closest("[data-cancel]");
    if (cancel && cancel.closest(".confirm-slot")) { e.preventDefault(); cancel.closest(".confirm-slot").innerHTML = ""; return; }
    var menu = e.target.closest(".row-menu");   // one menu open at a time; a tap outside closes it
    document.querySelectorAll(".row-menu[open]").forEach(function (d) { if (d !== menu) d.open = false; });
    if (e.target.closest("[data-dismiss-undo]")) { document.getElementById("undo-bar").hidden = true; return; }
    var copy = e.target.closest("[data-copy]");
    if (copy) {
      var src = document.querySelector(copy.dataset.copy);
      if (!src) return;
      var text = src.value !== undefined ? src.value : src.textContent;
      var done = function () { var t = copy.textContent; copy.textContent = "Copied ✓"; setTimeout(function () { copy.textContent = t; }, 1600); };
      if (navigator.clipboard && window.isSecureContext) navigator.clipboard.writeText(text.trim()).then(done);
      else { src.hidden = false; src.select(); document.execCommand("copy"); src.hidden = true; done(); }
    }
  });

  // Highlight the line being played; follow it unless Brian scrolled in the last 4 s. (Media events don't bubble: capture.)
  // The line being spoken wins; in a pause the line just finished stays lit for TAIL s, so it doesn't flicker. While
  // playing it checks every 0.1 s (timeupdate alone comes every ~0.25 s), with a small LEAD: word times mark a word a
  // moment after it starts. (2026-10-20: it ran ~1 s late, because the finished line kept the light for its whole TAIL.)
  var current = null, ticker = null, LEAD = 0.2, TAIL = 0.75;
  function highlight(a) {
    if (!document.body.contains(a)) { clearInterval(ticker); ticker = null; return; }   // left the page while playing
    var t = a.currentTime + LEAD, hit = null, tail = null;
    lines().some(function (l) {   // lines are in time order
      var s = parseFloat(l.dataset.start), end = parseFloat(l.dataset.end);
      if (s > t) return true;
      if (t < end) hit = l;
      else if (t < end + TAIL) tail = l;
      return false;
    });
    hit = hit || tail;
    if (hit === current) return;
    if (current) current.classList.remove("playing");
    current = hit;
    if (!hit) return;
    hit.classList.add("playing");
    var d = document.getElementById("transcript");
    if (d && d.open && Date.now() - lastUserScroll > 4000) {
      var reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      hit.scrollIntoView({ block: "center", behavior: reduce ? "auto" : "smooth" });
    }
  }
  document.addEventListener("timeupdate", function (e) { if (e.target.id === "player") highlight(e.target); }, true);
  document.addEventListener("play", function (e) {
    if (e.target.id !== "player") return;
    clearInterval(ticker);
    ticker = setInterval(function () { highlight(e.target); }, 100);
  }, true);
  ["pause", "ended"].forEach(function (ev) {
    document.addEventListener(ev, function (e) { if (e.target.id === "player") { clearInterval(ticker); ticker = null; } }, true);
  });

  // Fallback for full-page form posts: come back to the same scroll position instead of the top.
  document.addEventListener("submit", function (e) {
    if (!e.target.hasAttribute("hx-target")) {
      try { sessionStorage.setItem("scroll", JSON.stringify({ path: location.pathname, y: window.scrollY, at: Date.now() })); } catch (_) {}
    }
  });
  function restoreScroll() {
    try {
      var s = JSON.parse(sessionStorage.getItem("scroll") || "null");
      sessionStorage.removeItem("scroll");
      if (s && s.path === location.pathname && Date.now() - s.at < 15000) window.scrollTo(0, s.y);
    } catch (_) {}
  }

  // Keyboard shortcuts (desktop): space play/pause · j/k next/previous line · / search · ? help
  var cursor = -1;
  var GO = { h: "/", c: "/conversations", t: "/tasks", p: "/people", r: "/review", s: "/settings" }, goAt = 0;
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") {   // close open edit panels (works while typing in them too)
      var open = document.querySelectorAll(".line-edit[open], details.edit[open], .speaker-chip[open], .row-menu[open]");
      open.forEach(function (d) { d.open = false; });
      if (open.length) { var s = open[0].querySelector("summary"); if (s) s.focus(); }
      return;
    }
    if (e.metaKey || e.ctrlKey || e.altKey || typing(document.activeElement)) return;
    if (Date.now() - goAt < 1500 && GO[e.key]) { goAt = 0; location.href = GO[e.key]; return; }   // "g" then a letter
    goAt = 0;
    var a = player();
    if (e.key === " " && a && !(document.activeElement && document.activeElement.tagName === "BUTTON")) {
      e.preventDefault();
      if (a.paused) a.play(); else a.pause();
    } else if ((e.key === "j" || e.key === "k") && lines().length) {
      var ls = lines();
      openTranscript();
      if (current && cursor < 0) cursor = ls.indexOf(current);
      cursor = Math.max(0, Math.min(ls.length - 1, cursor + (e.key === "k" ? 1 : -1)));   // Brian: k = next, j = previous
      ls.forEach(function (l) { l.classList.remove("cursor"); });
      ls[cursor].classList.add("cursor");
      ls[cursor].scrollIntoView({ block: "center" });
      if (a) a.currentTime = parseFloat(ls[cursor].dataset.start);
    } else if (e.key === "/") {
      e.preventDefault();
      var q = document.querySelector('input[name="q"]');
      if (q) q.focus(); else location.href = "/search";
    } else if (e.key === "g") {
      goAt = Date.now();
    } else if (e.key === "e" && cursor >= 0 && lines()[cursor]) {
      var ed = lines()[cursor].querySelector(".line-edit");
      if (ed) { e.preventDefault(); ed.open = true; var f = ed.querySelector("select, textarea"); if (f) f.focus(); }
    } else if (e.key === "c") {
      var cp = document.querySelector('[data-copy="#recap"]');
      if (cp) cp.click();
    } else if (e.key === "?") {
      var dlg = document.getElementById("shortcuts");
      if (dlg) { if (dlg.open) dlg.close(); else dlg.showModal(); }
    }
  });

  // Search: tapping the box after a search drops your recent searches down (typing narrows them); a tap outside, Esc or
  // searching closes it. The list is server-rendered in search.html; this only shows, narrows and hides it.
  function showRecent(input, typed) {
    var pop = input.closest(".search-field").querySelector(".recent-pop"), now = input.value.trim().toLowerCase(), shown = 0;
    if (!pop) return;
    pop.querySelectorAll("a[data-q]").forEach(function (a) {
      var q = a.dataset.q.toLowerCase(), on = q !== now && (!typed || q.indexOf(now) >= 0);
      a.hidden = !on;
      shown += on ? 1 : 0;
    });
    pop.hidden = !shown;
    input.setAttribute("aria-expanded", shown ? "true" : "false");
  }
  function hideRecent() {
    document.querySelectorAll(".recent-pop").forEach(function (p) { p.hidden = true; });
    document.querySelectorAll("input[data-recent]").forEach(function (i) { i.setAttribute("aria-expanded", "false"); });
  }
  document.addEventListener("focusin", function (e) { if (e.target.matches && e.target.matches("input[data-recent]")) showRecent(e.target, false); });
  document.addEventListener("input", function (e) { if (e.target.matches && e.target.matches("input[data-recent]")) showRecent(e.target, true); });
  document.addEventListener("pointerdown", function (e) { if (!(e.target.closest && e.target.closest(".search-field"))) hideRecent(); });
  document.addEventListener("keydown", function (e) { if (e.key === "Escape") hideRecent(); });
  document.addEventListener("submit", hideRecent);

  // 5-step tour: Record, Approve, Read, Fix, Find (the same five steps as the Today help card). Progress lives in this
  // browser only (localStorage), so it survives the page changes between steps.
  // Status history charts: point or tap to see the time and value of the nearest reading (server-drawn SVG, no library).
  function chartHover(e) {
    var card = e.target.closest(".chart[data-points]");
    if (!card || !card.open || e.target.closest("summary")) return;   // folded, or tapping the title to fold it
    var svg = card.querySelector("svg"), pts = JSON.parse(card.dataset.points || "[]");
    if (!pts.length) return;
    var r = svg.getBoundingClientRect(), vb = svg.viewBox.baseVal;
    var x = (e.clientX - r.left) * vb.width / r.width, best = pts[0];
    pts.forEach(function (p) { if (Math.abs(p[0] - x) < Math.abs(best[0] - x)) best = p; });
    var hair = svg.querySelector(".hair"), dot = svg.querySelector(".hdot"), tip = card.querySelector(".chart-tip");
    hair.setAttribute("x1", best[0]); hair.setAttribute("x2", best[0]); dot.setAttribute("cx", best[0]); dot.setAttribute("cy", best[1]);
    hair.hidden = dot.hidden = tip.hidden = false;
    tip.textContent = best[2] + " · " + best[3];
  }
  document.addEventListener("pointermove", chartHover);
  // An M14 tile opens its history chart (folded by default) and scrolls to it.
  document.addEventListener("click", function (e) {
    var a = e.target.closest && e.target.closest('a[href^="#chart-"]'), d = a && document.getElementById(a.getAttribute("href").slice(1));
    if (d) { d.open = true; }
  });
  document.addEventListener("pointerdown", chartHover);
  document.addEventListener("pointerleave", function (e) {
    var card = e.target.closest && e.target.closest(".chart[data-points]");
    if (card) card.querySelectorAll(".hair, .hdot, .chart-tip").forEach(function (el) { el.hidden = true; });
  }, true);

  var TOUR = [
    { path: "/", spot: ".page-head", title: "1. Record",
      text: "Record in Google Recorder as usual. The recording syncs to the M14 in seconds and shows up here on Today." },
    { path: "/", spot: "#needs-you|.page-head", title: "2. Approve",
      text: "New recordings wait here under Needs you. Each shows how long it takes and what it costs. Pick Transcribe + AI for a summary and tasks, or Transcript Only (No AI) to keep it fully private. The clock reminds you later instead." },
    { path: "conv", spot: "#check-strip", title: "3. Read",
      text: "Each conversation opens with this strip: what you owe, what others owe you, and anything worth a second look." },
    { path: "conv", spot: "#sec-owe .item|#speakers", title: "4. Fix",
      text: "Tap Done when a task is finished. The ⋯ button has Change person, Edit and Remove. Tap a speaker's name to rename them. Every change can be undone." },
    { path: "/search", spot: '.main input[name="q"]', title: "5. Find",
      text: "Search finds any word in any transcript, task, decision or person, one card per conversation, with filters and your recent searches. Everything else is under ⋯ and ⚙. That's the tour!" },
  ];
  function tourState() { try { return JSON.parse(localStorage.getItem("tour") || "null"); } catch (_) { return null; } }
  function setTour(st) { try { if (st) localStorage.setItem("tour", JSON.stringify(st)); else localStorage.removeItem("tour"); } catch (_) {} }
  function tourPath(step, st) { return step.path === "conv" ? (st.conv ? "/conversations/" + st.conv : "/conversations") : step.path; }
  function showTour() {
    var st = tourState(), card = document.getElementById("tour");
    document.querySelectorAll(".tour-spot").forEach(function (el) { el.classList.remove("tour-spot"); });
    if (!card) return;
    if (!st) { card.hidden = true; return; }
    var step = TOUR[st.i];
    if (location.pathname !== tourPath(step, st)) { location.href = tourPath(step, st); return; }
    card.hidden = false;
    document.getElementById("tour-count").textContent = "Step " + (st.i + 1) + " of " + TOUR.length;
    document.getElementById("tour-title").textContent = step.title;
    document.getElementById("tour-text").textContent = step.text;
    card.querySelector('[data-tour="back"]').hidden = st.i === 0;
    card.querySelector('[data-tour="next"]').textContent = st.i === TOUR.length - 1 ? "Finish" : "Next";
    var spot = null;   // "a|b": the first of these that's on the page
    step.spot.split("|").some(function (sel) { spot = document.querySelector(sel); return spot; });
    if (spot) { spot.classList.add("tour-spot"); spot.scrollIntoView({ block: "start", behavior: "smooth" }); }   // top of the screen: the card sits at the bottom
  }
  document.addEventListener("click", function (e) {
    var start = e.target.closest("[data-tour-start]");
    if (start) { setTour({ i: 0, conv: start.dataset.tourStart || null }); showTour(); return; }
    var b = e.target.closest("[data-tour]");
    if (!b) return;
    var st = tourState();
    if (!st) return;
    if (b.dataset.tour === "skip" || (b.dataset.tour === "next" && st.i === TOUR.length - 1)) setTour(null);
    else { st.i = Math.max(0, st.i + (b.dataset.tour === "next" ? 1 : -1)); setTour(st); }
    showTour();
  });
  document.addEventListener("DOMContentLoaded", showTour);
  document.addEventListener("htmx:afterSettle", function (e) { if (e.detail.boosted) showTour(); });

  // Today's Processing card is replaced every 15 s: an opened "Each step" list stays open.
  var openSteps = [];
  document.addEventListener("htmx:beforeSwap", function (e) {
    if (e.detail.target.id !== "progress-live") return;
    openSteps = [].map.call(e.detail.target.querySelectorAll("details.steps[open]"), function (d) { return d.dataset.job; });
  });
  document.addEventListener("htmx:afterSettle", function () {
    openSteps.forEach(function (id) { var d = document.querySelector('#progress-live details.steps[data-job="' + id + '"]'); if (d) d.open = true; });
    openSteps = [];
  });

  // Swipe a row (phone): left runs its form[data-swipe=left] (Remove / Dismiss), right its form[data-swipe=right]
  // (Remind me later). The buttons stay for desktop and for anyone who prefers them.
  var sw = null;
  document.addEventListener("touchstart", function (e) {
    var row = e.target.closest && e.target.closest(".swipeable");
    if (!row || e.touches.length !== 1 || e.target.closest("input,textarea,select,details[open]")) { sw = null; return; }
    sw = { row: row, x: e.touches[0].clientX, y: e.touches[0].clientY, dx: 0, on: false };
  }, { passive: true });
  document.addEventListener("touchmove", function (e) {
    if (!sw) return;
    var dx = e.touches[0].clientX - sw.x, dy = e.touches[0].clientY - sw.y;
    if (!sw.on && Math.abs(dy) > Math.abs(dx)) { sw = null; return; }   // scrolling, not swiping
    if (Math.abs(dx) > 10) sw.on = true;
    if (!sw.on) return;
    var f = swipeForm(sw.row, dx < 0 ? "left" : "right");
    sw.dx = f ? dx : dx / 4;   // no action that way: a small rubber-band only
    sw.row.style.transform = "translateX(" + sw.dx + "px)";
    sw.row.dataset.swipeHint = f ? (f.dataset.swipeLabel || "") : "";
    sw.row.classList.toggle("swipe-left", dx < 0); sw.row.classList.toggle("swipe-right", dx > 0);
  }, { passive: true });
  document.addEventListener("touchend", function () {
    if (!sw || !sw.on) { sw = null; return; }
    var row = sw.row, dx = sw.dx, f = swipeForm(row, dx < 0 ? "left" : "right");
    sw = null;
    if (f && Math.abs(dx) > row.offsetWidth * 0.35) {
      row.style.transform = "translateX(" + (dx < 0 ? "-" : "") + "110%)"; row.style.opacity = "0";
      setTimeout(function () { f.requestSubmit(); }, 150);
    } else { row.style.transform = ""; row.classList.remove("swipe-left", "swipe-right"); }
  });
  function swipeForm(row, dir) { return row.querySelector('form[data-swipe="' + dir + '"], form[data-swipe="both"]'); }

  // Long lists: a card with data-limit="5" shows its first 5 rows and a "Show all N" button.
  // Folded sections with data-remember stay open or closed the way Brian left them (this browser only).
  function tidy() {
    document.querySelectorAll(".card[data-limit]").forEach(function (c) {
      if (c.dataset.showAll) return;
      var rows = Array.prototype.filter.call(c.children, function (x) { return x.classList.contains("item"); });
      var n = +c.dataset.limit, old = c.querySelector(":scope > .show-all");
      if (old) old.remove();
      rows.forEach(function (r, i) { r.hidden = i >= n; });
      if (rows.length > n) {
        var b = document.createElement("button"); b.type = "button"; b.className = "item show-more show-all";
        b.textContent = "Show all " + rows.length;
        b.onclick = function () { c.dataset.showAll = "1"; rows.forEach(function (r) { r.hidden = false; }); b.remove(); };
        c.appendChild(b);
      }
    });
    document.querySelectorAll("details[data-remember]").forEach(function (d) {
      try { var v = localStorage.getItem("fold:" + d.dataset.remember); if (v !== null) d.open = v === "1"; } catch (e) {}
    });
  }
  document.addEventListener("toggle", function (e) {
    var d = e.target;
    if (d.dataset && d.dataset.remember) { try { localStorage.setItem("fold:" + d.dataset.remember, d.open ? "1" : "0"); } catch (x) {} }
  }, true);
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", tidy); else tidy();
  document.addEventListener("htmx:afterSettle", tidy);

  // The Undo bar fades after 6 s (Undo still works from a page reload for 10 min via the server).
  var undoTimer;
  function undoAutoHide() {
    var b = document.getElementById("undo-bar");
    clearTimeout(undoTimer);
    if (b && !b.hidden && !b.classList.contains("net-note")) undoTimer = setTimeout(function () { b.hidden = true; }, 6000);
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", undoAutoHide); else undoAutoHide();
  document.addEventListener("htmx:afterSettle", undoAutoHide);

  // Edit boxes: grow to fit their text; Enter saves single-line ones (Shift+Enter for a new line).
  function grow(t) { if (t.tagName === "TEXTAREA" && !CSS.supports("field-sizing", "content")) { t.style.height = "auto"; t.style.height = t.scrollHeight + 2 + "px"; } }
  document.addEventListener("input", function (e) { grow(e.target); });
  document.addEventListener("toggle", function (e) { if (e.target.open) e.target.querySelectorAll("textarea").forEach(grow); }, true);
  document.addEventListener("keydown", function (e) {
    var t = e.target;
    if (e.key === "Enter" && !e.shiftKey && t.matches && t.matches("textarea[data-enter-submits]")) { e.preventDefault(); t.form.requestSubmit(); }
  });
  // Pickers that apply at once (Conversations › Sort): choosing an option submits its form.
  document.addEventListener("change", function (e) { if (e.target.matches && e.target.matches("[data-submit-on-change]")) e.target.form.requestSubmit(); });

  // Edit in place: ✎ or double-tap turns the text into an edit box right where it is. Enter (single-line) or Save posts it
  // like the other in-place actions; Esc or Cancel puts the text back.
  function startEdit(el) {
    if (!el || el.hidden) return;
    var f = document.createElement("form");
    f.method = "post"; f.action = el.dataset.editUrl; f.className = "inline-edit";
    if (el.dataset.editTarget) {
      var t = "#" + el.dataset.editTarget;
      f.setAttribute("hx-post", el.dataset.editUrl); f.setAttribute("hx-target", t); f.setAttribute("hx-select", t);
      f.setAttribute("hx-swap", "outerHTML"); f.setAttribute("hx-select-oob", "#undo-bar");
    }
    var extra = JSON.parse(el.dataset.editExtra || "{}");
    Object.keys(extra).forEach(function (k) {
      var h = document.createElement("input"); h.type = "hidden"; h.name = k; h.value = extra[k]; f.appendChild(h);
    });
    var ta = document.createElement("textarea");
    ta.name = el.dataset.editField; ta.value = el.dataset.editValue || ""; ta.required = "editRequired" in el.dataset;
    ta.setAttribute("aria-label", "Edit " + el.dataset.editLabel); ta.rows = "editSingle" in el.dataset ? 1 : 3;
    if ("editSingle" in el.dataset) ta.setAttribute("data-enter-submits", "");
    f.appendChild(ta);
    var row = document.createElement("div"); row.className = "row";
    row.innerHTML = '<span class="meta">Saves when you click away · Esc cancels</span><button type="button" class="btn btn-ghost" data-edit-cancel>Cancel</button>';
    f.appendChild(row);
    var pen = el.parentElement.querySelector(":scope > [data-edit-pencil]");
    el.hidden = true; if (pen) pen.hidden = true;
    el.after(f);
    if (window.htmx) htmx.process(f);
    grow(ta); ta.focus(); ta.setSelectionRange(ta.value.length, ta.value.length);
  }
  function cancelEdit(f, away) {
    var el = f.previousElementSibling, pen = el && el.parentElement.querySelector(":scope > [data-edit-pencil]");
    if (el) el.hidden = false; if (pen) pen.hidden = false;
    f.remove(); if (el && !away) (pen || el).focus && (pen || el).focus();   // clicked away: leave focus where it went
  }
  document.addEventListener("click", function (e) {
    var pen = e.target.closest("[data-edit-pencil]");
    if (pen) { startEdit(pen.parentElement.querySelector(":scope > [data-edit-url]")); return; }
    var c = e.target.closest("[data-edit-cancel]");
    if (c) cancelEdit(c.closest("form"));
  });
  document.addEventListener("dblclick", function (e) {
    var el = e.target.closest("[data-edit-url]");
    if (el && !e.target.closest("a, button")) { e.preventDefault(); startEdit(el); }
  });
  // Click (or tap) away = save (Brian 2026-10-22: no Save button). Unchanged or emptied text just closes.
  document.addEventListener("focusout", function (e) {
    var f = e.target.closest && e.target.closest("form.inline-edit");
    if (!f || (e.relatedTarget && f.contains(e.relatedTarget))) return;
    setTimeout(function () {
      if (!f.isConnected || f.contains(document.activeElement)) return;
      var ta = f.querySelector("textarea"), el = f.previousElementSibling;
      var same = ta.value.trim() === (el && el.dataset.editValue || "").trim();
      if (same || (ta.required && !ta.value.trim())) cancelEdit(f, true); else f.requestSubmit();
    }, 120);
  });
  document.addEventListener("keydown", function (e) {
    var f = e.key === "Escape" && e.target.closest && e.target.closest("form.inline-edit");
    if (f) { e.stopImmediatePropagation(); cancelEdit(f); }
  }, true);

  // "Change person" pickers fill their list from the page's single #people-options when first opened.
  document.addEventListener("toggle", function (e) {
    if (!e.target.open) return;
    var tpl = document.getElementById("people-options");
    if (!tpl) return;
    e.target.querySelectorAll("select[data-people]:not([data-filled])").forEach(function (sel) {
      sel.appendChild(tpl.content.cloneNode(true));
      sel.value = sel.dataset.selected || "";
      sel.dataset.filled = "1";
    });
  }, true);   // toggle doesn't bubble: listen in the capture phase

  function bindRecorder(root) {
    // Voice note: record in the browser, upload as a recording.
    var rec = root.querySelector("#record");
    if (!rec || !window.MediaRecorder || rec.dataset.bound) return;
    rec.dataset.bound = "1";
    var mr = null, chunks = [];
    rec.hidden = false;
    rec.addEventListener("click", function () {
      if (mr && mr.state === "recording") { mr.stop(); return; }
      navigator.mediaDevices.getUserMedia({ audio: true }).then(function (stream) {
        chunks = [];
        mr = new MediaRecorder(stream);
        mr.ondataavailable = function (e) { chunks.push(e.data); };
        mr.onstop = function () {
          stream.getTracks().forEach(function (t) { t.stop(); });
          rec.textContent = "Uploading…";
          var f = new FormData();
          var stamp = new Date().toISOString().slice(0, 16).replace("T", " ").replace(":", "_");
          f.append("file", new Blob(chunks, { type: "audio/webm" }), "Voice note " + stamp + ".webm");
          fetch("/capture/upload", { method: "POST", body: f }).then(function () { location.href = "/capture?saved=audio"; });
        };
        mr.start();
        rec.textContent = "■ Stop and save";
      });
    });
  }

  // A page or action that fails is never a silent tap (2026-10-20: a server error made the app look frozen). A page link
  // that hits a server error loads the normal way instead, so the page (or the error) shows; anything else gets a note.
  function netNote(text) {
    var n = document.getElementById("net-note");
    if (!n) {
      n = document.createElement("div"); n.id = "net-note"; n.className = "undo-bar net-note"; n.setAttribute("role", "alert");
      document.body.appendChild(n);
    }
    n.textContent = text; n.hidden = false;
    clearTimeout(netNote.t); netNote.t = setTimeout(function () { n.hidden = true; }, 8000);
  }
  document.addEventListener("htmx:responseError", function (e) {
    var d = e.detail, msg = "";
    if (d.boosted && d.requestConfig && d.requestConfig.verb === "get") { location.href = d.pathInfo.requestPath; return; }
    try { msg = JSON.parse(d.xhr.responseText).detail; } catch (_) {}
    netNote(typeof msg === "string" && msg ? msg : "That didn't work (error " + d.xhr.status + "). Try again.");
  });
  // After a deploy, a window left open kept running the page code it first loaded (htmx swaps only the page body), so
  // fixes didn't reach it (2026-10-22: tapping transcript text still did nothing on the desktop). Every response says
  // which page code the M14 serves; when it's newer, the page change loads in full instead (new CSS and JS).
  var loadedV = (/[?&]v=(\w+)/.exec((document.querySelector('script[src*="/static/app.js"]') || {}).src || "") || [])[1];
  document.addEventListener("htmx:beforeSwap", function (e) {
    var d = e.detail, v = d.xhr && d.xhr.getResponseHeader("X-Asset-V"), url = d.xhr && d.xhr.responseURL;
    if (!d.boosted || !v || !loadedV || v === loadedV || !url) return;
    if (d.requestConfig && d.requestConfig.verb !== "get" && new URL(url).pathname === d.pathInfo.requestPath) return;   // a post shown in place
    d.shouldSwap = false;
    location.href = url;
  });
  document.addEventListener("htmx:sendError", function () { netNote("Can't reach the M14. Check Tailscale is on, then try again."); });
  document.addEventListener("htmx:timeout", function () { netNote("The M14 is slow to answer. Try again in a moment."); });
  if (window.htmx) htmx.config.timeout = 30000;

  function onPage() {
    bindRecorder(document);
    restoreScroll();
    if (window.matchMedia("(min-width: 1100px)").matches && document.querySelector(".conv-side")) openTranscript();
    // Arriving at "#L12" (a search result, a task's "line N" from another page): open the transcript and show that line.
    var line = /^#L\d+$/.test(location.hash) && document.getElementById(location.hash.slice(1));
    if (line) { openTranscript(); line.classList.add("cursor"); line.scrollIntoView({ block: "center" }); }
  }
  // Jump bar: open the section (and any folded parent) before scrolling to it, so its header and content show. A
  // section in the wide-screen right column (.conv-side) scrolls inside that column only; the left one stays put.
  document.addEventListener("click", function (e) {
    var a = e.target.closest(".jump-bar a");
    if (!a) return;
    e.preventDefault();
    var side = document.querySelector(".conv-side"), wide = side && getComputedStyle(side).overflowY === "auto";
    if (a.hasAttribute("data-jump-top")) {
      window.scrollTo({ top: 0, behavior: "smooth" });
      if (wide) side.scrollTo({ top: 0, behavior: "smooth" });
      return;
    }
    var t = document.getElementById(a.getAttribute("href").slice(1));
    if (!t) return;
    for (var d = t; d; d = d.parentElement) if (d.tagName === "DETAILS") d.open = true;
    if (wide && side.contains(t)) side.scrollTo({ top: side.scrollTop + t.getBoundingClientRect().top - side.getBoundingClientRect().top, behavior: "smooth" });
    else t.scrollIntoView({ behavior: "smooth", block: "start" });
  });
  // Action buttons that open a panel under their row ([data-panel]); one open at a time; the buttons never move.
  document.addEventListener("click", function (e) {
    var b = e.target.closest("[data-panel]");
    if (!b) return;
    var open = b.getAttribute("aria-expanded") !== "true";
    b.parentElement.querySelectorAll("[data-panel]").forEach(function (x) {
      x.setAttribute("aria-expanded", "false");
      var p = document.getElementById(x.dataset.panel); if (p) p.hidden = true;
    });
    if (open) {
      b.setAttribute("aria-expanded", "true");
      var p = document.getElementById(b.dataset.panel); p.hidden = false;
      var f = p.querySelector("input, select, textarea"); if (f) f.focus({ preventScroll: true });
    }
  });
  // Autosave forms ([data-autosave], hx-trigger=change): say "Saved" after each change.
  document.addEventListener("htmx:afterRequest", function (e) {
    var f = e.detail.elt; if (!f || !f.matches || !f.matches("[data-autosave]")) return;
    var st = f.querySelector(".save-state"); if (!st) return;
    st.textContent = e.detail.successful ? "Saved ✓" : "Couldn't save. Check your connection and try again.";
    st.classList.toggle("saved", e.detail.successful);
  });
  document.addEventListener("DOMContentLoaded", onPage);
  document.addEventListener("htmx:afterSettle", function (e) { bindRecorder(e.target); if (e.detail && e.detail.boosted) onPage(); });
  if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js");
})();
