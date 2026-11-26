/* No vibration (Brian, 2026-11-23: his browser can't vibrate, so the setting and every buzz are gone). */
(function () {
  // a talk page that was loaded early (on press) reports that it was really opened, so its New badge goes
  function opened() { var m = document.getElementById("mark-opened"); if (m && !m.dataset.sent) { m.dataset.sent = "1"; fetch("/conversations/" + m.dataset.cid + "/opened", { method: "POST", credentials: "same-origin" }).catch(function () {}); } }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", opened); else opened();
})();

// Scribe Bot page helpers (vanilla JS, no framework). Event delegation, so content swapped in by htmx just works.
//   tap a timestamp to play · highlight the line being played · "line N" links open the transcript
//   keyboard shortcuts · copy-to-clipboard · in-app voice notes
(function () {
  var lastUserScroll = 0;
  ["wheel", "touchmove"].forEach(function (ev) {
    window.addEventListener(ev, function () { lastUserScroll = Date.now(); }, { passive: true });
  });
  function typing(el) { return el && (el.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName)); }
  function player() { return document.getElementById("player"); }
  function lines() { return Array.prototype.slice.call(document.querySelectorAll(".line[data-start]")); }
  function openTranscript() { var d = document.getElementById("transcript"); if (d) d.open = true; }
  /* F117 (live check): a "line 6" link marked line 6 but left the old k mark on line 1, so k went on from line 1.
     One mark at a time now, wherever it is set (a link, the address, the lines arriving, back/forward). */
  function markLine(ln) {
    if (!ln) return;
    document.querySelectorAll(".line.cursor").forEach(function (l) { if (l !== ln) l.classList.remove("cursor"); });
    ln.classList.add("cursor"); ln.scrollIntoView({ block: "center" });
  }
  window.addEventListener("hashchange", function () {
    if (/^#L\d+$/.test(location.hash)) { openTranscript(); markLine(document.getElementById(location.hash.slice(1))); }
  });

  // Theme: Light, Dark or System (Settings › Appearance).
  // Cookie (server renders it, no flash) + localStorage (survives cookie clears).
  var THEMES = ["system", "light", "dark"];
  function applyTheme(t) {
    if (t === "system") document.documentElement.removeAttribute("data-theme");
    else document.documentElement.setAttribute("data-theme", t);
    // F68 (screen pass): the reading pane (a same-site frame) kept the old theme until it reloaded; it follows at once
    document.querySelectorAll("iframe").forEach(function (f) { try { var d = f.contentDocument && f.contentDocument.documentElement;
      if (d) { if (t === "system") d.removeAttribute("data-theme"); else d.setAttribute("data-theme", t); } } catch (_) {} });
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
    var sel = window.getSelection && window.getSelection();
    if (ln && sel && !sel.isCollapsed) ln = null;   // selecting words to fix them doesn't start the audio
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
      if (a) { a.currentTime = Math.max(0, at - 0.3); var pr = a.play(); if (pr && pr.catch) pr.catch(function () {}); }
      return;
    }
    var lnk = e.target.closest('a[href^="#L"]');   // same link twice gives no hashchange, so mark it here too (F117)
    if (lnk) { openTranscript(); markLine(document.getElementById(lnk.getAttribute("href").slice(1))); }
    var cancel = e.target.closest("[data-cancel]");
    if (cancel && cancel.closest(".confirm-slot")) { e.preventDefault(); cancel.closest(".confirm-slot").innerHTML = ""; return; }
    var menu = e.target.closest(".row-menu, .chip-menu");   // one menu open at a time; a tap outside closes it
    document.querySelectorAll(".row-menu[open], .chip-menu[open]").forEach(function (d) { if (d !== menu) d.open = false; });
    if (e.target.closest("[data-dismiss-undo]")) { document.getElementById("undo-bar").hidden = true; return; }
    var go = e.target.closest("[data-glance-open]");
    if (go) { var cv = document.getElementById("conv"); if (cv) cv.classList.remove("glance-on"); var gl = document.getElementById("glance"); if (gl) gl.hidden = true;
      if (go.tagName !== "A") { e.preventDefault(); window.scrollTo(0, 0); return; }
      // A link into the full page (Transcript, a decision, a task): open that section (and any folded parent) and show it,
      // even when the hash is already the same (a second tap after going back to the glance card).
      e.preventDefault();
      var id = go.getAttribute("href").slice(1), el = document.getElementById(id);
      if (id === "transcript") openTranscript();
      if (el) { for (var d = el; d; d = d.parentElement) if (d.tagName === "DETAILS") d.open = true;
        history.replaceState(null, "", "#" + id); setTimeout(function () { el.scrollIntoView({ block: "start" }); }, 0); }
      return; }
    if (e.target.closest("[data-glance-back]")) { var cv2 = document.getElementById("conv"), gl2 = document.getElementById("glance");
      if (cv2) cv2.classList.add("glance-on"); if (gl2) gl2.hidden = false;
      history.replaceState(null, "", location.pathname + location.search); window.scrollTo(0, 0); return; }
    if (e.target.closest("[data-tx-size]")) { var tx = document.getElementById("tx-read"); if (tx) tx.classList.toggle("big"); return; }
    var more = e.target.closest("[data-cap-more]");
    if (more) { var cc = more.closest("[data-cap]"); cc.classList.add("cap-open"); more.parentNode.remove(); return; }
    var copy = e.target.closest("[data-copy]");
    if (copy) {
      var src = document.querySelector(copy.dataset.copy);
      if (!src) return;
      var text = src.value !== undefined ? src.value : src.textContent;
      /* F112 (1024 pass): "c" copied the summary from a button inside a shut menu, so "Copied ✓" showed nowhere; a note
         shows instead. F113: putting the label back as plain text dropped the button's icon (and a second tap within
         1.6 s kept "Copied ✓" for good). */
      var done = function () {
        if (!copy.dataset.was) copy.dataset.was = copy.innerHTML;
        copy.textContent = "Copied ✓"; clearTimeout(copy._t);
        copy._t = setTimeout(function () { copy.innerHTML = copy.dataset.was; delete copy.dataset.was; }, 1600);
        /* F116 (live check): Chrome still lays out a button inside a shut <details> (it hides it with content-visibility),
           so getClientRects() alone said "on screen" and the note never showed. A shut menu now counts as unseen too. */
        var unseen = !copy.getClientRects().length || !!copy.closest("details:not([open])") || (copy.checkVisibility && !copy.checkVisibility());
        if (window.ScribeToast && unseen) ScribeToast(copy.dataset.copy === "#recap" ? "Summary copied" : "Copied");
      };
      if (navigator.clipboard && window.isSecureContext) navigator.clipboard.writeText(text.trim()).then(done, function () { if (window.ScribeToast) ScribeToast("Couldn't copy"); });
      else { src.hidden = false; src.select(); document.execCommand("copy"); src.hidden = true; done(); }
    }
  });

  // Highlight the line being played; follow it unless Brian scrolled in the last 4 s. (Media events don't bubble: capture.)
  // The line being spoken wins; in a pause the line just finished stays lit for TAIL s, so it doesn't flicker. While
  // playing it checks every 0.1 s (timeupdate alone comes every ~0.25 s), with a small LEAD: word times mark a word a
  // moment after it starts. (2026-10-20: it ran ~1 s late, because the finished line kept the light for its whole TAIL.)
  var current = null, ticker = null, LEAD = 0.2, TAIL = 0.75;
  // R5-22: the lines and their times are read once and kept until the page changes, not looked up and parsed again
  // 10 times a second while playing (a long talk has thousands of lines: that ran the whole time audio played).
  var timedCache = null;
  if (window.MutationObserver) new MutationObserver(function () { timedCache = null; })
    .observe(document.documentElement, { childList: true, subtree: true, attributes: true, attributeFilter: ["data-start", "data-end"] });
  function timed() {
    if (!timedCache || !window.MutationObserver) {
      var ls = lines();
      timedCache = { ls: ls, s: ls.map(function (l) { return parseFloat(l.dataset.start); }), e: ls.map(function (l) { return parseFloat(l.dataset.end); }) };
    }
    return timedCache;
  }
  function highlight(a) {
    if (!document.body.contains(a)) { clearInterval(ticker); ticker = null; return; }   // left the page while playing
    var t = a.currentTime + LEAD, hit = null, tail = null, tm = timed();
    for (var i = 0; i < tm.ls.length; i++) {   // lines are in time order
      if (tm.s[i] > t) break;
      if (t < tm.e[i]) hit = tm.ls[i];
      else if (t < tm.e[i] + TAIL) tail = tm.ls[i];
    }
    hit = hit || tail;
    if (hit === current) return;
    if (current) current.classList.remove("playing");
    current = hit;
    if (!hit) return;
    hit.classList.add("playing");
    var d = document.getElementById("transcript");
    // R4-12 (Brian, 2026-11-25): never scroll away from a line being fixed
    if (d && d.open && Date.now() - lastUserScroll > 4000 && !document.querySelector(".lines details.line-edit[open], .lines [contenteditable=\"true\"], .fix-menu")) {
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

  // R5-10: a second tap on a form's button before the page answers sent it twice (two "Add task"s, a line split or
  // joined twice). A plain post (one that loads a page) goes once; forms the page code sends itself are left alone.
  // Checked last (on window), so a form another handler took over (htmx, fetch, a confirm step) is already marked.
  window.addEventListener("submit", function (e) {
    var f = e.target;
    if (e.defaultPrevented || !f || !f.getAttribute || (f.getAttribute("method") || "").toLowerCase() !== "post" || f.target) return;
    var t = +f.dataset.sentAt || 0;
    if (Date.now() - t < 4000) { e.preventDefault(); return; }
    f.dataset.sentAt = Date.now();
  });
  addEventListener("pageshow", function () { document.querySelectorAll("form[data-sent-at]").forEach(function (f) { delete f.dataset.sentAt; }); });

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
  var GO = { h: "/", c: "/conversations", t: "/tasks", p: "/people", r: "/review", s: "/settings" }, goAt = 0;
  /* F110 (1024 pass): the transcript loads when its section opens, so before that j/k found no lines and did nothing.
     Now the key opens the section and moves to the line once the lines arrive.
     F111: after a "line 6" link, k went back to line 1; it now carries on from the marked line. */
  var pend = null;
  function txLater() { var b = document.getElementById("tx-body"); return !!(b && b.hasAttribute("hx-get")); }
  function step(k) {
    var ls = lines(), a = player();
    openTranscript();
    if (!ls.length) { pend = k; return; }
    var mk = document.querySelector(".line.cursor");   // F111: start from the line a "line 6" link marked, not line 1
    var cursor = mk ? ls.indexOf(mk) : current ? ls.indexOf(current) : -1;
    cursor = Math.max(0, Math.min(ls.length - 1, cursor + (k === "k" ? 1 : -1)));   // Brian: k = next, j = previous
    ls.forEach(function (l) { l.classList.remove("cursor"); });
    ls[cursor].classList.add("cursor");
    ls[cursor].scrollIntoView({ block: "center" });
    if (a) a.currentTime = parseFloat(ls[cursor].dataset.start);
  }
  document.addEventListener("htmx:afterSettle", function () { if (pend && lines().length) { var k = pend; pend = null; step(k); } });
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
    } else if ((e.key === "j" || e.key === "k") && (lines().length || txLater())) {
      step(e.key);
    } else if (e.key === "/") {
      e.preventDefault();
      // F129: the first search box you can see (the computer top bar is hidden under 960 px, so "/" did nothing there),
      // and without scrolling: focusing the sticky top bar's box jumped a talk back to its top (D-SP-7)
      var q = [].filter.call(document.querySelectorAll('input[type="search"][name="q"]'), function (el) { return el.offsetParent !== null; })[0];
      if (q) q.focus({ preventScroll: true }); else location.href = "/search";
    } else if (e.key === "g") {
      goAt = Date.now();
    } else if (e.key === "e" && document.querySelector(".line.cursor")) {   // F111: also the line a link marked
      var ed = document.querySelector(".line.cursor").querySelector(".line-edit");
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
      text: "New recordings wait here under Needs you. Each shows how long it takes and what it costs. Summarize gives a summary and tasks; Transcribe is free and stays on the M14." },
    { path: "conv", spot: "#check-strip", title: "3. Read",
      text: "Each talk opens with this strip: what you owe, what others owe you, and anything worth a second look." },
    { path: "conv", spot: "#sec-owe .item|#speakers", title: "4. Fix",
      text: "Tap Done when a task is finished. The ⋯ button has Change person, Edit and Remove. Tap a speaker's name to rename them. Every change can be undone." },
    { path: "/search", spot: '.main input[name="q"]', title: "5. Find",
      text: "Search finds any word in any transcript, task, decision or person, one card per talk, with filters and your recent searches. Everything else is under ⋯ and ⚙. That's the tour!" },
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
    var st = tourState();
    if (!st) return;
    // B26 (Brian, 2026-11-25): a tap anywhere outside the tour card ends the tour, like the ✕ (and Escape).
    if (!b) { var card = document.getElementById("tour"); if (card && !card.hidden && !card.contains(e.target)) { setTour(null); showTour(); } return; }
    if (b.dataset.tour === "skip" || (b.dataset.tour === "next" && st.i === TOUR.length - 1)) setTour(null);
    else { st.i = Math.max(0, st.i + (b.dataset.tour === "next" ? 1 : -1)); setTour(st); }
    showTour();
  });
  document.addEventListener("keydown", function (e) {
    var card = document.getElementById("tour");
    if (e.key === "Escape" && card && !card.hidden && tourState()) { setTour(null); showTour(); }
  });
  document.addEventListener("DOMContentLoaded", showTour);
  // D-12: the public demo's welcome card, once per browser; any of its buttons (or Escape) closes it for good
  (function () {
    var w = document.getElementById("demo-welcome");
    if (!w) return;
    var seen = function () { try { return localStorage.getItem("demo-welcome") === "1"; } catch (_) { return true; } };
    var close = function () { w.hidden = true; try { localStorage.setItem("demo-welcome", "1"); } catch (_) {} };
    if (!seen() && !tourState() && !document.documentElement.classList.contains("in-pane")) w.hidden = false;
    w.addEventListener("click", function (e) { if (e.target.closest("[data-demo-welcome]")) close(); });
    document.addEventListener("keydown", function (e) { if (e.key === "Escape" && !w.hidden) close(); });
  })();

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
  // Brian, 2026-11-20: it also hides by itself after fixes that refresh without htmx (fix who said what), and a
  // sideways swipe on it dismisses it (like a phone notification).
  new MutationObserver(function (ms) {
    if (ms.some(function (m) { return [].some.call(m.addedNodes, function (n) { return n.id === "undo-bar"; }) || (m.target.id === "undo-bar" && m.attributeName === "hidden"); })) undoAutoHide();
  }).observe(document.body, { childList: true, subtree: true, attributes: true, attributeFilter: ["hidden"] });
  var swipe = null;
  document.addEventListener("pointerdown", function (e) {
    var b = e.target.closest && e.target.closest("#undo-bar");
    if (b && e.pointerType !== "mouse" && !e.target.closest("button, a")) swipe = { b: b, x: e.clientX, dx: 0 };
  });
  document.addEventListener("pointermove", function (e) {
    if (!swipe) return;
    swipe.dx = e.clientX - swipe.x;
    swipe.b.style.transition = "none";
    swipe.b.style.transform = "translateX(calc(-50% + " + swipe.dx + "px))"; swipe.b.style.opacity = String(Math.max(0.2, 1 - Math.abs(swipe.dx) / 220));
  }, { passive: true });
  function swipeEnd() {
    if (!swipe) return;
    var b = swipe.b, far = Math.abs(swipe.dx) > 70; swipe = null;
    b.style.transition = ""; b.style.transform = ""; b.style.opacity = "";
    if (far) { b.hidden = true; clearTimeout(undoTimer); }
  }
  document.addEventListener("pointerup", swipeEnd); document.addEventListener("pointercancel", swipeEnd);

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
    // A transcript line saves in the background and shows at once (2026-11-20: it reloaded the whole talk page and
    // timed out on the phone while the audio was loading).
    if (window.ScribeFix && el.closest(".lines[data-split-speakers]")) f.dataset.fast = "1";
    else if (el.dataset.editTarget) {
      var t = "#" + el.dataset.editTarget;
      f.setAttribute("hx-post", el.dataset.editUrl); f.setAttribute("hx-target", t); f.setAttribute("hx-select", t);
      f.setAttribute("hx-swap", "outerHTML"); f.setAttribute("hx-select-oob", "#undo-bar");
    }
    var extra = JSON.parse(el.dataset.editExtra || "{}");
    Object.keys(extra).forEach(function (k) {
      var h = document.createElement("input"); h.type = "hidden"; h.name = k; h.value = extra[k]; f.appendChild(h);
    });
    var ta = document.createElement("textarea");
    ta.name = el.dataset.editField; ta.value = el.dataset.editValue != null ? el.dataset.editValue : el.textContent.trim(); ta.required = "editRequired" in el.dataset;
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
  window.ScribeEdit = startEdit;
  document.addEventListener("dblclick", function (e) {
    var el = e.target.closest("[data-edit-url]");
    if (el && el.closest("[data-hold-copy]")) return;   // summary text: a double-tap selects words (Brian, 2026-11-23); ✎ edits
    // Splitting a line ("Only part of it?") uses taps too: a double-tap there, or right after a split, isn't an edit
    // (Brian, 2026-11-21: the two collided). Words are fixed with ✎ Fix words in the name menu, or a double-tap elsewhere.
    if (el && window.ScribeFix && window.ScribeFix.busy()) { e.preventDefault(); return; }
    if (el && !e.target.closest("a, button")) {
      e.preventDefault();
      var a = player(); if (a && el.closest(".lines") && !a.paused) a.pause();   // the two taps also started the audio
      startEdit(el);
    }
  });
  document.addEventListener("submit", function (e) {
    var f = e.target;
    if (!f.matches || !f.matches("form.inline-edit[data-fast]")) return;
    e.preventDefault(); e.stopImmediatePropagation();
    var el = f.previousElementSibling, data = {};
    new FormData(f).forEach(function (v, k) { data[k] = v; });
    if (el) { el.textContent = data.text; el.dataset.editValue = data.text; delete el.dataset.wrapped; }
    cancelEdit(f, true);
    window.ScribeFix.send(f.action, data);
  }, true);
  // Click (or tap) away = save (Brian 2026-10-22: no Save button). Unchanged or emptied text just closes.
  document.addEventListener("focusout", function (e) {
    var f = e.target.closest && e.target.closest("form.inline-edit");
    if (!f || (e.relatedTarget && f.contains(e.relatedTarget))) return;
    setTimeout(function () {
      if (!f.isConnected || f.contains(document.activeElement)) return;
      var ta = f.querySelector("textarea"), el = f.previousElementSibling;
      var same = ta.value.trim() === (el ? (el.dataset.editValue != null ? el.dataset.editValue : el.textContent) : "").trim();
      if (same || (ta.required && !ta.value.trim())) cancelEdit(f, true); else f.requestSubmit();
    }, 120);
  });
  document.addEventListener("keydown", function (e) {
    var f = e.key === "Escape" && e.target.closest && e.target.closest("form.inline-edit");
    if (f) { e.stopImmediatePropagation(); cancelEdit(f); }
  }, true);

  // U12 (Brian, 2026-11-25: a transcript line's ⋯ and right-click › More fixes were "unusable as built", waiting on the
  // M14 every time): the line's fix panel is copied from the page's one #line-edit-tpl the moment it opens, and filled
  // with this line's id, speaker and words. Only if that copy is missing does it ask the M14 (the old way).
  // R4-12 (Brian, 2026-11-25): opening a line's fixes pauses the audio, so the page stops moving under you
  document.addEventListener("toggle", function (e) {
    var d = e.target; if (!d.open || !d.matches || !d.matches("details.line-edit")) return;
    var a = document.getElementById("player"); if (a && !a.paused) a.pause();
  }, true);
  // R4-11: picking who said the line saves it at once; "Someone new…" asks for the name first
  document.addEventListener("change", function (e) {
    var sel = e.target; if (!sel.matches || !sel.matches("select[data-le-who]")) return;
    var f = sel.form, extra = f.querySelectorAll(".le-new");
    if (!sel.value) { extra.forEach(function (x) { x.hidden = false; }); var n = f.querySelector('input[name="new_name"]'); if (n) n.focus(); return; }
    extra.forEach(function (x) { x.hidden = true; });
    if (f.requestSubmit) f.requestSubmit(); else f.submit();
  });
  document.addEventListener("toggle", function (e) {
    var d = e.target;
    if (!d.open || !d.matches || !d.matches("details.line-edit") || d.dataset.filled) return;
    var line = d.closest(".line[data-seg]"), panel = d.querySelector(":scope > .chip-panel"); if (!line || !panel) return;
    d.dataset.filled = "1";
    var sid = line.dataset.seg, idx = line.id.slice(1), tpl = document.getElementById("line-edit-tpl");
    function put(html) {
      var w = document.createElement("div"); w.innerHTML = html;
      var np = w.querySelector(".chip-panel"); if (!np) return;
      var sel = np.querySelector('select[name="speaker_id"]'); if (sel && line.dataset.sp) sel.value = line.dataset.sp;
      var ta = np.querySelector('textarea[name="text"]'), said = line.querySelector(":scope > div > span");
      if (ta && said && !ta.value) ta.value = said.textContent;
      if (idx === "0") { var j = np.querySelector(".le-join"); if (j) j.remove(); }
      panel.replaceWith(np);
      if (window.htmx) htmx.process(np);
      try { np.querySelectorAll("textarea").forEach(grow); } catch (_) {}
    }
    if (tpl) put(tpl.innerHTML.replace(/__SID__/g, sid).replace(/__IDX__/g, idx));
    else fetch("/segments/" + sid + "/edit", { credentials: "same-origin" }).then(function (r) { return r.ok ? r.text() : ""; }).then(put).catch(function () { delete d.dataset.filled; });
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
      if (document.documentElement.hasAttribute("data-demo")) { window.sbNote("Demo: voice notes aren't recorded here. Nothing was uploaded."); return; }   // D-11
      if (rec.dataset.retry && rec._retry) { delete rec.dataset.retry; rec._retry(); return; }
      if (mr && mr.state === "recording") { mr.stop(); return; }
      navigator.mediaDevices.getUserMedia({ audio: true }).then(function (stream) {
        chunks = [];
        mr = new MediaRecorder(stream);
        mr.ondataavailable = function (e) { chunks.push(e.data); };
        mr.onstop = function () {
          stream.getTracks().forEach(function (t) { t.stop(); });
          var f = new FormData();
          var stamp = new Date().toISOString().slice(0, 16).replace("T", " ").replace(":", "_");
          f.append("file", new Blob(chunks, { type: "audio/webm" }), "Voice note " + stamp + ".webm");
          // R5-13: a failed upload kept "Uploading…" for good; now it says so and the next tap sends the same note again
          var up = function () {
            rec.textContent = "Uploading…";
            fetch("/capture/upload", { method: "POST", body: f }).then(function (r) {
              if (!r.ok) throw r.status;
              location.href = "/capture?saved=audio";
            }).catch(function (st) {
              rec.textContent = "Didn't save · Tap to try again"; rec.dataset.retry = "1"; rec._retry = up;
              window.sbNote(window.sbFail(typeof st === "number" ? st : 0));
            });
          };
          up();
        };
        mr.start();
        rec.textContent = "■ Stop and save";
      }).catch(function () { window.sbNote("The microphone isn't allowed. Allow it for this site in the browser's settings."); });
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
  window.sbNote = netNote;   // R5-12: the app's own note for every block (no browser alert boxes)
  // R5-11: never an error code on screen; a plain sentence that says what to do
  window.sbFail = function (status) {
    if (document.documentElement.hasAttribute("data-demo"))   // the public demo has no M14 or Tailscale to point to
      return status === 0 ? "Can't reach the demo. Check your connection and try again." : "That didn't work in the demo. Reload the page and try again.";
    return status >= 500 ? "Something went wrong on the M14. Try again; if it keeps happening, open Status."
      : status === 0 ? "Can't reach the M14. Check Tailscale is on, then try again."
      : status === 413 ? "That file is too big to send."
      : "That didn't work. Reload the page and try again.";
  };
  document.addEventListener("htmx:responseError", function (e) {
    var d = e.detail, msg = "";
    try { msg = JSON.parse(d.xhr.responseText).detail; } catch (_) {}
    if (d.xhr.status === 422 && typeof msg !== "string") msg = "Something in that form was missing. Check it and try again.";
    netNote(typeof msg === "string" && msg ? msg : window.sbFail(d.xhr.status));
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
    if (line) { openTranscript(); markLine(line); }
  }
  // Jump bar: open the section (and any folded parent) before scrolling to it, so its header and content show. A
  // section in the wide-screen right column (.conv-side) scrolls inside that column only; the left one stays put.
  document.addEventListener("click", function (e) {
    var a = e.target.closest(".jump-bar a, .rail-src a, .rail-out a");
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
    // A short glow on where you landed (Brian, 2026-11-20, like his portfolio). A long section glows only its heading.
    // Only the heading glows (Brian, 2026-11-21: a glow around the whole section looked cut off and muddy)
    var g = t.matches(".line, .list-head, .section-head, .part-div") ? t
      : t.tagName === "DETAILS" ? t.querySelector(":scope > summary") || t
      : t.querySelector(":scope > .section-head, :scope > summary, .section-head, h2") || t;
    g.classList.remove("jump-glow"); void g.offsetWidth; g.classList.add("jump-glow");
    setTimeout(function () { g.classList.remove("jump-glow"); }, 1900);
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
      if (p.scrollIntoView) setTimeout(function () { p.scrollIntoView({ block: "nearest", behavior: "smooth" }); }, 30);
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
  document.addEventListener("htmx:afterSettle", function (e) { bindRecorder(e.target); });
  // Zoomed in by accident stays zoomed on the next page (links swap the page in place, so the browser keeps the zoom;
  // Brian, 2026-11-21: "I have to zoom out to see the rail"). Each new page snaps back to 100%: the viewport is pinned
  // to scale 1 for a moment, then pinch-zoom is allowed again.
  function unzoom() {
    var vv = window.visualViewport, meta = document.querySelector('meta[name=viewport]');
    if (!meta || (vv && Math.abs(vv.scale - 1) < 0.01)) return;
    var base = meta.getAttribute("content").replace(/,\s*maximum-scale=[^,]*/g, "");
    meta.setAttribute("content", base + ",maximum-scale=1");
    setTimeout(function () { meta.setAttribute("content", base); }, 400);
  }
  window.addEventListener("pageshow", unzoom);
  if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js");
})();

/* Short Today (Brian, 2026-11-20): a [data-cap="3"] card shows its first 3 rows, then "See N more" (opens in place)
   and "See all" (data-all, its full page). Works again after htmx swaps a section. */
(function () {
  function cap(root) {
    (root || document).querySelectorAll("[data-cap]").forEach(function (card) {
      if (card.querySelector(":scope > .cap-more")) return;
      var n = parseInt(card.dataset.cap, 10) || 3;
      var rows = card.querySelectorAll(":scope > .item");
      if (rows.length <= n) return;
      rows.forEach(function (r, i) { if (i >= n) r.classList.add("cap-hidden"); });
      var bar = document.createElement("div"); bar.className = "cap-more";
      bar.innerHTML = '<button type="button" class="btn btn-ghost btn-sm" data-cap-more>See ' + (rows.length - n) + ' more</button>' +
        (card.dataset.all ? '<a class="btn btn-ghost btn-sm" href="' + card.dataset.all + '">See all ›</a>' : "");
      card.appendChild(bar);
    });
  }
  document.addEventListener("DOMContentLoaded", function () { cap(); });
  document.addEventListener("htmx:afterSwap", function (e) { cap(e.target.parentNode || document); });
})();

/* A link to a line (#L12) or a section opens the full conversation page instead of the glance card. */
(function () {
  // B16 (2026-11-25): this block called openTranscript(), which lives in another block, so a page opened at "#L6" stopped
  // with "openTranscript is not defined" and the transcript never loaded. It has its own copy now.
  function openTranscript() { var d = document.getElementById("transcript"); if (d) d.open = true; }
  /* The last choice is remembered on this device (Brian, 2026-11-20: getting to "Show everything" took a long scroll):
     after "Show everything", talks open in full until "‹ At a glance" is tapped. */
  function wantsFull() { try { return localStorage.getItem("talkView") === "full"; } catch (_) { return false; } }
  function full() { var cv = document.getElementById("conv"); if (!cv) return;
    if (!location.hash && wantsFull() && cv.classList.contains("glance-on")) { cv.classList.remove("glance-on"); var g0 = document.getElementById("glance"); if (g0) g0.hidden = true; return; }
    if (location.hash && location.hash !== "#glance") { cv.classList.remove("glance-on"); var gl = document.getElementById("glance"); if (gl) gl.hidden = true;
    var el = document.getElementById(location.hash.slice(1));   // "#transcript" etc. arrives opened, not folded
    if (!el && /^#(L\d+|part-\d+)$/.test(location.hash)) { openTranscript(); return; }   // a line: load the transcript first, then this runs again
    if (el) { for (var d = el; d; d = d.parentElement) if (d.tagName === "DETAILS") d.open = true; el.scrollIntoView({ block: "start" }); } } }
  document.addEventListener("click", function (e) {
    if (!e.target.closest) return;
    try { if (e.target.closest("[data-remember]")) localStorage.setItem("talkView", "full");
          else if (e.target.closest("[data-glance-back]")) localStorage.setItem("talkView", "glance"); } catch (_) {}
  }, true);
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", full); else full();
  window.addEventListener("hashchange", full);
  document.addEventListener("htmx:afterSettle", full);
})();

/* Fix who said what (Brian picked A, 2026-11-20): select words in a transcript line and a small bar offers
   "✂ Split here" (the words become their own line) and "Who said this? ▾" (they also move to another speaker).
   Saves at once; no AI. Works with a mouse drag, a long-press on a phone, or a cursor for "split here". */
(function () {
  var bar = null, cur = null;
  function hide() { if (bar) bar.hidden = true; cur = null; }
  function textEl(node) { var el = node && (node.nodeType === 1 ? node : node.parentElement); return el && el.closest(".lines .line [data-edit-url]"); }
  function offset(el, node, off) { var r = document.createRange(); r.selectNodeContents(el); r.setEnd(node, off); return r.toString().length; }
  function post(extra) {
    if (!cur) return;
    if (window.ScribeFix) {   // fast: only the changed lines come back (2026-11-20)
      var seg = cur.seg, d = Object.assign({ start: cur.start, end: cur.end }, extra);
      hide(); try { window.getSelection().removeAllRanges(); } catch (_) {}
      window.ScribeFix.send("/segments/" + seg + "/split", d); return;
    }
    var f = document.createElement("form"); f.method = "post"; f.action = "/segments/" + cur.seg + "/split"; f.hidden = true;
    var data = Object.assign({ start: cur.start, end: cur.end }, extra);
    Object.keys(data).forEach(function (k) { var i = document.createElement("input"); i.name = k; i.value = data[k]; f.appendChild(i); });
    document.body.appendChild(f); hide(); f.submit();
  }
  function build(lines) {
    bar = document.createElement("div"); bar.className = "split-bar"; bar.hidden = true; bar.setAttribute("role", "toolbar");
    bar.setAttribute("aria-label", "Fix these words");
    var sp = []; try { sp = JSON.parse(lines.dataset.splitSpeakers || "[]"); } catch (_) {}
    bar.innerHTML = '<button type="button" class="btn btn-primary btn-sm" data-split>✂ Split here</button>' +
      '<button type="button" class="btn btn-sm split-who" data-who aria-expanded="false">Who said this? ▾</button>' +
      '<div class="split-menu" hidden>' + sp.map(function (s) {
        return '<button type="button" data-sp="' + s[0] + '"><i style="background:' + s[2] + '"></i>' +
          String(s[1]).replace(/[&<>"]/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]; }) + '</button>';
      }).join("") + '<form data-split-new><input name="n" placeholder="Someone else…" aria-label="Someone else\'s name"><button class="btn btn-sm">OK</button></form></div>';
    document.body.appendChild(bar);
    // Keep the selection while tapping the bar (a tap would otherwise clear it before the click lands)
    bar.addEventListener("pointerdown", function (e) { if (!e.target.closest("input")) e.preventDefault(); });
    bar.addEventListener("click", function (e) {
      if (e.target.closest("[data-split]")) post({});
      var who = e.target.closest("[data-who]");
      if (who) { var m = bar.querySelector(".split-menu"); m.hidden = !m.hidden; who.setAttribute("aria-expanded", String(!m.hidden)); place(); }
      var b = e.target.closest("[data-sp]"); if (b) post({ speaker_id: b.dataset.sp });
    });
    bar.addEventListener("submit", function (e) { e.preventDefault(); var v = bar.querySelector("[data-split-new] input").value.trim(); if (v) post({ new_name: v }); });
  }
  function place() {
    if (!bar || !cur) return;
    var r = cur.rect, w = bar.offsetWidth, x = Math.max(8, Math.min(innerWidth - w - 8, r.left + r.width / 2 - w / 2));
    var below = r.bottom + 10;   // below the words: Android puts its own copy/paste bar above them
    if (below + bar.offsetHeight > innerHeight - 80) below = Math.max(8, r.top - bar.offsetHeight - 10);
    bar.style.left = x + "px"; bar.style.top = below + "px";
  }
  var t = null, mouse = window.matchMedia && matchMedia("(pointer: fine)").matches;   // phones use Fix mode (taps) instead
  document.addEventListener("selectionchange", function () {
    if (!mouse) return;
    clearTimeout(t);
    t = setTimeout(function () {
      var s = window.getSelection();
      if (!s || !s.rangeCount) { if (!(bar && bar.contains(document.activeElement))) hide(); return; }
      var r = s.getRangeAt(0), a = textEl(r.startContainer), b = textEl(r.endContainer);
      if (bar && bar.contains(r.startContainer)) return;
      if (!a || a !== b || s.isCollapsed) { if (!(bar && bar.contains(document.activeElement))) hide(); return; }
      var lines = a.closest(".lines"); if (!bar) build(lines);
      var line = a.closest(".line");
      cur = { seg: line.dataset.seg, start: offset(a, r.startContainer, r.startOffset), end: offset(a, r.endContainer, r.endOffset),
              rect: r.getBoundingClientRect() };
      bar.querySelector(".split-menu").hidden = true; bar.querySelector("[data-who]").setAttribute("aria-expanded", "false");
      bar.hidden = false; place();
    }, 250);
  });
  window.addEventListener("scroll", function () { if (bar && !bar.hidden) { var s = window.getSelection(); if (s && s.rangeCount) { cur && (cur.rect = s.getRangeAt(0).getBoundingClientRect()); place(); } } }, { passive: true });
  document.addEventListener("keydown", function (e) { if (e.key === "Escape") hide(); });
})();

/* Who said what (Brian picked M2 A, 2026-11-20; replaces Fix mode, which was too slow: every tap reloaded the whole
   talk page and rebuilt every word). Tap a name: pick who said it (the whole block of lines under that name), or
   "✂ Only part of it?" then tap the word where the other person starts (1:1: the other person, else pick). The change
   shows at once; the M14 saves in the background and sends back only the few lines that changed. Lines in a row by
   the same person share one name; a pause of 30 s or more starts a new paragraph with its own time. */
(function () {
  var menu = null, part = null, PAUSE = 30;
  function box() { return document.querySelector(".lines[data-split-speakers]"); }
  function esc(t) { return String(t).replace(/[&<>"]/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]; }); }
  function speakers() { try { return JSON.parse(box().dataset.splitSpeakers || "[]"); } catch (_) { return []; } }
  function sp(id) { var s = speakers().filter(function (x) { return String(x[0]) === String(id); })[0]; return s || [id, "Unknown", "currentColor"]; }
  function all() { var l = box(); return l ? [].slice.call(l.querySelectorAll(".line[data-seg]")) : []; }
  function regroup() {
    var prev = null, shown = -1e9;
    all().forEach(function (l, i) {
      l.id = "L" + i;
      var txt = l.querySelector("[data-edit-url]"); if (txt) { txt.dataset.editTarget = l.id; txt.dataset.editLabel = "line " + (i + 1); }
      var same = !!prev && prev.dataset.sp === l.dataset.sp && l.dataset.sp !== "", s = parseFloat(l.dataset.start) || 0;
      var pause = same && s - parseFloat(prev.dataset.end) >= PAUSE;
      l.classList.toggle("same", same);
      l.classList.toggle("pause", pause);
      // T1 A (Brian, 2026-11-20): inside a long stretch by one person, a time about once a minute, to find 15:30.
      var mark = same && !pause && s - shown >= 60;
      l.classList.toggle("tmark", mark);
      if (!same || pause || mark) shown = s;
      prev = l;
    });
    pin();
  }
  /* T1 A: while you read a long stretch by one person, their name stays pinned at the top of the transcript; tap it to
     change who said that stretch (or "Only part of it?"), same as tapping the name at its start. */
  var pinEl = null, pinLine = null;
  function pinBox() {
    var l = box(); if (!l) return null;
    if (!pinEl || !l.contains(pinEl)) {
      pinEl = document.createElement("div"); pinEl.className = "who-pin"; pinEl.hidden = true;
      pinEl.innerHTML = '<button type="button" class="who-pin-btn"><i></i><b></b><span></span></button>';
      l.prepend(pinEl);
    }
    return pinEl;
  }
  function pin() {
    var p = pinBox(); if (!p) return;
    var side = document.querySelector(".conv-side"), col = side && getComputedStyle(side).overflowY === "auto" && side.contains(p);
    var bar = document.querySelector(".topbar"), top = col ? side.getBoundingClientRect().top : (bar && getComputedStyle(bar).display !== "none" ? bar.getBoundingClientRect().bottom : 0);
    // Below the audio player when it's stuck at the top too (Brian, 2026-11-21: the name covered the play button).
    var pl = document.querySelector(".player"); if (pl && getComputedStyle(pl).position === "sticky") { var pr = pl.getBoundingClientRect(), st = parseFloat(getComputedStyle(pl).top) || 0; if (pr.top <= st + 2 && pr.bottom > top) top = pr.bottom; }
    if (col) { p.style.setProperty("--pin-top", (pl && side.contains(pl) ? pl.offsetHeight : 0) + "px"); }
    if (!col) p.style.setProperty("--pin-top", Math.max(0, top) + "px");
    var y = top + 44, ls = all(), cur = null, lo = 0, hi = ls.length - 1;
    if (!ls.length || ls[0].getBoundingClientRect().top > y || ls[hi].getBoundingClientRect().bottom < y) { p.hidden = true; return; }
    while (lo <= hi) { var mid = (lo + hi) >> 1; if (ls[mid].getBoundingClientRect().bottom < y) lo = mid + 1; else { cur = ls[mid]; hi = mid - 1; } }
    if (!cur) { p.hidden = true; return; }
    var head = cur; while (head.classList.contains("same") && head.previousElementSibling && head.previousElementSibling.matches(".line[data-seg]")) head = head.previousElementSibling;
    if (head.getBoundingClientRect().top >= top) { p.hidden = true; return; }   // its name is still on screen
    var s = sp(cur.dataset.sp);
    pinLine = cur;
    p.querySelector("i").style.background = s[2]; p.querySelector("b").textContent = s[1];
    p.querySelector("span").textContent = "talking since " + mmss(parseFloat(head.dataset.start) || 0);
    p.hidden = false;
  }
  var pinFrame = 0;
  function pinSoon() { if (!pinFrame) pinFrame = requestAnimationFrame(function () { pinFrame = 0; if (box()) pin(); }); }
  window.addEventListener("scroll", pinSoon, { passive: true });
  document.addEventListener("scroll", function (e) { if (e.target.classList && e.target.classList.contains("conv-side")) pinSoon(); }, { passive: true, capture: true });
  window.addEventListener("resize", pinSoon);
  function block(line) {
    var head = line;
    while (head.classList.contains("same") && head.previousElementSibling && head.previousElementSibling.matches(".line[data-seg]")) head = head.previousElementSibling;
    var out = [head], n = head.nextElementSibling;
    while (n && n.matches(".line[data-seg].same")) { out.push(n); n = n.nextElementSibling; }
    return out;
  }
  function two() { var s = {}; all().forEach(function (l) { if (l.dataset.sp) s[l.dataset.sp] = 1; }); return Object.keys(s).length === 2; }
  function setWho(l, id) {
    var s = sp(id), w = l.querySelector(".who");
    l.dataset.sp = id;
    if (w) { w.textContent = s[1]; w.style.setProperty("--c", s[2]); }
  }
  function mmss(x) { x = Math.floor(x); return Math.floor(x / 60) + ":" + ("0" + (x % 60)).slice(-2); }
  function fill(l, r) {
    l.dataset.seg = r.id; l.dataset.start = r.start_s; l.dataset.end = r.end_s;
    setWho(l, r.speaker_id);
    var t = l.querySelector(".t");
    if (t) { t.textContent = mmss(r.start_s); if (t.dataset.seek !== undefined) { t.dataset.seek = r.start_s; t.setAttribute("aria-label", "Play from " + mmss(r.start_s)); } }
    var x = l.querySelector("[data-edit-url]");
    if (x) { x.textContent = r.text; x.dataset.editUrl = "/segments/" + r.id + "/text"; x.dataset.editValue = r.text; delete x.dataset.wrapped; }
    var d = l.querySelector("details.line-edit");
    if (d && d.getAttribute("hx-get") !== "/segments/" + r.id + "/edit") {
      var fresh = document.createElement("details");
      fresh.className = "line-edit"; fresh.setAttribute("hx-get", "/segments/" + r.id + "/edit"); fresh.setAttribute("hx-trigger", "toggle once");
      fresh.setAttribute("hx-target", "find .chip-panel"); fresh.setAttribute("hx-swap", "outerHTML");
      fresh.innerHTML = d.querySelector("summary").outerHTML + '<div class="chip-panel"><p class="meta">Loading…</p></div>';
      d.replaceWith(fresh);
      if (window.htmx) htmx.process(fresh);
    }
    l.classList.remove("moving-line");
  }
  function apply(j) {
    var l = box(); if (!l) return;
    if (j.speakers) l.dataset.splitSpeakers = JSON.stringify(j.speakers);
    (j.removed || []).forEach(function (id) { var n = l.querySelector('.line[data-seg="' + id + '"]'); if (n) n.remove(); });
    var prev = null;
    (j.lines || []).forEach(function (r) {
      var n = l.querySelector('.line[data-seg="' + r.id + '"]');
      if (!n) { n = (prev || all()[0]).cloneNode(true); n.classList.remove("playing", "cursor"); (prev ? prev.after(n) : l.prepend(n)); }
      fill(n, r); prev = n;
    });
    if (j.speakers) all().forEach(function (n) { if (n.dataset.sp) setWho(n, n.dataset.sp); });   // names + colors everywhere
    regroup();
    headSoon();
    var u = document.getElementById("undo-bar");
    if (u && j.undo) {
      u.innerHTML = '<span class="undo-label">' + esc(j.undo.label) + '</span><form method="post" action="/undo/' + j.undo.id +
        '"><button class="btn btn-ghost undo-btn">Undo</button></form><button type="button" class="icon-btn" data-dismiss-undo aria-label="Dismiss" title="Hide this bar"><svg class="icon" aria-hidden="true"><use href="#i-x"/></svg></button>';
      u.classList.remove("net-note"); u.hidden = false;
    }
  }
  function send(url, data) {
    var fd = new FormData(); Object.keys(data).forEach(function (k) { fd.append(k, data[k]); });
    return fetch(url, { method: "POST", body: fd, credentials: "same-origin", headers: { "X-Fast": "1" } }).then(function (r) {
      if (!r.ok) throw new Error(r.status);
      return r.json();
    }).then(apply).catch(function () {
      var u = document.getElementById("undo-bar");
      if (u) { u.innerHTML = '<span class="undo-label">That didn\'t save. Reloading…</span>'; u.hidden = false; }
      setTimeout(function () { location.reload(); }, 1200);
    });
  }
  // The talk-time bar and speaker chips at the top follow each fix (they kept the old names/shares until a reload).
  var headT = 0;
  function headSoon() {
    clearTimeout(headT);
    headT = setTimeout(function () {
      if (document.querySelector(".conv-head .speaker-chip[open]")) return;
      fetch(location.pathname, { credentials: "same-origin" }).then(function (r) { return r.text(); }).then(function (html) {
        var d = new DOMParser().parseFromString(html, "text/html");
        [".conv-head .talk", "#speakers"].forEach(function (sel) { var a = document.querySelector(sel), b = d.querySelector(sel); if (a && b) a.replaceWith(b); });
        var nb = d.querySelector(".lines[data-split-speakers]"), l = box(); if (nb && l) l.dataset.splitSpeakers = nb.dataset.splitSpeakers;
      }).catch(function () {});
    }, 1500);
  }
  var lastPart = 0;
  window.ScribeFix = { send: send, regroup: regroup, busy: function () { return !!part || Date.now() - lastPart < 900; } };
  function closeMenu() { if (menu) { menu.remove(); menu = null; } }
  function endPart() {
    if (!part) return;
    part.lines.forEach(function (l) { l.classList.remove("parting"); });
    if (part.hint) part.hint.remove();
    part = null;
  }
  function wrap(el) {
    if (el.dataset.wrapped) return;
    var n = 0;
    el.innerHTML = el.textContent.split(/(\s+)/).map(function (t) {
      if (!t) return "";
      return /^\s+$/.test(t) ? esc(t) : '<span class="w" data-w="' + (n++) + '">' + esc(t) + "</span>";
    }).join("");
    el.dataset.wrapped = "1";
  }
  function openMenu(anchor, html, onPick) {
    closeMenu();
    menu = document.createElement("div"); menu.className = "split-bar fix-menu"; menu.setAttribute("role", "dialog");
    menu.innerHTML = html; menu._y = scrollY;
    document.body.appendChild(menu);
    var r = anchor.getBoundingClientRect(), w = menu.offsetWidth;
    // R4-11: a line that follows the same speaker hides its name, so a menu anchored there opened in the top-left corner
    if (!r.width && !r.height) r = (anchor.closest(".line") || anchor).getBoundingClientRect();
    menu.style.left = Math.max(8, Math.min(innerWidth - w - 8, r.left)) + "px";
    var top = r.bottom + 6; if (top + menu.offsetHeight > innerHeight - 70) top = Math.max(8, r.top - menu.offsetHeight - 6);
    menu.style.top = top + "px";
    menu.addEventListener("click", function (e) {
      var b = e.target.closest("[data-sp]"); if (b) { closeMenu(); onPick({ speaker_id: b.dataset.sp }); }
      if (e.target.closest("[data-part]")) { closeMenu(); onPick({ part: true }); }
      if (e.target.closest("[data-fixwords]")) { closeMenu(); onPick({ fixwords: true }); }
    });
    menu.addEventListener("submit", function (e) { e.preventDefault(); var v = menu.querySelector("input").value.trim(); if (v) { closeMenu(); onPick({ new_name: v }); } });
  }
  function names(skip, mark) {
    return speakers().filter(function (s) { return String(s[0]) !== String(skip); }).map(function (s) {
      return '<button type="button" data-sp="' + s[0] + '"><i style="background:' + esc(s[2]) + '"></i>' + esc(s[1]) +
        (String(s[0]) === String(mark) ? " ✓" : "") + "</button>";
    }).join("") + '<form><input name="n" placeholder="Someone else…" aria-label="Someone else\'s name"><button class="btn btn-sm">OK</button></form>';
  }
  function whoMenu(line, anchor) {
    var blk = block(line), here = line.dataset.sp;
    openMenu(anchor, '<div class="split-menu"><p class="fix-title">Who said ' + (blk.length > 1 ? "these " + blk.length + " lines" : "this line") + "?</p>" +
      names(null, here) + '<button type="button" data-part class="fix-part">✂ Only part of it?</button>' +
      '<button type="button" data-fixwords class="fix-part">✎ Fix words in this line</button></div>', function (c) {
      if (c.part) return startPart(blk);
      if (c.fixwords) { var x = line.querySelector("[data-edit-url]"); if (x && window.ScribeEdit) { delete x.dataset.wrapped; x.textContent = x.dataset.editValue || x.textContent; window.ScribeEdit(x); } return; }
      if (c.speaker_id && c.speaker_id === here) return;
      if (c.speaker_id) blk.forEach(function (l) { setWho(l, c.speaker_id); });
      regroup();
      var ids = blk.map(function (l) { return l.dataset.seg; });
      send("/segments/" + ids[0] + "/speaker", Object.assign({ ids: ids.join(",") }, c));
    });
  }
  function startPart(blk) {
    endPart();
    part = { lines: blk };
    blk.forEach(function (l) { l.classList.add("parting"); var x = l.querySelector("[data-edit-url]"); if (x) wrap(x); });
    var h = document.createElement("div"); h.className = "part-hint"; h.setAttribute("role", "status");
    h.innerHTML = '<span>Tap the first word someone else said. It and the rest of its line move to them.</span><button type="button" class="btn btn-sm" data-part-cancel>Cancel</button>';
    blk[0].before(h); part.hint = h;
  }
  function flip(line, w, extra) {
    lastPart = Date.now();
    var ws = line.querySelectorAll(".w"), at = parseInt(w.dataset.w, 10);
    for (var i = at; i < ws.length; i++) ws[i].classList.add("moving");
    line.classList.add("moving-line");
    endPart();
    send("/segments/" + line.dataset.seg + "/flip", Object.assign({ at: at }, extra || {}));
  }
  document.addEventListener("click", function (e) {
    if (!e.target.closest) return;
    if (menu && menu.contains(e.target)) return;
    if (e.target.closest("[data-part-cancel]")) { endPart(); return; }
    var who = e.target.closest(".lines .who");
    if (who) { e.preventDefault(); e.stopPropagation(); endPart(); whoMenu(who.closest(".line"), who); return; }
    var pb = e.target.closest(".who-pin-btn");
    if (pb) { e.preventDefault(); e.stopPropagation(); endPart(); if (pinLine) whoMenu(pinLine, pb); return; }
    var w = part && e.target.closest(".line.parting .w");
    // A finger is wider than a word: a tap between words or beside the text used to start the audio instead of
    // splitting (2026-11-20). Anywhere on a highlighted line now picks the nearest word; other lines do nothing.
    var pl = part && !w && e.target.closest(".line.parting");
    if (pl && !e.target.closest(".line-edit")) {
      var best = null, bd = 1e9;
      [].forEach.call(pl.querySelectorAll(".w"), function (x) {
        var r = x.getBoundingClientRect(), dx = Math.max(r.left - e.clientX, 0, e.clientX - r.right), dy = Math.max(r.top - e.clientY, 0, e.clientY - r.bottom);
        var d = dx + dy * 3; if (d < bd) { bd = d; best = x; }
      });
      w = best;
    }
    if (part && !w && e.target.closest(".lines") && !e.target.closest(".part-hint")) { e.preventDefault(); e.stopPropagation(); return; }
    if (w) {
      e.preventDefault(); e.stopPropagation();
      var line = w.closest(".line");
      if (two()) flip(line, w);
      else openMenu(w, '<div class="split-menu"><p class="fix-title">Who starts here?</p>' + names(line.dataset.sp) + "</div>", function (c) { flip(line, w, c); });
      return;
    }
    if (menu) { closeMenu(); }
  }, true);
  document.addEventListener("keydown", function (e) { if (e.key === "Escape") { if (menu) closeMenu(); else endPart(); } });
  // Close the menu only on a real scroll: phones nudge the page a few pixels (address bar) while you tap.
  window.addEventListener("scroll", function () { if (menu && Math.abs(scrollY - (menu._y || 0)) > 60) closeMenu(); }, { passive: true });
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", function () { if (box()) regroup(); }); else if (box()) regroup();
  document.addEventListener("htmx:afterSettle", function () { if (box()) regroup(); });
})();

/* Uploads with progress (Brian, 2026-11-23: 23 Voice Memos sat on a stuck screen with no sign of life or result).
   One file at a time, so the card can say "Uploading 3 of 23", the file name and its %, then "Checking" while the M14
   saves it. At the end every file is listed: Added / Already in Scribe Bot (never added twice; links to the talk) /
   Didn't upload (Try again). Leaving the page mid-upload asks first. Used by Capture › Upload and drag-and-drop. */
window.ScribeUpload = (function () {
  var card = null, busy = false, xhr = null, stop = false;
  function esc(t) { return String(t).replace(/[&<>"]/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]; }); }
  function mb(n) { return n < 1048576 ? Math.max(1, Math.round(n / 1024)) + " KB" : (n / 1048576).toFixed(1) + " MB"; }
  function el() {
    if (card) return card;
    card = document.createElement("div"); card.className = "up-card"; card.setAttribute("role", "status"); card.setAttribute("aria-live", "polite");
    card.innerHTML = '<div class="up-head"><b class="up-title"></b><button type="button" class="up-x" aria-label="Close">✕</button></div>' +
      '<div class="up-bar"><i></i></div><div class="up-now meta"></div><ul class="up-list"></ul><div class="up-actions"></div>';
    document.body.appendChild(card);
    card.querySelector(".up-x").addEventListener("click", close);
    return card;
  }
  function close() {
    if (busy === true) { window.sbConfirm("Stop uploading? Files already sent are kept.", "Stop").then(function (ok) { if (ok) { stop = true; if (xhr) xhr.abort(); } }); return; }
    busy = false; if (card) { card.remove(); card = null; }
  }
  document.addEventListener("keydown", function (e) { if (e.key === "Escape" && card && busy !== true) close(); });
  document.addEventListener("pointerdown", function (e) { if (card && busy !== true && !card.contains(e.target)) close(); }, true);
  window.addEventListener("beforeunload", function (e) { if (busy === true) { e.preventDefault(); e.returnValue = ""; } });
  var LABEL = { audio: ["ok", "Added"], older: ["ok", "Added (you had it, never processed)"], transcript: ["ok", "Transcript saved"],
    known: ["dup", "Already in Scribe Bot, skipped"], pending: ["dup", "Already uploaded, skipped"], failed: ["bad", "Didn't upload"],
    demo: ["dup", "Not added: the demo doesn't take recordings"] };
  function row(f) {
    var li = document.createElement("li"); li.className = "up-row up-wait";
    li.innerHTML = '<span class="up-ic"></span><span class="up-name">' + esc(f.name) + '</span><span class="up-st meta">Waiting</span>';
    return li;
  }
  function mark(li, res) {
    var l = LABEL[res.result] || LABEL.audio, st = li.querySelector(".up-st");
    li.className = "up-row up-" + l[0];
    var txt = l[1];
    if (res.result === "known" && res.trash) txt = "Already in Scribe Bot (in Trash), skipped";
    st.innerHTML = esc(txt) + (res.href && res.title ? ': <a href="' + esc(res.href) + '">' + esc(res.title) + "</a>" : "");
  }
  var withAi = false;   // G2: the choice made on the card before uploading
  function send(f, onProg) {
    return new Promise(function (ok) {
      // D-11: the public demo takes no recordings, so the file isn't even sent (the server says the same if it is)
      if (document.documentElement.hasAttribute("data-demo")) { onProg(-1); return ok({ result: "demo" }); }
      var fd = new FormData(); fd.append("file", f, f.name); if (withAi) fd.append("ai", "1");
      xhr = new XMLHttpRequest();
      xhr.open("POST", "/capture/upload"); xhr.setRequestHeader("Accept", "application/json");
      xhr.upload.onprogress = function (e) { if (e.lengthComputable) onProg(e.loaded, e.total); };
      xhr.upload.onload = function () { onProg(-1); };
      xhr.onload = function () {
        var r = null; try { r = JSON.parse(xhr.responseText).results[0]; } catch (_) {}
        ok(xhr.status === 200 && r ? r : { result: "failed" });
      };
      xhr.onerror = xhr.onabort = xhr.ontimeout = function () { ok({ result: "failed" }); };
      xhr.send(fd);
    });
  }
  // G2 (Brian, 2026-11-23): choose up front: Transcribe only (free) or Transcribe + AI notes (with a cost estimate from
  // the files' lengths). The last choice is remembered on this device.
  function lengthOf(f) {
    return new Promise(function (ok) {
      if (!/^audio|^video/.test(f.type || "") && !/\.(m4a|mp3|wav|webm|opus|ogg|aac|flac)$/i.test(f.name)) return ok(0);
      var a = document.createElement("audio"), u = URL.createObjectURL(f), done = function (v) { URL.revokeObjectURL(u); ok(v || 0); };
      a.preload = "metadata"; a.onloadedmetadata = function () { done(isFinite(a.duration) ? a.duration : 0); }; a.onerror = function () { done(0); };
      setTimeout(function () { done(0); }, 4000); a.src = u;
    });
  }
  function choose(files) {
    var audio = files.filter(function (f) { return !/\.(txt|vtt|srt)$/i.test(f.name); });
    if (!audio.length) return Promise.resolve(false);
    var c = el(), last = null; try { last = localStorage.getItem("upChoice"); } catch (_) {}
    c.querySelector(".up-title").textContent = "Upload " + audio.length + " recording" + (audio.length > 1 ? "s" : "");
    c.querySelector(".up-list").innerHTML = ""; c.querySelector(".up-now").textContent = ""; c.classList.add("up-done");
    var acts = c.querySelector(".up-actions");
    acts.innerHTML = '<div class="up-choose" role="radiogroup" aria-label="What to make">' +
      '<button type="button" class="up-opt" data-up-ai="0" role="radio"><i class="qs-radio"></i><span><b>Transcribe only</b><small>Free · Summarize any of them later</small></span></button>' +
      '<button type="button" class="up-opt" data-up-ai="1" role="radio"><i class="qs-radio"></i><span><b>Transcribe + AI notes</b><small class="up-cost">Summary, tasks and decisions · working out the cost…</small></span></button></div>' +
      '<div class="up-btns"><button type="button" class="btn btn-ghost up-cancel">Cancel</button><button type="button" class="btn btn-primary up-go">Upload</button></div>';
    var pick = last === "1" ? "1" : "0";
    function paintPick() { acts.querySelectorAll(".up-opt").forEach(function (b) { var on = b.dataset.upAi === pick; b.classList.toggle("on", on); b.setAttribute("aria-checked", on); }); }
    paintPick();
    Promise.all(audio.map(lengthOf)).then(function (secs) {
      var mins = secs.reduce(function (a, b) { return a + b; }, 0) / 60, known = secs.every(Boolean);
      var cost = Math.max(0.01, mins * 0.0011);
      var el2 = acts.querySelector(".up-cost"); if (el2) el2.textContent = "Summary, tasks and decisions · " + (mins ? (known ? "about $" : "at least $") + cost.toFixed(2) : "about $0.001 a minute");
    });
    return new Promise(function (ok) {
      acts.addEventListener("click", function (e) {
        var o = e.target.closest(".up-opt"); if (o) { pick = o.dataset.upAi; paintPick(); return; }
        if (e.target.closest(".up-cancel")) { ok(null); return; }
        if (e.target.closest(".up-go")) { try { localStorage.setItem("upChoice", pick); } catch (_) {} ok(pick === "1"); }
      });
    });
  }
  function run(files, ai) {
    if (busy || !files.length) return;
    files = Array.prototype.slice.call(files);
    if (ai === undefined) { busy = "choosing"; return choose(files).then(function (v) { busy = false; if (v === null) { close(); return; } run(files, v); }); }
    withAi = !!ai;
    var c = el(), list = c.querySelector(".up-list"), bar = c.querySelector(".up-bar i"), now = c.querySelector(".up-now"),
      title = c.querySelector(".up-title"), acts = c.querySelector(".up-actions");
    list.innerHTML = ""; acts.innerHTML = ""; c.classList.remove("up-done");
    var rows = files.map(function (f) { var li = row(f); list.appendChild(li); return li; });
    var total = files.reduce(function (a, f) { return a + f.size; }, 0) || 1, before = 0, out = [];
    busy = true; stop = false;
    var i = 0, pre = {};
    // Fingerprint each file here first and ask which ones Scribe Bot already has, so duplicates are skipped at once
    // instead of after a full upload (Brian, 2026-11-23). Needs crypto.subtle (https); without it every file is sent.
    function hex(buf) { return Array.prototype.map.call(new Uint8Array(buf), function (x) { return ("0" + x.toString(16)).slice(-2); }).join(""); }
    function precheck() {
      if (!(window.crypto && crypto.subtle)) return Promise.resolve();
      title.textContent = "Checking for duplicates…";
      var hs = [], k = 0;
      return files.reduce(function (p, f) {
        return p.then(function () {
          if (stop || /\.(txt|vtt|srt)$/i.test(f.name) || f.size > 400 * 1048576) { hs.push(null); return; }
          now.textContent = "Checking " + (++k) + " of " + files.length + " · " + f.name;
          return f.arrayBuffer().then(function (b) { return crypto.subtle.digest("SHA-256", b); }).then(function (d) { hs.push(hex(d)); },
            function () { hs.push(null); });
        });
      }, Promise.resolve()).then(function () {
        var ask = hs.filter(Boolean); if (!ask.length) return;
        return fetch("/capture/known", { method: "POST", headers: { "Content-Type": "application/json", Accept: "application/json" },
          body: JSON.stringify({ hashes: ask }) }).then(function (r) { return r.ok ? r.json() : { known: {} }; }).then(function (j) {
          hs.forEach(function (h, n) { if (h && j.known[h]) pre[n] = j.known[h]; });
        });
      }).catch(function () {});
    }
    precheck().then(function () { now.textContent = ""; next(); });
    function next() {
      if (i < files.length && pre[i]) { out.push({ file: files[i], res: pre[i] }); mark(rows[i], pre[i]); before += files[i].size; i++; return next(); }
      if (i >= files.length || stop) return finish();
      var f = files[i], li = rows[i];
      title.textContent = "Uploading " + (i + 1) + " of " + files.length;
      li.className = "up-row up-now-row"; li.querySelector(".up-st").textContent = "Uploading";
      li.scrollIntoView({ block: "nearest" });
      send(f, function (done, size) {
        if (done < 0) { now.textContent = f.name + " · Checking and saving on the M14…"; li.querySelector(".up-st").textContent = "Checking"; return; }
        now.textContent = f.name + " · " + mb(done) + " of " + mb(size) + " (" + Math.round(done / size * 100) + "%)";
        bar.style.width = Math.min(100, (before + done) / total * 100) + "%";
      }).then(function (res) {
        out.push({ file: f, res: res }); mark(li, res);
        before += f.size; bar.style.width = (before / total * 100) + "%";
        i++; next();
      });
    }
    function finish() {
      busy = false; xhr = null;
      rows.slice(out.length).forEach(function (li) { li.querySelector(".up-st").textContent = "Not sent (stopped)"; });
      var n = function (k) { return out.filter(function (o) { return k.indexOf(o.res.result) >= 0; }).length; };
      var added = n(["audio", "older"]), dup = n(["known", "pending"]), bad = n(["failed"]), tx = n(["transcript"]), demo = n(["demo"]);
      var parts = [];
      if (demo) parts.push(demo + " not added (demo)");
      if (added) parts.push(added + " added");
      if (tx) parts.push(tx + " transcript" + (tx > 1 ? "s" : ""));
      if (dup) parts.push(dup + " already in Scribe Bot");
      if (bad) parts.push(bad + " didn't upload");
      title.textContent = demo && !added && !bad ? "Demo: nothing was uploaded" :
        (bad ? "Done, with problems: " : "✓ Done: ") + (parts.join(" · ") || "nothing sent");
      now.textContent = added ? (withAi ? "New recordings show in Talks within a minute; AI notes follow a few minutes after each transcript." : "New recordings show in Talks within a minute, transcript only. Summarize any of them when you like.") :
        dup && !bad ? "Nothing new: Scribe Bot already had these." :
        demo ? "This demo is for looking around. To add recordings, run Scribe Bot on your own computer." : "";
      c.classList.add("up-done");
      acts.innerHTML = (bad ? '<button type="button" class="btn up-retry">Try again (' + bad + ")</button>" : "") +
        (added ? '<a class="btn btn-primary" href="/inbox">Open Inbox</a>' : "") + '<button type="button" class="btn up-close">Close</button>';
      acts.querySelector(".up-close").addEventListener("click", close);
      var rt = acts.querySelector(".up-retry");
      if (rt) rt.addEventListener("click", function () { run(out.filter(function (o) { return o.res.result === "failed"; }).map(function (o) { return o.file; }), withAi); });
    }
  }
  /* Capture › Upload: the form sends through the uploader instead of a page load (a plain post still works without JS) */
  document.addEventListener("submit", function (e) {
    var form = e.target;
    if (!form.matches || !form.matches('form[action="/capture/upload"]')) return;
    var inp = form.querySelector('input[type="file"]');
    if (!inp || !inp.files.length || !window.XMLHttpRequest) return;
    e.preventDefault();
    run(inp.files);
    setTimeout(function () { inp.value = ""; }, 0);
  });
  return run;
})();

/* Drop anywhere (Brian, 2026-11-20: dragging a memo onto the page just opened it in a new tab). Drop audio files
   (Voice Memos, Recorder, anything) and .txt transcripts on any Scribe Bot page: they upload with the progress card
   above (one at a time, each file's result listed). A .txt with the same name as a recording is its transcript; Voice Memos carry theirs inside the file. */
(function () {
  var over = null, depth = 0;
  function hasFiles(e) { return e.dataTransfer && Array.prototype.indexOf.call(e.dataTransfer.types || [], "Files") >= 0; }
  function show(text) {
    if (!over) { over = document.createElement("div"); over.className = "drop-over"; document.body.appendChild(over); }
    over.innerHTML = "<div>" + text + "</div>"; over.hidden = false;
  }
  function hide() { depth = 0; if (over) over.hidden = true; }
  document.addEventListener("dragenter", function (e) { if (!hasFiles(e)) return; depth++; show("⬇︎ Drop recordings and .txt transcripts<br><small>You choose Transcribe only or + AI notes next</small>"); });
  document.addEventListener("dragleave", function (e) { if (hasFiles(e) && --depth <= 0) hide(); });
  document.addEventListener("dragover", function (e) { if (hasFiles(e)) e.preventDefault(); });   // without this the browser opens the file
  document.addEventListener("drop", function (e) {
    if (!hasFiles(e)) return;
    e.preventDefault();
    if (e.target.closest && e.target.closest('input[type="file"]')) { hide(); return; }
    var files = Array.prototype.filter.call(e.dataTransfer.files, function (f) { return /\.(m4a|mp3|wav|webm|opus|ogg|aac|flac|txt)$/i.test(f.name); });
    if (!files.length) { show("Only audio files and .txt transcripts can be added here."); setTimeout(hide, 2500); return; }
    hide(); window.ScribeUpload(files);
  });
})();

/* Contents rail (Brian picked A, 2026-11-20; same as his portfolio): thin ticks on the right edge, one per section;
   the one you're reading turns blue as you scroll. Tap the rail for the outline, or press and slide up/down and let go
   to jump. On a computer, pointing at the rail opens the outline. Pages with a jump list use it; other long pages
   (3+ headed sections) get one automatically. */
(function () {
  var items = [], rail = null, out = null, active = -1, drag = null, sig = "";
  function sideScrolls() { var s = document.querySelector(".conv-side"); return s && getComputedStyle(s).overflowY === "auto" ? s : null; }
  function mmss(s) { s = Math.floor(s); var h = Math.floor(s / 3600), m = Math.floor(s / 60) % 60, x = ("0" + s % 60).slice(-2);
    return h ? h + ":" + ("0" + m).slice(-2) + ":" + x : m + ":" + x; }
  /* Sub ticks inside an open transcript (2026-11-20): one every 5, 10, 15... minutes, at most 8, so a long talk can be
     entered in the middle. Phone layout only: on a computer the transcript has its own scrolling column. */
  /* Only while you're reading the transcript (Brian, 2026-11-20: they go away again in the other sections). */
  function inTx() {
    var tx = document.getElementById("transcript"); if (!tx) return false;
    var r = tx.getBoundingClientRect(), line = Math.min(innerHeight * 0.35, 230);
    return r.top <= line && r.bottom > line;
  }
  var wasInTx = false;
  function txTicks() {
    var tx = document.getElementById("transcript");
    wasInTx = inTx();
    if (!tx || (tx.tagName === "DETAILS" && !tx.open) || sideScrolls() || !wasInTx) return [];
    var lines = tx.querySelectorAll(".line[data-start]"); if (lines.length < 20) return [];
    var end = parseFloat(lines[lines.length - 1].getAttribute("data-start")) || 0, steps = [300, 600, 900, 1200, 1800, 3600], step = 3600;
    for (var k = 0; k < steps.length; k++) if (end / steps[k] <= 8) { step = steps[k]; break; }
    var res = [], next = step;
    for (var i = 0; i < lines.length; i++) {
      var s = parseFloat(lines[i].getAttribute("data-start")) || 0;
      if (s >= next && lines[i].id) { res.push({ id: lines[i].id, label: mmss(s), sub: true }); while (next <= s) next += step; }
    }
    return res.length >= 2 ? res : [];
  }
  /* People A–Z (Brian, 2026-11-21): one tick per letter range (A–D, E–H...); the letters inside a range appear as small
     ticks only while you're in that range, then hide again. */
  var wasRange = -2;
  function letterRanges() {
    var l = document.getElementById("people-list"); if (!l) return null;
    var hs = [].filter.call(l.querySelectorAll(".list-head"), function (h) { return h.offsetParent !== null && /^.$/.test(h.textContent.trim()); });
    if (hs.length < 7) return null;
    var n = Math.min(6, Math.ceil(hs.length / 3)), per = Math.ceil(hs.length / n), rs = [];
    hs.forEach(function (h) { if (!h.id) h.id = "ltr-" + h.textContent.trim().charCodeAt(0); });
    for (var i = 0; i < hs.length; i += per) rs.push(hs.slice(i, i + per));
    return rs;
  }
  function curRange(rs) {
    if (!rs) return -2;
    var line = Math.min(innerHeight * 0.35, 230), cur = -1;
    rs.forEach(function (r, i) { if (r[0].getBoundingClientRect().top <= line) cur = i; });
    return cur;
  }
  function letterItems(rs) {
    var cr = wasRange = curRange(rs), list = [{ id: "content", label: "Top", top: true }];
    rs.forEach(function (r, i) {
      var a = r[0].textContent.trim(), z = r[r.length - 1].textContent.trim();
      list.push({ id: r[0].id, label: a === z ? a : a + "–" + z });
      if (i === cr) r.slice(1).forEach(function (h) { list.push({ id: h.id, label: h.textContent.trim(), sub: true }); });
    });
    return list;
  }
  /* Brian, 2026-11-21: the first outline row says which page you're on ("↑ Inbox") instead of "Top" */
  function topLabel() {
    return "↑ Top";   // Brian, 2026-11-25: always "Top" (a long title was cut with "…", which looked tacky)
  }
  function gather() {
    var g = gather0();
    g.forEach(function (it) { if (it.top) it.label = topLabel(); });
    return g;
  }
  function gather0() {
    if (document.querySelector("#conv.glance-on")) return [];
    var rs = letterRanges(); if (rs) return letterItems(rs);
    wasRange = -2;   // At a glance is one short screen: nothing to jump to
    var src = document.querySelector("nav[data-rail]");
    if (src) {
      var list = [];
      [].forEach.call(src.querySelectorAll("a"), function (a) {
        var id = a.getAttribute("href").slice(1), t = document.getElementById(id);
        if (id !== "content" && t && t.offsetParent === null && !t.closest("details:not([open])")) return;   // hidden on this screen
        list.push({ id: id, label: a.textContent.trim(), top: a.hasAttribute("data-jump-top") });
        if (id === "transcript") list = list.concat(txTicks());
      });
      return list;
    }
    // Headed sections, Today's group titles and list date headings (2026-11-20: Today, Inbox, Talks, People and Tasks
    // all get a rail once they're taller than about a screen and a half).
    var hs = [].filter.call(document.querySelectorAll("#content .section-head h2, #content details.section > summary h2, #content section > h2, " +
      "#content h2.group-title, #content .list-head"), function (h) { return h.offsetParent !== null && h.textContent.trim(); });
    if (hs.length < 2 || document.documentElement.scrollHeight < innerHeight * 1.4) return [];
    return [{ id: "content", label: "Top", top: true }].concat(hs.map(function (h, i) {
      var box = h.classList.contains("list-head") ? h : h.closest("section, details.section, .section-head") || h;
      if (!box.id) box.id = "sec-auto-" + i;
      return { id: box.id, label: h.childNodes[0] && h.childNodes[0].textContent.trim() || h.textContent.trim() };
    }));
  }
  function el(i) { return document.getElementById(items[i].id); }
  function spy() {
    if (!rail) return;
    var line = Math.min(innerHeight * 0.35, 230), cur = 0;   // below the fixed header bar
    var side = sideScrolls();
    items.forEach(function (it, i) {   // wide screens: the transcript column is always beside you, so it doesn't count
      var e = el(i); if (i && e && e.offsetParent !== null && !(side && side.contains(e)) && e.getBoundingClientRect().top <= line) cur = i; });
    var tall = document.documentElement.scrollHeight > innerHeight + 40;
    if (!side && tall && scrollY > 40 && innerHeight + scrollY >= document.documentElement.scrollHeight - 4) {
      for (var j = items.length - 1; j > cur; j--) { var b = el(j); if (b && b.getBoundingClientRect().top < innerHeight) { cur = j; break; } }   // at the very bottom: the last section you can see
    }
    if (inTx() !== wasInTx) { wasInTx = !wasInTx; rebuild(); }
    if (wasRange !== -2 && curRange(letterRanges()) !== wasRange) rebuild();   // moved into another letter range   // entered or left the transcript: add/remove its time ticks
    if (cur === active) return;
    active = cur;
    var par = -1;   // inside a transcript time tick, its section ("Transcript") stays lit too
    if (items[cur] && items[cur].sub) for (var p = cur - 1; p >= 0; p--) if (!items[p].sub && !items[p].top) { par = p; break; }
    [].forEach.call(rail.children, function (t, i) { t.classList.toggle("on", i === cur); t.classList.toggle("par", i === par); });
    [].forEach.call(out.querySelectorAll("a"), function (a, i) { a.classList.toggle("on", i === cur); a.classList.toggle("par", i === par); });
  }
  var frame = 0;
  function schedule() { if (!frame) frame = requestAnimationFrame(function () { frame = 0; spy(); }); }
  var openedAt = 0;
  function open() { if (out.hidden) openedAt = Date.now(); out.hidden = false; rail.setAttribute("aria-expanded", "true"); }
  function close() { out.hidden = true; rail.setAttribute("aria-expanded", "false"); point(-1); }
  function point(i) { [].forEach.call(out.querySelectorAll("a"), function (a, j) { a.classList.toggle("pointed", j === i); }); }
  function rowAt(y) { var as = out.querySelectorAll("a"); for (var i = 0; i < as.length; i++) { var r = as[i].getBoundingClientRect(); if (y >= r.top && y <= r.bottom) return i; } return -1; }
  function jump(i) { close(); var a = out.querySelectorAll("a")[i]; if (a) a.click(); }
  /* Built on every page load and when coming back from the Back cache. Rebuilt when the page's sections change (At a glance <->
     Show everything, the transcript opened or closed), otherwise left alone. */
  function build() {
    var next = gather(), s = next.length >= 3 ? next.map(function (it) { return it.id; }).join(",") : "";
    [].forEach.call(document.querySelectorAll(".rail, .rail-out"), function (x) { if (x !== rail && x !== out) x.remove(); });   // copies from the Back cache
    if (s === sig && (!rail || document.body.contains(rail))) { spy(); return; }
    if (rail) rail.remove(); if (out) out.remove();
    rail = out = null; sig = s; active = -1; items = next;
    if (!s) return;
    rail = document.createElement("button"); rail.type = "button"; rail.className = "rail"; rail.setAttribute("aria-label", "Page outline");
    rail.setAttribute("aria-expanded", "false");
    rail.innerHTML = items.map(function (it) { return '<i class="' + (it.top || it.sub ? "sub" : "") + '"></i>'; }).join("");
    out = document.createElement("nav"); out.className = "rail-out"; out.hidden = true; out.setAttribute("aria-label", "Page outline");
    out.innerHTML = items.map(function (it) { return '<a href="#' + it.id + '"' + (it.top ? " data-jump-top" : "") + (it.sub ? ' class="sub"' : "") + "><i></i>" + it.label.replace(/[<&]/g, "") + "</a>"; }).join("");
    document.body.appendChild(rail); document.body.appendChild(out);
    rail.addEventListener("pointerdown", function (e) {
      if (e.pointerType === "mouse") return;
      e.preventDefault(); open(); drag = { y: e.clientY, t: Date.now(), moved: false };
      try { rail.setPointerCapture(e.pointerId); } catch (_) {}
    });
    rail.addEventListener("pointermove", function (e) {
      if (!drag) return;
      if (Math.abs(e.clientY - drag.y) > 8) drag.moved = true;
      point(rowAt(e.clientY));
    });
    // A quick tap opens the outline under the finger; the phone's follow-up "click" then landed on whichever row
    // appeared there and jumped to it (bug found 2026-11-20). Swallow that click.
    rail.addEventListener("touchend", function (e) { if (e.cancelable) e.preventDefault(); }, { passive: false });
    rail.addEventListener("pointerup", function (e) {
      if (!drag) return;
      var held = drag.moved || Date.now() - drag.t > 400, i = rowAt(e.clientY); drag = null;
      if (held) { if (i >= 0) jump(i); else close(); }   // a quick tap leaves the outline open to pick from
    });
    rail.addEventListener("click", function (e) {
      if (e.detail === 0) { out.hidden ? open() : close(); if (!out.hidden) out.querySelector("a").focus(); }   // keyboard
      else if (matchMedia("(pointer: fine)").matches) open();
    });
    rail.addEventListener("mouseenter", function () { if (matchMedia("(pointer: fine)").matches) open(); });
    out.addEventListener("mouseleave", function () { if (matchMedia("(pointer: fine)").matches) close(); });
    out.addEventListener("click", function (e) {
      var a = e.target.closest("a"); if (!a) return;
      if (e.detail && Date.now() - openedAt < 350 && !matchMedia("(pointer: fine)").matches) { e.preventDefault(); return; }   // the opening tap itself
      var t = document.getElementById(a.getAttribute("href").slice(1));   // a time tick: open the transcript first
      if (t) for (var d = t.parentElement; d; d = d.parentElement) if (d.tagName === "DETAILS") d.open = true;
      setTimeout(close, 0);
    });
    var side = document.querySelector(".conv-side"); if (side) side.addEventListener("scroll", schedule, { passive: true });
    spy();
  }
  document.addEventListener("pointerdown", function (e) { if (out && !out.hidden && !out.contains(e.target) && e.target !== rail && !rail.contains(e.target)) close(); });
  document.addEventListener("keydown", function (e) { if (e.key === "Escape" && out && !out.hidden) { close(); rail.focus(); } });
  window.addEventListener("scroll", schedule, { passive: true });
  var later = 0;
  function rebuild() { clearTimeout(later); later = setTimeout(build, 30); }
  // U16P (Brian, 2026-11-25: "task sheet laggy on phone"): opening a task, a menu or a pop-up doesn't change the page's
  // sections, so it no longer re-measures the whole page while the sheet is rising (that read stalled the animation).
  var POPS = ".fx-sheet, .row-menu, .chip-menu, .menu-panel, .chip-pop, dialog, .task-row, .item-main, .ctx-menu, .pick-bar";
  function fromPage(e) { var t = e.target; if (!t || !t.closest || !t.closest(POPS)) rebuild(); }
  window.addEventListener("resize", rebuild);
  document.addEventListener("toggle", fromPage, true);             // a section or the transcript opened/closed
  document.addEventListener("click", fromPage);                    // At a glance <-> Show everything
  window.addEventListener("hashchange", rebuild);
  document.addEventListener("htmx:afterSettle", rebuild);          // a new page arrived by tapping a link
  document.addEventListener("htmx:historyRestore", rebuild);       // Back button
  document.addEventListener("htmx:beforeHistorySave", function () { if (rail) rail.remove(); if (out) out.remove(); rail = out = null; sig = ""; });
  window.addEventListener("pageshow", rebuild);
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", build); else build();
})();

/* FX-A filter panel: tap outside, Cancel or Esc closes it. */
(function () {
  document.addEventListener("click", function (e) {
    var c = e.target.closest && e.target.closest("[data-fx-close]");
    if (c) { var d = c.closest("details.fx-sheet"); if (d) { e.preventDefault(); d.open = false; } }
  });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") document.querySelectorAll("details.fx-sheet[open]").forEach(function (d) { d.open = false; });
  });
})();

/* E (Brian, 2026-11-20): Talks › Select, tick the talks you want, Export › gives the same 4 choices for just those. */
(function () {
  var picked = {};
  function list() { return document.getElementById("conv-list"); }
  function idOf(item) { var a = item.querySelector('a.item-link[href^="/conversations/"]'); var m = a && /\/conversations\/(\d+)$/.exec(a.getAttribute("href")); return m && m[1]; }
  function n() { return Object.keys(picked).length; }
  function paint() {
    var l = list(), bar = document.getElementById("pick-bar"), on = !!(l && l.classList.contains("picking"));
    if (l) [].forEach.call(l.querySelectorAll(".item"), function (it) { var p = !!picked[idOf(it)]; it.classList.toggle("picked", p);
      var c = it.querySelector("[data-row-cb]"); if (c) c.setAttribute("aria-checked", p ? "true" : "false"); });
    var mc = document.querySelector("[data-pick-all]"); if (mc && l) { var tot = l.querySelectorAll(".item [data-row-cb]").length; mc.setAttribute("aria-checked", n() && n() >= tot ? "true" : n() ? "mixed" : "false"); }
    if (bar) { bar.hidden = !on; bar.querySelector(".pick-n").textContent = n() + " selected"; /* F59: same word as the other select bars */
      [].forEach.call(bar.querySelectorAll("[data-pick-export], [data-pick-act]"), function (x) { x.disabled = !n(); }); }
    var b = document.querySelector("[data-pick-mode]:not(.pk-x)"); if (b) { b.setAttribute("aria-pressed", on ? "true" : "false"); b.textContent = on ? "Done" : "Select"; }
    var head = document.querySelector("[data-export-head]");
    if (head) head.innerHTML = on && n() ? "<b>Export " + n() + " selected talk" + (n() > 1 ? "s" : "") + "</b>" : '<b>Export the talks shown</b> <span class="muted">(up to 100)</span>';
    [].forEach.call(document.querySelectorAll(".fx-more .dl-list a[href]"), function (a) {
      if (!a.dataset.base) a.dataset.base = a.getAttribute("href");
      var base = a.dataset.base;
      a.setAttribute("href", on && n() ? base.replace(/\?.*$/, "") + "?ids=" + Object.keys(picked).join(",") + (/transcript=1/.test(base) ? "&transcript=1" : "") : base);
    });
  }
  var lastIdx = null;
  function fine() { return matchMedia("(hover: hover) and (pointer: fine)").matches; }
  document.addEventListener("click", function (e) {
    if (!e.target.closest) return;
    // B1 / B3 (Brian, 2026-11-23): the checkbox (computers) or the round icon (any screen) picks a talk; Shift picks a range;
    // the last one unticked ends picking (Gmail).
    var cb = e.target.closest("#conv-list .item [data-row-cb], #conv-list .item [data-pick-ic]");
    if (cb) {
      e.preventDefault(); e.stopPropagation();
      var L0 = list(), it0 = cb.closest(".item"), id0 = idOf(it0); if (!id0 || !L0) return;
      var all = [].slice.call(L0.querySelectorAll(".item")), i0 = all.indexOf(it0);
      if (e.shiftKey && lastIdx != null && all[lastIdx]) { var on = !picked[id0];
        all.slice(Math.min(i0, lastIdx), Math.max(i0, lastIdx) + 1).forEach(function (x) { var k = idOf(x); if (k) { if (on) picked[k] = 1; else delete picked[k]; } }); }
      else if (picked[id0]) delete picked[id0]; else picked[id0] = 1;
      lastIdx = i0; L0.classList.toggle("picking", n() > 0); paint(); return;
    }
    // computers: while some are ticked, clicking a talk still opens it (the boxes do the picking)
    if (fine() && e.target.closest("#conv-list.picking .item") && !e.target.closest(".show-more")) return;
    if (e.target.closest("[data-pick-mode]")) { var l = list(); if (l) { l.classList.toggle("picking"); if (!l.classList.contains("picking")) picked = {}; } paint(); return; }
    if (e.target.closest("[data-pick-all]")) { var l2 = list(); if (l2) { var its = l2.querySelectorAll(".item"), every = n() && n() >= [].filter.call(its, idOf).length;
      if (every) { picked = {}; if (fine()) l2.classList.remove("picking"); } else [].forEach.call(its, function (it) { var i = idOf(it); if (i) picked[i] = 1; }); } paint(); return; }
    if (e.target.closest("[data-pick-export]")) { var d = document.querySelector("details.fx-more"); if (d) d.open = true; return; }
    var act = e.target.closest("[data-pick-act]");
    if (act && n()) {
      var ids = Object.keys(picked).join(",");
      if (act.dataset.pickAct === "ai") { location.href = "/talks-bulk/ai?ids=" + ids; return; }   // the cost first
      var lab = act.querySelector("span") || act;   // the label next to the icon
      if (act.dataset.pickAct === "merge" && n() < 2) { lab.textContent = "Pick 2+"; setTimeout(function () { lab.textContent = "Merge"; }, 1500); return; }
      act.disabled = true; if (act.dataset.pickAct === "merge") lab.textContent = "Merging.";
      var f = new FormData(); f.append("ids", ids); f.append("what", act.dataset.pickAct);
      fetch("/talks-bulk", { method: "POST", body: f, credentials: "same-origin" }).then(function (r) {
        if (!r.ok) return r.json().catch(function () { return {}; }).then(function (j) { throw new Error(j.detail || window.sbFail(r.status)); });
        location.href = act.dataset.pickAct === "merge" ? r.url : "/conversations";   // merged: open the new talk
      }).catch(function (err) { act.disabled = false; lab.textContent = "Try again"; window.sbNote(err instanceof TypeError ? window.sbFail(0) : err.message); });
      return;
    }
    if (Date.now() - heldAt < 700 && heldIt && e.target.closest("#conv-list .item") === heldIt) { heldIt = null; e.preventDefault(); e.stopPropagation(); return; }   // the long-press's own lift
    var it = e.target.closest("#conv-list.picking .item");
    if (it && !e.target.closest(".show-more")) {
      e.preventDefault(); e.stopPropagation();
      var id = idOf(it); if (id) { if (picked[id]) delete picked[id]; else picked[id] = 1; }
      paint(); return;
    }
    // Brian, 2026-11-22: a tap anywhere that isn't a talk, the bar or the Export sheet stops picking (like every list)
    var L = list();
    if (L && L.classList.contains("picking") && !e.target.closest("#conv-list .item, #pick-bar, .fx-sheet[open] .fx-body, .undo-bar, .show-more")) {
      e.preventDefault(); e.stopPropagation(); L.classList.remove("picking"); picked = {}; paint();
    }
  }, true);
  document.addEventListener("keydown", function (e) { var L = list(); if (e.key === "Escape" && L && L.classList.contains("picking")) { L.classList.remove("picking"); picked = {}; paint(); } });
  // Long-press a talk to start picking (Brian, 2026-11-22: "unable to click and hold and select them"), like the Inbox
  var held = null, heldAt = 0, heldIt = null;
  document.addEventListener("pointerdown", function (e) {
    var it = e.target.closest && e.target.closest("#conv-list .item"), l = list();
    if (!it || !l || l.classList.contains("picking") || e.pointerType === "mouse") return;
    held = setTimeout(function () {
      held = null; heldAt = Date.now(); heldIt = it; l.classList.add("picking"); picked = {};
      var id = idOf(it); if (id) picked[id] = 1; paint();
    }, 480);
    held0 = [e.clientX, e.clientY];
  });
  var held0 = [0, 0];   // moving 8px any way (a swipe or a scroll) cancels the long-press (2026-11-22)
  ["pointerup", "pointercancel", "pointermove"].forEach(function (ev) { document.addEventListener(ev, function (e) {
    if (held && (ev !== "pointermove" || Math.hypot(e.clientX - held0[0], e.clientY - held0[1]) > 8)) { clearTimeout(held); held = null; } }); });
  document.addEventListener("contextmenu", function (e) { if (e.target.closest && e.target.closest("#conv-list .item")) e.preventDefault(); });
  document.addEventListener("htmx:afterSettle", function () {   // "Show more" swaps the list in: keep the ticks
    var l = list(), bar = document.getElementById("pick-bar");
    if (l && bar && !bar.hidden && !l.classList.contains("picking")) l.classList.add("picking");
    if (!l) picked = {};
    paint();
  });
})();

/* Audio starts faster (Brian, 2026-11-21): once the transcript is open, the player fetches the file's index in the
   background, so the first tap only needs the audio itself; the phone also keeps the file cached for a day. */
(function () {
  function warm() { var a = document.querySelector("audio[data-warm]"), tx = document.getElementById("transcript");
    if (a && a.preload === "none" && (!tx || tx.open || tx.tagName !== "DETAILS")) { a.preload = "metadata"; try { a.load(); } catch (_) {} } }
  document.addEventListener("toggle", function (e) { if (e.target.id === "transcript" && e.target.open) warm(); }, true);
  document.addEventListener("pointerdown", function (e) { if (e.target.closest && e.target.closest(".lines")) warm(); }, true);
  document.addEventListener("htmx:afterSettle", warm);
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", warm); else warm();
})();

/* Inbox (redesign, 2026-11-21): ✦ opens the row's sheet with the cost to confirm; long-press a row (or Select) to pick
   several, then Transcript only or Skip for all of them. */
(function () {
  var picking = false, picked = {}, held = null, heldAt = 0, lastCb = null;
  function bar() { return document.getElementById("in-pick"); }
  function paint() {
    var b = bar(); if (!b) return;
    b.hidden = !picking; b.querySelector(".pick-n").textContent = Object.keys(picked).length + " selected";
    var l = document.getElementById("inbox-list"); if (l) l.classList.toggle("picking", picking);
    var rows = document.querySelectorAll("#inbox-list .in-row"), n = Object.keys(picked).length;
    rows.forEach(function (r) { var on = !!picked[r.dataset.pickId]; r.classList.toggle("picked", on); var cb = r.querySelector("[data-in-cb]"); if (cb) cb.setAttribute("aria-checked", on); });
    var all = b.querySelector(".pk-all-in"); if (all) all.setAttribute("aria-checked", n && n === rows.length ? "true" : n ? "mixed" : "false");
    var sb = document.querySelector('[data-inpick="start"]'); if (sb) sb.textContent = picking ? "Done" : "Select";
    // F60 (screen pass): with nothing selected the actions looked ready but did nothing; dimmed like Talks' bar
    b.querySelectorAll(".pk-bot button").forEach(function (x) { x.disabled = !n; });
  }
  function start() { picking = true; picked = {}; paint(); }
  document.addEventListener("click", function (e) {
    if (!e.target.closest) return;
    // Computers (Brian, 2026-11-23): a checkbox on each row, like Talks. Ticking one starts picking, unticking the last
    // stops it; Shift-click ticks the rows in between.
    var cb = e.target.closest("[data-in-cb]");
    if (cb) {
      e.preventDefault(); e.stopPropagation();
      var r0 = cb.closest(".in-row"), id0 = r0 && r0.dataset.pickId; if (!id0) return;
      var on = !picked[id0], list = [].slice.call(document.querySelectorAll("#inbox-list .in-row:not([hidden])"));
      if (e.shiftKey && lastCb && picked[lastCb]) {
        var i = list.findIndex(function (r) { return r.dataset.pickId === lastCb; }), j = list.indexOf(r0);
        if (i >= 0) list.slice(Math.min(i, j), Math.max(i, j) + 1).forEach(function (r) { picked[r.dataset.pickId] = 1; });
      } else if (on) picked[id0] = 1; else delete picked[id0];
      lastCb = id0; picking = Object.keys(picked).length > 0; paint(); return;
    }
    var b = e.target.closest("[data-inpick]");
    if (b) {
      var what = b.dataset.inpick;
      if (what === "start") { if (picking) { picking = false; picked = {}; paint(); } else start(); return; }
      if (what === "done") { picking = false; picked = {}; paint(); return; }
      if (what === "all") { var rs = document.querySelectorAll("#inbox-list .in-row:not([hidden])");
        if (rs.length && Object.keys(picked).length >= rs.length) { picked = {}; if (matchMedia("(hover: hover) and (pointer: fine) and (min-width: 960px)").matches) picking = false; }
        else rs.forEach(function (r) { picked[r.dataset.pickId] = 1; });
        paint(); return; }
      var ids = Object.keys(picked); if (!ids.length) return;
      if (what === "merge") {
        var lb = b.querySelector(".pk-l") || b;
        if (ids.length < 2) { lb.textContent = "Pick 2+"; setTimeout(function () { lb.textContent = "Merge"; }, 1500); return; }
        // Brian, 2026-11-21: "merging takes a while". The list changes at once (the picked rows fold into one "Merging…"
        // row) and the joining carries on in the background; the new recording drops in when it's ready. Leaving the page
        // doesn't stop it (the M14 finishes the merge either way).
        var rows = ids.map(function (id) { return document.getElementById("rec-" + id); }).filter(Boolean);
        var names = rows.map(function (r) { var t = r.querySelector(".r1-t"); return t && t.firstChild ? t.firstChild.textContent.trim() : ""; }).filter(Boolean);
        var ph = document.createElement("div"); ph.className = "item in-merging"; ph.setAttribute("aria-live", "polite");
        ph.innerHTML = '<span class="ib spin" aria-hidden="true"><svg class="icon" aria-hidden="true"><use href="#i-merge"/></svg></span><div class="item-main"><div class="item-title"></div><div class="item-sub"><span>Merging in the background</span></div></div>';
        ph.querySelector(".item-title").textContent = names.join(" + ");
        if (rows[0]) rows[0].before(ph);
        rows.forEach(function (r) { r.hidden = true; });
        picking = false; picked = {}; paint();
        var mf = new FormData(); mf.append("ids", ids.join(","));
        fetch("/inbox/merge", { method: "POST", body: mf, credentials: "same-origin" }).then(function (r) {
          if (!r.ok) return r.json().catch(function () { return {}; }).then(function (j) { throw new Error(j.detail || window.sbFail(r.status)); });
          return fetch(r.url, { credentials: "same-origin" }).then(function (x) { return x.text(); }).then(function (html) {
            var doc = new DOMParser().parseFromString(html, "text/html"), fresh = doc.getElementById("inbox-list"), cur = document.getElementById("inbox-list");
            if (fresh && cur) { cur.replaceWith(fresh); if (window.htmx) htmx.process(fresh); }
            var nid = (r.url.split("#rec-")[1] || ""), nr = nid && document.getElementById("rec-" + nid);
            if (nr) { nr.classList.add("jump-glow"); nr.scrollIntoView({ block: "center", behavior: "smooth" }); }
          });
        }).catch(function (err) {
          ph.remove(); rows.forEach(function (r) { r.hidden = false; });
          window.sbNote(err instanceof TypeError ? window.sbFail(0) : err.message);
        });
        return;
      }
      // R5-24: Summarize shows the total cost first (it used to spend straight away)
      if (what === "ai") { location.href = "/inbox-bulk/ai?ids=" + ids.join(","); return; }
      // one request for the lot; older recordings are got ready on the way (2026-11-22: they used to be skipped silently)
      b.disabled = true; var bl = b.querySelector(".pk-l"); if (bl) bl.textContent = "Working.";
      var bf = new FormData(); bf.append("ids", ids.join(",")); bf.append("what", what);
      fetch("/inbox/bulk", { method: "POST", body: bf, credentials: "same-origin" }).then(function (r) {
        if (!r.ok) throw new Error("bulk " + r.status);
        location.reload();
      }).catch(function () { b.disabled = false; if (bl) bl.textContent = "Try again"; });
      return;
    }
    var row = picking && !matchMedia("(hover: hover) and (pointer: fine) and (min-width: 960px)").matches && e.target.closest("#inbox-list .in-row > summary");
    if (row && Date.now() - heldAt < 700) { e.preventDefault(); e.stopPropagation(); return; }   // the long-press's own lift
    if (row) { e.preventDefault(); e.stopPropagation(); var id = row.parentElement.dataset.pickId; if (id) { if (picked[id]) delete picked[id]; else picked[id] = 1; } paint(); return; }
    // Brian, 2026-11-21: easy way out: a tap anywhere that isn't a row or the bar stops picking
    if (picking && !e.target.closest("#in-pick, .fx-sheet[open] .fx-body, .undo-bar")) { e.preventDefault(); e.stopPropagation(); picking = false; picked = {}; paint(); }
  }, true);
  document.addEventListener("keydown", function (e) { if (picking && e.key === "Escape") { picking = false; picked = {}; paint(); } });
  document.addEventListener("pointerdown", function (e) {
    var r = e.target.closest && e.target.closest("#inbox-list .in-row > summary");
    if (!r || picking || e.pointerType === "mouse") return;
    held = setTimeout(function () { held = null; heldAt = Date.now(); start(); var id = r.parentElement.dataset.pickId; if (id) picked[id] = 1; paint(); }, 480);
    held0 = [e.clientX, e.clientY];
  });
  // a finger that moves 8px any way (scroll OR a sideways swipe) is not a long-press (2026-11-22: a slow swipe selected)
  var held0 = [0, 0];
  ["pointerup", "pointercancel", "pointermove"].forEach(function (ev) { document.addEventListener(ev, function (e) { if (held && (ev !== "pointermove" || Math.hypot(e.clientX - held0[0], e.clientY - held0[1]) > 8)) { clearTimeout(held); held = null; } }); });
  document.addEventListener("contextmenu", function (e) { if (e.target.closest && e.target.closest("#inbox-list .in-row")) e.preventDefault(); });
  document.addEventListener("htmx:afterSettle", function () { if (!bar()) { picking = false; picked = {}; } });
})();

/* Inbox sheet: the Type pick applies to "Transcript only" too. */
document.addEventListener("change", function (e) {
  if (!e.target.matches || !e.target.matches(".sh-select")) return;
  document.querySelectorAll('[data-type-copy="' + e.target.id + '"]').forEach(function (c) { c.value = e.target.value; });
});

/* Bottom sheets close with a swipe down (Brian, 2026-11-21: the grab bar looked swipeable but wasn't). Drag the grab bar
   or the sheet's top (or anywhere once the sheet is scrolled to its top) down past a third of its height, or flick. */
(function () {
  var d = null;
  function body(t) { return t.closest && t.closest("details.fx-sheet[open] > .fx-body"); }
  document.addEventListener("touchstart", function (e) {
    var b = body(e.target); if (!b || e.touches.length !== 1 || matchMedia("(min-width: 960px)").matches) return;
    if (e.target.closest("input, select, textarea")) return;
    var onTop = e.target.closest(".fx-grab, .sh-head, .fx-head") || b.scrollTop <= 0;
    if (!onTop) return;
    d = { b: b, y: e.touches[0].clientY, t: Date.now(), dy: 0 };
  }, { passive: true });
  document.addEventListener("touchmove", function (e) {
    if (!d) return;
    d.dy = e.touches[0].clientY - d.y;
    if (d.dy <= 0) { d.b.style.transform = ""; return; }
    if (d.b.scrollTop > 0) { d = null; return; }
    e.preventDefault();
    d.b.style.transition = "none"; d.b.style.transform = "translateY(" + d.dy + "px)";
  }, { passive: false });
  document.addEventListener("touchend", function () {
    if (!d) return;
    var b = d.b, fast = d.dy > 40 && Date.now() - d.t < 250, far = d.dy > b.offsetHeight / 3;
    b.style.transition = "transform .18s ease-out";
    if (fast || far) {
      b.style.transform = "translateY(100%)";
      setTimeout(function () { var s = b.closest("details"); if (s) s.open = false; b.style.transform = ""; b.style.transition = ""; }, 180);
    } else { b.style.transform = ""; }
    d = null;
  });
})();

/* Select several (Brian, 2026-11-21, "like selecting emails in Gmail"), for any list with [data-picker] whose rows have
   data-pick-id. Start: long-press a row or a [data-pk-start=<list id>] button. Tap rows to tick. Stop: ✕, Esc, or a tap
   anywhere that isn't a row or the bar. Bar buttons: data-pk-post=<url> (POSTs ids=1,2,3, optional data-pk-confirm) or
   data-pk-go=<url> (opens url?ids=1,2,3); data-pk-min = how many it needs. */
(function () {
  var on = null, picked = {}, held = null, heldAt = 0, lastCb = null;
  function desk() { return matchMedia("(hover: hover) and (pointer: fine) and (min-width: 960px)").matches; }
  function list() { return on && document.getElementById(on); }
  function bar() { return on && document.querySelector('[data-pk-bar="' + on + '"]'); }
  function paint() {
    document.querySelectorAll("[data-picker]").forEach(function (l) {
      var me = l.id === on; l.classList.toggle("picking", me);
      l.querySelectorAll("[data-pick-id]").forEach(function (r) { var y = me && !!picked[r.dataset.pickId]; r.classList.toggle("picked", y);
        var c = r.querySelector("[data-pk-cb]"); if (c) c.setAttribute("aria-checked", y); });
      var all = document.querySelector('[data-pk-bar="' + l.id + '"] [data-pk-all]'), n = me ? Object.keys(picked).length : 0, t = l.querySelectorAll("[data-pick-id]").length;
      if (all) all.setAttribute("aria-checked", n && n >= t ? "true" : n ? "mixed" : "false");
    });
    document.querySelectorAll("[data-pk-bar]").forEach(function (b) {
      b.hidden = b.dataset.pkBar !== on;
      var n = b.querySelector(".pick-n"); if (n) n.textContent = Object.keys(picked).length + " selected";
      // F60 (screen pass): People, Tasks and Trash dim their actions at 0 selected too, like Talks and Inbox
      var k = Object.keys(picked).length; b.querySelectorAll(".pk-bot button").forEach(function (x) { x.disabled = !k; });
    });
    document.querySelectorAll("[data-pk-start]").forEach(function (b) { b.textContent = b.dataset.pkStart === on ? "Done" : "Select"; });
  }
  function start(id) { on = id; picked = {}; paint(); }
  function stop() { on = null; picked = {}; paint(); }
  window.ScribePick = { stop: stop, active: function () { return !!on; } };
  document.addEventListener("click", function (e) {
    if (!e.target.closest) return;
    var s = e.target.closest("[data-pk-start]");
    if (s) { e.preventDefault(); if (on === s.dataset.pkStart) stop(); else start(s.dataset.pkStart); return; }
    // Computers (Brian, 2026-11-23): a checkbox on each row, like Talks and Inbox. The first tick starts picking, the last
    // untick stops it, Shift-click ticks the rows in between; clicking the row itself still opens it.
    var cb = e.target.closest("[data-pk-cb]");
    if (cb) {
      e.preventDefault(); e.stopPropagation();
      var r0 = cb.closest("[data-pick-id]"), L0 = cb.closest("[data-picker]"); if (!r0 || !L0) return;
      if (on !== L0.id) { on = L0.id; picked = {}; }
      var id0 = r0.dataset.pickId, rows = [].slice.call(L0.querySelectorAll("[data-pick-id]"));
      if (e.shiftKey && lastCb && picked[lastCb]) { var i = rows.findIndex(function (r) { return r.dataset.pickId === lastCb; }), j = rows.indexOf(r0);
        if (i >= 0) rows.slice(Math.min(i, j), Math.max(i, j) + 1).forEach(function (r) { picked[r.dataset.pickId] = 1; }); }
      else if (picked[id0]) delete picked[id0]; else picked[id0] = 1;
      lastCb = id0; if (!Object.keys(picked).length) on = null; paint(); return;
    }
    if (!on) return;
    if (e.target.closest("[data-pk-done]")) { stop(); return; }
    if (e.target.closest("[data-pk-all]")) { var L = list(); if (L) { var rs = L.querySelectorAll("[data-pick-id]");
      if (rs.length && Object.keys(picked).length >= rs.length) { picked = {}; if (desk()) on = null; } else rs.forEach(function (r) { picked[r.dataset.pickId] = 1; }); } paint(); return; }
    var b = e.target.closest("[data-pk-bar] button");
    if (b) {
      var ids = Object.keys(picked), min = parseInt(b.dataset.pkMin || "1", 10);
      var lb = b.querySelector(".pk-l") || b;
      if (ids.length < min) { var t = lb.textContent; lb.textContent = "Pick " + min + "+"; setTimeout(function () { lb.textContent = t; }, 1400); return; }
      if (b.dataset.pkGo) { location.href = b.dataset.pkGo + "?ids=" + ids.join(","); return; }
      if (b.dataset.pkPost) {
        var send = function () {
          b.disabled = true; var f = new FormData(); f.append("ids", ids.join(","));
          fetch(b.dataset.pkPost, { method: "POST", body: f, credentials: "same-origin" }).then(function (r) {
            if (!r.ok) return r.json().catch(function () { return {}; }).then(function (j) { throw new Error(j.detail || window.sbFail(r.status)); });
            location.href = r.url || location.href;
          }).catch(function (err) { b.disabled = false; window.sbNote(err instanceof TypeError ? window.sbFail(0) : err.message); });   // R5-13
        };
        if (b.dataset.pkConfirm) window.sbConfirm(b.dataset.pkConfirm, (b.querySelector(".pk-l") || b).textContent.trim()).then(function (ok) { if (ok) send(); });
        else send();
      }
      return;
    }
    var row = e.target.closest("#" + on + " [data-pick-id]");
    if (row && desk()) return;   // computers: the row opens; only its checkbox picks
    if (row) {
      e.preventDefault(); e.stopPropagation();
      if (Date.now() - heldAt < 700) return;   // the long-press's own lift
      var id = row.dataset.pickId; if (picked[id]) delete picked[id]; else picked[id] = 1; paint(); return;
    }
    if (!e.target.closest("[data-pk-bar], .fx-sheet[open], .undo-bar, .tabbar, .topbar")) { e.preventDefault(); e.stopPropagation(); stop(); }
  }, true);
  document.addEventListener("keydown", function (e) { if (on && e.key === "Escape") stop(); });
  document.addEventListener("pointerdown", function (e) {
    var r = e.target.closest && e.target.closest("[data-picker] [data-pick-id]");
    if (!r || on || e.pointerType === "mouse") return;
    var l = r.closest("[data-picker]");
    held = setTimeout(function () { held = null; heldAt = Date.now(); start(l.id); picked[r.dataset.pickId] = 1; paint(); }, 480);
  });
  ["pointerup", "pointercancel", "pointermove"].forEach(function (ev) { document.addEventListener(ev, function (e) { if (held && (ev !== "pointermove" || Math.abs(e.movementY) > 4)) { clearTimeout(held); held = null; } }); });
  document.addEventListener("contextmenu", function (e) { if (e.target.closest && e.target.closest("[data-picker] [data-pick-id]")) e.preventDefault(); });
  document.addEventListener("htmx:afterSettle", function () { if (on && !list()) { on = null; picked = {}; } paint(); });
})();

/* Inbox sheet: See more / Hide on the preview (Brian, 2026-11-21; R5-26: fold labels say Hide, never "less"). */
document.addEventListener("click", function (e) {
  var b = e.target.closest && e.target.closest("[data-prev-toggle]"); if (!b) return;
  var w = b.closest(".sh-prev-wrap"), more = w.querySelector("[data-more]"), less = w.querySelector("[data-less]");
  var open = more.hidden; more.hidden = !open; less.hidden = open;
  b.textContent = open ? "Hide" : "See more" + (b.dataset.peek || "");
});

/* Tap a task's title: open its sheet (Brian, 2026-11-21). */
document.addEventListener("click", function (e) {
  var t = e.target.closest && e.target.closest("[data-open-sheet]"); if (!t || e.target.closest("a, button, form")) return;
  if (window.ScribePick && window.ScribePick.active() && !matchMedia("(hover: hover) and (pointer: fine) and (min-width: 960px)").matches) return;   // phones: a tap picks
  var d = document.getElementById(t.dataset.openSheet); if (d) { e.preventDefault(); d.open = true; }
});

/* "+N more" / "Hide" speakers on a talk (2026-11-21). */
document.addEventListener("click", function (e) {
  var b = e.target.closest && e.target.closest("[data-sp-more]"); if (!b) return;
  var on = b.closest(".legend").classList.toggle("show-all");
  b.textContent = on ? "Hide" : "+" + b.dataset.n + " more";
});

/* Tasks (Brian, 2026-11-21): long-press a task also opens its sheet (the ⋯ is gone). */
(function () {
  var held = null, at = 0, x0 = 0, y0 = 0;
  document.addEventListener("pointerdown", function (e) {
    var r = e.target.closest && e.target.closest(".task-row [data-open-sheet]");
    // N4 (2026-11-25): in a list you can pick from, a long-press starts picking (like Inbox and People); a tap opens it
    if (!r || e.pointerType === "mouse" || r.closest("[data-picker]") || (window.ScribePick && window.ScribePick.active())) return;
    x0 = e.clientX; y0 = e.clientY;
    held = setTimeout(function () { held = null; at = Date.now(); var d = document.getElementById(r.dataset.openSheet); if (d) d.open = true; }, 480);
  });
  document.addEventListener("pointermove", function (e) { if (held && (Math.abs(e.clientX - x0) > 8 || Math.abs(e.clientY - y0) > 8)) { clearTimeout(held); held = null; } });
  ["pointerup", "pointercancel"].forEach(function (ev) { document.addEventListener(ev, function () { if (held) { clearTimeout(held); held = null; } }); });
  document.addEventListener("click", function (e) { if (Date.now() - at < 700 && e.target.closest && e.target.closest(".task-row")) { e.preventDefault(); e.stopPropagation(); } }, true);
  document.addEventListener("contextmenu", function (e) { if (e.target.closest && e.target.closest(".task-row [data-open-sheet]")) e.preventDefault(); });
})();

/* M3 A: Inbox length sliders and number boxes move together; a slider left at its end means "no limit". */
document.addEventListener("input", function (e) {
  var t = e.target; if (!t.closest || !t.closest("[data-range]")) return;
  var box = t.closest("[data-range]");
  if (t.type === "range") {
    var n = box.querySelector('input[name="' + t.dataset.for + '"]');
    var atEnd = t.dataset.for === "min_m" ? t.value === t.min : t.value === t.max;
    n.value = atEnd ? "" : t.value;
  } else {
    var s = box.querySelector('input[data-for="' + t.name + '"]'); if (s) s.value = t.value || (t.name === "min_m" ? s.min : s.max);
  }
});

/* Short / Detailed notes switch on a talk (Brian, 2026-11-20). */
document.addEventListener("click", function (e) {
  var b = e.target.closest && e.target.closest("[data-notes]"); if (!b) return;
  var card = b.closest("section") || b.closest(".card"), d = b.dataset.notes === "detailed";
  b.parentNode.querySelectorAll("[data-notes]").forEach(function (x) { x.classList.toggle("on", x === b); });
  var s = card.querySelector(".notes-short"), dt = card.querySelector(".notes-detailed");
  if (s) s.hidden = d; if (dt) dt.hidden = !d;
  // Scrolled down inside the summary (tabs pinned)? Start the other one at its top (Brian, 2026-11-23)
  var head = b.closest(".sum-head"), pin = head ? parseFloat(getComputedStyle(head).top) || 0 : 0;
  if (head && card.getBoundingClientRect().top < pin - 2) scrollTo({ top: card.getBoundingClientRect().top + scrollY - pin, behavior: "instant" });
});

/* One-tap pickers (Brian, 2026-11-21: "too many extra clicks"). A row marked data-quick-pick opens its date / date-and-time
   picker straight away; any data-autosave field saves the moment you choose (no Save / OK buttons). A person list's
   "＋ Someone new…" shows a name box instead: Enter or tapping away saves it. Sheets marked data-keep-open stay open
   after the save so you can change the next thing. */
(function () {
  function fill(sel) {
    var tpl = document.getElementById("people-options");
    if (!tpl || sel.dataset.filled) return;
    sel.appendChild(tpl.content.cloneNode(true));
    if (sel.hasAttribute("data-autosave")) { var o = document.createElement("option"); o.value = "__new"; o.textContent = "＋ Someone new…"; sel.appendChild(o); }
    sel.value = sel.dataset.selected || ""; sel.dataset.filled = "1";
  }
  ["pointerdown", "focusin"].forEach(function (ev) {
    document.addEventListener(ev, function (e) { var s = e.target.closest && e.target.closest("select[data-people]"); if (s) fill(s); }, true);
  });
  document.addEventListener("toggle", function (e) {   // the "＋ Someone new…" option for lists filled by the older code
    if (!e.target.open || !e.target.querySelectorAll) return;
    e.target.querySelectorAll("select[data-people][data-autosave]").forEach(function (s) {
      if (!s.querySelector('option[value="__new"]')) { var o = document.createElement("option"); o.value = "__new"; o.textContent = "＋ Someone new…"; s.appendChild(o); }
    });
  }, true);
  document.addEventListener("click", function (e) {
    var row = e.target.closest && e.target.closest("[data-quick-pick]");
    if (!row) return;
    var inp = row.querySelector('input[type="date"], input[type="datetime-local"], input[type="time"]');
    if (!inp) return;
    e.preventDefault();
    try { inp.showPicker(); } catch (_) { inp.focus(); inp.click(); }
  });
  function save(f) { if (f && !f.dataset.saving) { f.dataset.saving = "1"; var b = f.querySelector("[data-qp-submit]"); if (b) f.requestSubmit(b); else f.requestSubmit(); setTimeout(function () { delete f.dataset.saving; }, 800); } }
  document.addEventListener("change", function (e) {
    var t = e.target; if (!t.matches || !t.matches("[data-autosave]")) return;
    if (t.tagName === "SELECT" && t.value === "__new") {
      var n = t.form.querySelector(".qp-new"); if (n) { n.hidden = false; n.focus(); }
      return;
    }
    if (t.tagName === "SELECT" && (t.value === "__not" || t.value === "__idk" || t.value === "__idk_rest")) {   // "Not X" / "Don't know" (2026-11-22) / "...or the rest" (B11)
      var sp = t.form.querySelector('input[name="special"]'); if (sp) sp.value = t.value.slice(2);
      t.value = ""; save(t.form); return;
    }
    if (t.tagName === "SELECT" && !t.value) return;
    var n2 = t.form.querySelector(".qp-new"); if (n2) n2.value = "";
    save(t.form);
  });
  document.addEventListener("focusout", function (e) {   // a typed new name saves when you tap away
    var n = e.target; if (!n.matches || !n.matches(".qp-new") || !n.value.trim()) return;
    if (!n.isConnected || !n.form || n.form.dataset.saving || n.form.classList.contains("htmx-request")) return;   // Enter already saved it
    var s = n.form.querySelector("select[data-people], select[data-autosave]"); if (s && s.value === "__new") s.value = "";
    save(n.form);
  });
  document.addEventListener("submit", function (e) {   // a new name wins over the list
    var n = e.target.querySelector && e.target.querySelector(".qp-new");
    // any person list (not only [data-people] ones: a talk's speaker list sent "__new" and got error 422, 2026-11-22)
    var sel = e.target.querySelector && e.target.querySelector('select[data-people], select[data-autosave]');
    if (n && n.value.trim()) { var f0 = e.target; f0.dataset.saving = "1"; setTimeout(function () { delete f0.dataset.saving; }, 800); }
    if (sel && sel.value !== "__new") sel = null;
    if (n && n.value.trim()) { if (sel) sel.value = ""; }
    else if (sel) { e.preventDefault(); e.stopImmediatePropagation(); n.hidden = false; n.focus(); }
  }, true);
  var reopen = null;
  document.addEventListener("htmx:beforeRequest", function (e) {
    var el = e.target, keep = el.closest && el.closest("[data-keep-open], form.inline-edit");
    var sh = keep && keep.closest("details.fx-sheet[open]");
    reopen = sh && sh.id ? sh.id : null;
  });
  document.addEventListener("htmx:afterSettle", function () {
    if (!reopen) return;
    var d = document.getElementById(reopen); reopen = null;
    if (d) d.open = true;
  });
  // Tap (not only double-tap) a title marked data-edit-tap to edit it (the task pop-up's title, Brian 2026-11-21).
  document.addEventListener("click", function (e) {
    var el = e.target.closest && e.target.closest("[data-edit-tap]");
    if (el && !el.hidden && window.ScribeEdit) { e.preventDefault(); window.ScribeEdit(el); }
  });
  // Double-tap a task in a list: its pop-up opens with the title ready to edit. The first tap opens the pop-up, so the
  // second lands on its dimmed backdrop: within 450 ms that's the double-tap (and it no longer closes the pop-up again).
  var opened = { at: 0, id: null };
  document.addEventListener("click", function (e) {
    var r = e.target.closest && e.target.closest(".task-row [data-open-sheet]");
    if (r && !e.target.closest("a, button, form")) { opened.at = Date.now(); opened.id = r.dataset.openSheet; }
  }, true);
  document.addEventListener("click", function (e) {
    var back = e.target.closest && e.target.closest(".fx-back");
    if (!back || Date.now() - opened.at > 450) return;
    var d = back.closest("details.fx-sheet"); if (!d || d.id !== opened.id) return;
    e.preventDefault(); e.stopImmediatePropagation(); opened.at = 0;
    var t = d.querySelector("[data-edit-tap]"); if (t && !t.hidden && window.ScribeEdit) window.ScribeEdit(t);
  }, true);
})();

/* Brian (2026-11-21): (1) while any pop-up, sheet, menu or dialog is open, the page behind it doesn't scroll;
   (2) no pop-up may hang off the side of the screen: if one would, it's nudged back in (8px from the edge). */
(function () {
  var OPEN = "details.fx-sheet[open], details.row-menu[open], details.chip-menu[open], dialog[open]";
  var POP = ".chip-pop, .menu-panel, .fx-body";
  // R4-04 (Brian, 2026-11-25): a sheet nudged in as a pop-up kept that nudge (and its height cap) after the layout
  // was switched to Right or Below, so the pane opened mid-screen. In the pane, nothing of the pop-up's fit is kept.
  function unfit(p) { p.style.translate = ""; p.style.maxHeight = ""; p.style.overflowY = ""; delete p.dataset.fitY; p.classList.remove("flip-up", "flip-down"); }
  function fit(d) {
    if (inPane(d)) { [].forEach.call(d.querySelectorAll(POP), unfit); return; }
    [].forEach.call(d.querySelectorAll(POP), function (p) {
      if (p.closest("details:not([open])")) return;
      p.style.translate = "";
      var b = p.getBoundingClientRect(), W = document.documentElement.clientWidth;
      if (!b.width || b.width >= W - 16) return;          // full-width phone sheets are already on screen
      var dx = b.right > W - 8 ? W - 8 - b.right : (b.left < 8 ? 8 - b.left : 0);
      if (dx) p.style.translate = Math.round(dx) + "px 0";
    });
    [].forEach.call(d.querySelectorAll(POP), function (p) {
      if (p.closest("details:not([open])") || p.dataset.fitY) return;
      p.dataset.fitY = "1"; fitY(p);
    });
  }
  // Up and down too (2026-11-22 audit): a menu that would go under the bottom tab bar or past the screen bottom opens
  // upward when there's more room above; otherwise it gets a max height and scrolls inside.
  function fitY(p) {
    var cs = getComputedStyle(p); if (cs.position !== "absolute") return;
    var bar = document.querySelector(".tabbar"), barH = bar && bar.getBoundingClientRect().height && getComputedStyle(bar).display !== "none" ? bar.getBoundingClientRect().height : 0;
    var lo = window.innerHeight - 8 - barH, d = p.closest("details"), s = d && d.querySelector(":scope > summary");
    var sr = s ? s.getBoundingClientRect() : null, b = p.getBoundingClientRect();
    if (p.classList.contains("chip-pop")) {
      if (b.top < 8 && sr && lo - sr.bottom > sr.top) p.classList.add("flip-down");
      return;
    }
    if (b.bottom > lo && sr && sr.top - 8 > lo - sr.bottom && b.height < sr.top - 8) p.classList.add("flip-up");
    else if (b.bottom > lo) { p.style.maxHeight = Math.max(160, lo - b.top) + "px"; p.style.overflowY = "auto"; }
  }
  // T8: a recording or task shown in the computer's reading pane isn't a pop-up: the list beside it keeps scrolling
  function inPane(d) { return window.ScribeDp && window.ScribeDp.on() && d.matches(window.ScribeDp.SHEETS); }
  // U16P: a phone's sheet is fixed and full width, so it's never off screen: it isn't measured (that read, while the
  // sheet was rising, made the tap feel slow). Nothing open and nothing locked: there's nothing to do.
  var phone = matchMedia("(max-width: 959px)");
  // after the layout changes (Pop up · Right · Below), every open pop-up is measured again from scratch
  window.ScribeRefit = function () { [].forEach.call(document.querySelectorAll("details[open] " + POP.split(", ").join(", details[open] ")), unfit); sync(); };
  function sync() {
    var html = document.documentElement, open = document.querySelectorAll(OPEN), any = document.querySelectorAll("details[open]");
    if (!open.length && !any.length && !html.classList.contains("pop-lock")) return;
    var lock = [].some.call(open, function (d) { return !inPane(d); });
    html.classList.toggle("pop-lock", lock);
    [].forEach.call(any, function (d) { if (!(phone.matches && d.matches(".fx-sheet"))) fit(d); });
  }
  document.addEventListener("toggle", function (e) {
    var d = e.target;
    if (d.tagName === "DETAILS" && !d.open) [].forEach.call(d.querySelectorAll(POP), function (p) {   // fresh fit next time
      delete p.dataset.fitY; p.classList.remove("flip-up", "flip-down"); p.style.maxHeight = ""; p.style.overflowY = ""; });
    requestAnimationFrame(function () {
      sync();
      // a panel that opens in the page (a speaker's "Who is it?", a line's edit panel, + Tag, Edit) is scrolled into view
      // when it would end under the tab bar or below the screen
      if (d.tagName === "DETAILS" && d.open && d.matches(".speaker-chip, .line-edit, .tag-add, details.edit")) {
        var c = d.querySelector(":scope > .chip-panel, :scope > :not(summary)");
        if (c && getComputedStyle(c).position !== "absolute" && getComputedStyle(c).position !== "fixed") c.scrollIntoView({ block: "nearest", behavior: "smooth" });
      }
    });
  }, true);
  ["click", "htmx:afterSettle", "htmx:historyRestore", "pageshow", "close"].forEach(function (ev) {
    document.addEventListener(ev, function () { requestAnimationFrame(sync); }, true);
  });
  window.addEventListener("resize", sync);
  window.addEventListener("pageshow", sync);   // a pop-up that's already open when the page loads (Today's cleanup ask)
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", sync); else sync();
})();

/* Swipe rows (rebuilt 2026-11-22, Brian: "text overlapping, not smooth, low res"; like iPhone Mail / Gmail).
   A row with data-sw='{"l":[...],"r":[...]}' follows your finger as one solid piece over a colored layer behind it:
   "l" = swipe right-to-left, "r" = left-to-right. Each action {t: label, i: icon, c: color, f: form selector} submits
   that form in the row, so it behaves exactly like its own button. A short swipe shows the buttons; a long swipe (past
   55% of the row) does the FIRST action straight away (a small buzz marks the point); letting go early springs back. */
(function () {
  var BW = 88, FULL = 0.55, st = null, open = null;
  function host(row) { return row.tagName === "DETAILS" ? row.querySelector(":scope > summary") : row; }
  function acts(row, side) { try { return JSON.parse(row.dataset.sw)[side] || []; } catch (e) { return []; } }
  function build(row, side) {
    // the layer sits beside the row, not inside it (a closed Inbox <details> hides everything but its summary).
    // 2026-11-22 night (Brian: "low res", text touching the color, overlap on Notifications): the color now spans the
    // whole card width (the card's own rounded corners clip it, no scaling = sharp text), and the moving row carries
    // its card's background out to the card edges, so its text always keeps its normal gap from the color.
    var h = host(row), box = row.parentElement, L = box.querySelector(":scope > .sw-layer"), a = acts(row, side);
    if (L && L._row === row && L.dataset.side === side) return L;
    if (L) L.remove();
    if (getComputedStyle(box).position === "static") box.style.position = "relative";
    L = document.createElement("div"); L._row = row; L.dataset.side = side;
    L.className = "sw-layer sw-" + side + " sw-c-" + (a[0].c || "ink");
    box.classList.add("sw-box");                               // nothing slides the page sideways
    var top = row.offsetTop + (h === row ? 0 : h.offsetTop), left = row.offsetLeft + (h === row ? 0 : h.offsetLeft);
    var pl = Math.max(0, left), pr = Math.max(0, box.clientWidth - left - h.offsetWidth);
    L.style.cssText = "inset:auto;top:" + (top + 1) + "px;left:0;width:" + box.clientWidth + "px;height:" + (h.offsetHeight - 2) + "px";
    var bg = getComputedStyle(box).backgroundColor;
    h.style.setProperty("--sw-bg", bg && bg !== "rgba(0, 0, 0, 0)" ? bg : "var(--surface)");
    h.style.boxShadow = "-" + pl + "px 0 0 var(--sw-bg), " + pr + "px 0 0 var(--sw-bg)";
    a.forEach(function (x) {
      var b = document.createElement("button"); b.type = "button"; b.className = "sw-b sw-c-" + (x.c || "ink"); b.dataset.sel = x.f;
      if (x.i) b.insertAdjacentHTML("beforeend", '<svg class="sw-i" aria-hidden="true"><use href="#i-' + x.i.replace(/\W/g, "") + '"/></svg>');
      var t = document.createElement("span"); t.className = "sw-t"; t.textContent = x.t; b.appendChild(t);
      L.appendChild(b);
    });
    box.insertBefore(L, row); h.classList.add("sw-host");
    return L;
  }
  var frame = 0, want = null;
  // B37/N11 (Brian, 2026-11-25: smoother swiping): a row let go with speed settles at that speed (a quick flick snaps,
  // a slow release glides), the way native lists do; ms = how long the settle takes (default .3 s).
  function move(row, dx, anim, ms) {
    var h = host(row);
    if (anim) { cancelAnimationFrame(frame); frame = 0; want = null; h.style.transitionDuration = ms ? ms + "ms" : ""; h.classList.add("sw-anim"); h.style.transform = dx ? "translate3d(" + dx + "px,0,0)" : ""; return; }
    h.classList.remove("sw-anim"); want = [h, dx];   // follow the finger once per screen frame
    if (!frame) frame = requestAnimationFrame(function () { frame = 0; if (want) want[0].style.transform = "translate3d(" + want[1] + "px,0,0)"; want = null; });
  }
  function cleanup(row) {
    var h = host(row);
    h.classList.remove("sw-host", "sw-anim"); h.style.boxShadow = "";
    var L = row.parentElement && row.parentElement.querySelector(":scope > .sw-layer"); if (L && L._row === row) L.remove();
    if (row.parentElement && !row.parentElement.querySelector(":scope > .sw-layer")) row.parentElement.classList.remove("sw-box");
  }
  function reset(row) {   // back to normal: no layer, no transform left behind
    var h = host(row); move(row, 0, true); if (open === row) open = null;
    setTimeout(function () { if (!h.style.transform) cleanup(row); }, 320);
  }
  function fold(row) {   // a row that's leaving (Delete, Done, Dismiss…) folds shut smoothly, like Gmail
    var L = row.parentElement && row.parentElement.querySelector(":scope > .sw-layer");
    [row, L].forEach(function (el) { if (!el) return; el.style.height = el.offsetHeight + "px"; el.style.overflow = "hidden"; });
    void row.offsetHeight;
    [row, L].forEach(function (el) { if (!el) return; el.classList.add("sw-fold"); el.style.height = "0px"; });
  }
  function fire(row, sel) {
    var f = row.querySelector(sel);
    if (f && f.hasAttribute("data-sw-keep")) { move(row, 0, true); setTimeout(function () { f.requestSubmit(); }, 150); return; }   // Summarize: the row stays, its cost page opens
    fold(row);
    setTimeout(function () { if (f) f.requestSubmit(); }, 200);
    setTimeout(function () { if (row.isConnected) { row.classList.remove("sw-fold"); row.style.height = row.style.overflow = ""; cleanup(row); move(row, 0, true); } }, 1500);   // a row that stays comes back
  }
  document.addEventListener("touchstart", function (e) {
    var t = e.target;
    if (open && !(t.closest && t.closest(".sw-layer"))) { var o = open; reset(o); if (t.closest && t.closest("[data-sw]") === o) { st = { eat: true }; return; } }
    var row = t.closest && t.closest("[data-sw]");
    if (!row || e.touches.length !== 1 || (row.tagName === "DETAILS" && row.open) || t.closest(".sw-layer, input, textarea, select")
        || (row.closest(".picking"))) { st = null; return; }
    // a finger that starts at the left edge opens the drawer (ui.js), never a row swipe (Brian, 2026-11-23: an edge swipe
    // sent rows to Trash)
    if (e.touches[0].clientX <= 28 && innerWidth < 960 && document.getElementById("drawer")) { st = null; return; }
    st = { row: row, x: e.touches[0].clientX, y: e.touches[0].clientY, dx: 0, on: false, w: host(row).offsetWidth, v: 0, lx: e.touches[0].clientX, lt: e.timeStamp };
  }, { passive: true });
  document.addEventListener("touchmove", function (e) {
    if (!st || st.eat) return;
    var x = e.touches[0].clientX, dx = x - st.x, dy = e.touches[0].clientY - st.y;
    if (!st.on) {
      if (Math.hypot(dx, dy) < 7) return;                          // decide only after a real move: early jitter no longer cancels
      if (Math.abs(dy) > Math.abs(dx) * 0.8) { st = null; return; }   // scrolling, not swiping
      st.x = x - (dx > 0 ? 1 : -1); dx = x - st.x;                    // no jump: the row starts right under the finger
    }
    var dt = e.timeStamp - st.lt; if (dt > 0) { st.v = 0.6 * ((x - st.lx) / dt) + 0.4 * st.v; st.lx = x; st.lt = e.timeStamp; }
    var side = dx < 0 ? "l" : "r", a = acts(st.row, side);
    st.on = true;
    if (!a.length) { st.side = null; move(st.row, dx / 6); return; }   // nothing that way: a small rubber band
    if (st.side !== side) { st.side = side; st.L = build(st.row, side); st.full = false; }
    st.dx = dx;
    var full = Math.abs(dx) > st.w * FULL;
    if (full !== st.full) { st.full = full; st.L.classList.toggle("sw-full", full); }
    move(st.row, dx);
  }, { passive: true });
  function settle(dist, v) {   // ms to cover dist px at the finger's speed v (px/ms), kept between 140 and 320 ms
    var sp = Math.abs(v); return sp > 0.2 ? Math.round(Math.max(140, Math.min(320, Math.abs(dist) / sp))) : 0;
  }
  document.addEventListener("touchend", function () {
    if (!st) return;
    var s = st; st = null;
    if (s.eat || !s.on) return;
    if (!s.side) { move(s.row, 0, true); return; }
    var a = acts(s.row, s.side), sign = s.dx < 0 ? -1 : 1, shown = a.length * BW;
    var fling = s.v * sign > 0.5;                                      // a quick flick counts, like native lists
    var back = s.v * sign < -0.3;                                      // flicked back toward the start: close
    if (s.full && !back) { var to = sign * (s.w + 40), ms = settle(to - s.dx, s.v) || 200; move(s.row, to, true, ms); s.row.dataset.swJust = Date.now(); setTimeout(function () { fire(s.row, a[0].f); }, ms); }
    else if (!back && (Math.abs(s.dx) > shown * 0.45 || (fling && Math.abs(s.dx) > 24))) { move(s.row, sign * shown, true, settle(sign * shown - s.dx, s.v)); open = s.row; s.row.dataset.swJust = Date.now(); }
    else { move(s.row, 0, true, settle(s.dx, s.v)); if (open === s.row) open = null; var r = s.row; setTimeout(function () { if (!host(r).style.transform) cleanup(r); }, 340); }
  });
  // The browser took the touch over (it started scrolling, or a call came in): the row goes back instead of staying
  // half-swiped (B37: rows were left stuck partway).
  document.addEventListener("touchcancel", function () { if (st && st.on && st.row) reset(st.row); st = null; });
  document.addEventListener("click", function (e) {
    var b = e.target.closest && e.target.closest(".sw-b");
    if (b) {
      e.preventDefault(); e.stopPropagation();
      var row = b.closest(".sw-layer")._row;
      if (row) { open = null; move(row, (b.parentElement.dataset.side === "l" ? -1 : 1) * (host(row).offsetWidth + 40), true); setTimeout(function () { fire(row, b.dataset.sel); }, 200); }
      return;
    }
    var r = e.target.closest && e.target.closest("[data-sw]");   // the lift right after a swipe must not open the row
    if (r && r.dataset.swJust && Date.now() - r.dataset.swJust < 400) { e.preventDefault(); e.stopPropagation(); }
  }, true);
  document.addEventListener("htmx:beforeRequest", function () { if (open) reset(open); });
})();

/* Tap outside to get out (Brian, 2026-11-22): every small pop-up panel (a speaker's "Who is it?", ＋ Tag, a line's
   edit panel, Edit/picker panels, Filters, menus) closes when you tap anywhere outside it, so you never have to find
   the little button that opened it. That tap only closes it: it doesn't also press whatever was under your finger,
   except when you tap another pop-up's button (it opens right away) or the bottom/side navigation. Esc closes too.
   Bottom sheets already close from their dimmed backdrop; the shortcuts dialog now closes from its backdrop as well. */
(function () {
  var KINDS = "details.speaker-chip, details.tag-add, details.line-edit, details.edit, details.filters, details.row-menu, details.chip-menu, details.ts-row";
  var OPEN = KINDS.split(", ").map(function (k) { return k + "[open]"; }).join(", ");
  window.addEventListener("click", function (e) {
    var t = e.target;
    if (!t || !t.isConnected || !t.closest) return;
    if (t.closest(".pp-sheet")) return;   // the person picker belongs to the panel that opened it
    if (t.tagName === "DIALOG" && t.open) {   // a tap on the dialog's dimmed backdrop (outside its box)
      var b = t.getBoundingClientRect();
      if (e.clientX < b.left || e.clientX > b.right || e.clientY < b.top || e.clientY > b.bottom) { e.preventDefault(); t.close(); return; }
    }
    var open = [].filter.call(document.querySelectorAll(OPEN), function (d) { return !d.contains(t); });
    // A bottom sheet whose dimmed backdrop sits under the top bar (Status): a tap on the dimmed top bar closes it too.
    [].forEach.call(document.querySelectorAll("details.fx-sheet[open]"), function (d) {
      if (window.ScribeDp && window.ScribeDp.on() && d.matches(window.ScribeDp.SHEETS)) return;   // T8: the reading pane stays open
      var body = d.querySelector(":scope > .fx-body");
      if (body && !d.contains(t) && !t.closest("details.fx-sheet[open] > .fx-body")) open.push(d);
    });
    if (!open.length) return;
    open.forEach(function (d) { d.open = false; });
    var sum = t.closest("summary");
    if (sum && sum.parentElement && sum.parentElement.matches(KINDS + ", details.fx-sheet")) return;   // opens the other one
    if (t.closest(".tabbar a, .sidebar a, .fx-sheet[open] .fx-body")) return;
    if (t.closest(".lines .who, .who-pin-btn, [data-open-sheet]")) return;   // F99 (500 pass): a speaker's name or a task title opens its own pop-up (took two taps)
    e.preventDefault(); e.stopPropagation();
  }, true);
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") document.querySelectorAll(OPEN).forEach(function (d) { d.open = false; });
  });
})();

/* Tab rows that don't fit (phone Search / Review): a soft fade on the side where more tabs are hidden, so it's clear the
   row can be swiped (Brian, 2026-11-22: nothing should look cut off). The selected tab is scrolled into view. */
(function () {
  function mark(n) {
    var more = n.scrollWidth - n.clientWidth > 2;
    n.classList.toggle("fade-r", more && n.scrollLeft + n.clientWidth < n.scrollWidth - 2);
    n.classList.toggle("fade-l", more && n.scrollLeft > 2);
  }
  function all() {
    document.querySelectorAll("nav.tabs").forEach(function (n) {
      if (!n.dataset.fadeOn) { n.dataset.fadeOn = "1"; n.addEventListener("scroll", function () { mark(n); }, { passive: true });
        var on = n.querySelector(".on, [aria-current]"); if (on && n.scrollWidth > n.clientWidth) n.scrollLeft = Math.max(0, on.offsetLeft - 16); }
      mark(n);
    });
  }
  ["DOMContentLoaded", "htmx:afterSettle", "pageshow"].forEach(function (ev) { document.addEventListener(ev, all); });
  window.addEventListener("resize", all);
  if (document.readyState !== "loading") all();
})();

/* The transcript loads when its section opens (2026-11-22), so a talk page is ~5x smaller. Desktop opens it at once. */
/* Brian, 2026-11-25 (B16): "/conversations/59#L6" never loaded the transcript. A link to a line opens the section while
   the page is still starting, before htmx has wired up the hx-trigger, so the "load-tx" event went to nobody and the
   one-time flag stopped any retry. Now nothing is asked of htmx until it has started (DOMContentLoaded). */
(function () {
  var ready = document.readyState === "complete";
  function load() {
    var b = document.getElementById("tx-body"), d = document.getElementById("transcript");
    if (!ready || !window.htmx) return;
    if (b && d && d.open && b.hasAttribute("hx-get") && !b.dataset.req) { b.dataset.req = "1"; htmx.trigger(b, "load-tx"); }
  }
  document.addEventListener("toggle", function (e) { if (e.target.id === "transcript") load(); }, true);
  document.addEventListener("DOMContentLoaded", function () { ready = true; load(); });
  if (ready) load();
  document.addEventListener("htmx:afterSettle", load);
  // Once the lines arrive, show the line the link asked for (#L6) and mark it.
  document.addEventListener("htmx:afterSettle", function (e) {
    if (!e.target || !e.target.querySelector || !/^#L\d+$/.test(location.hash)) return;
    var ln = e.target.querySelector(location.hash) || (e.target.id === location.hash.slice(1) ? e.target : null);
    if (!ln) return;
    document.querySelectorAll(".line.cursor").forEach(function (l) { if (l !== ln) l.classList.remove("cursor"); });   // F117: one mark
    ln.classList.add("cursor"); ln.scrollIntoView({ block: "center" });
  });
})();


/* Typing on a phone (Brian, 2026-11-22: the keyboard covered the "Someone new" name box). The page now shrinks when the
   keyboard opens (viewport interactive-widget=resizes-content); as a backup, a focused text box that is still behind
   the keyboard or under the bottom tab bar is scrolled to the middle of what's visible. Works for every text box. */
(function () {
  if (!window.visualViewport) return;
  var el = null;
  function check() {
    if (!el || document.activeElement !== el) return;
    var vv = window.visualViewport, r = el.getBoundingClientRect(), bar = document.querySelector(".tabbar");
    var bottom = vv.offsetTop + vv.height - (bar && bar.offsetParent && getComputedStyle(bar).position === "fixed" && vv.height > innerHeight * 0.8 ? bar.offsetHeight : 0);
    if (r.bottom > bottom - 8 || r.top < vv.offsetTop) {
      var box = el.closest(".fx-body, .chip-panel, .menu-panel");
      // 2026-11-22 night (Brian: still covered on the Pixel): instant, not smooth; the keyboard's slide-up cancelled
      // the smooth scroll. Checked again as the keyboard settles.
      if (box && box.scrollHeight > box.clientHeight) el.scrollIntoView({ block: "center" });
      window.scrollBy(0, el.getBoundingClientRect().top - (vv.offsetTop + vv.height * 0.35));
    }
  }
  var TXT = "input:not([type=checkbox]):not([type=radio]):not([type=range]):not([type=hidden]):not([type=date]):not([type=time]):not([type=datetime-local]), textarea, [contenteditable=true]";
  var touch = matchMedia("(pointer: coarse)").matches;
  document.addEventListener("focusin", function (e) {
    var t = e.target;
    if (!t.matches || !t.matches(TXT)) return;
    // tab bar and + hide while typing (like native apps). U17P (Brian, 2026-11-25): not in a search box: those sit at the
    // top, the bar doesn't cover them, and losing it while searching left no way to another tab.
    var search = t.matches('input[type="search"], input[name="q"], [role="searchbox"], [data-search]');
    el = t; document.documentElement.classList.toggle("typing", touch && !search);
    [150, 400, 800, 1200].forEach(function (ms) { setTimeout(check, ms); });
  });
  document.addEventListener("focusout", function () {
    setTimeout(function () { var a = document.activeElement; if (!(a && a.matches && a.matches(TXT))) document.documentElement.classList.remove("typing"); }, 50);
  });
  window.visualViewport.addEventListener("resize", function () { setTimeout(check, 50); setTimeout(check, 300); });
})();

/* Speed log (Brian, 2026-11-22: "still not loading quickly; how do we find the right problem?"). After each page and
   each part loaded in the background (a transcript, Show more), the phone sends Scribe Bot how long it took, split into:
   m14 = the M14 building the page, net = the trip over Tailscale, dl = downloading it, draw = the phone showing it.
   Numbers and page addresses only. Read on the M14 in logs/speed.log. */
(function () {
  if (!window.performance || !performance.getEntriesByType || !navigator.sendBeacon) return;
  var q = [], timer = null;
  function app(e) { var s = (e.serverTiming || []).filter(function (x) { return x.name === "app"; })[0]; return s ? s.duration : null; }
  function row(e, k) {
    var m14 = app(e), wait = e.responseStart - e.requestStart, u = new URL(e.name, location.href);
    if (u.origin !== location.origin || /^\/(perf|ping|status\/ring|progress|static|audio)/.test(u.pathname) || !(e.responseStart > 0)) return;
    var r = { k: k, p: u.pathname + u.search, total: (k !== "part" ? (e.domContentLoadedEventEnd || e.responseEnd) : e.responseEnd) - e.startTime,
              dl: e.responseEnd - e.responseStart, net: m14 == null ? wait : Math.max(0, wait - m14) };
    if (m14 != null) r.m14 = m14;
    if (k !== "part" && e.domContentLoadedEventEnd) r.draw = e.domContentLoadedEventEnd - e.responseEnd;
    if (k === "page" || k === "back") {   // 2026-11-22 night: also the tap itself and the setup before the request goes out
      r.pre = e.requestStart - e.startTime;                                  // worker wake-up + finding/opening the connection
      var fcp = (performance.getEntriesByName("first-contentful-paint")[0] || {}).startTime; if (fcp) r.fcp = fcp;   // first time anything shows
      try { var t = JSON.parse(sessionStorage.getItem("sbTap") || "null"); sessionStorage.removeItem("sbTap");
        if (t && t.p === u.pathname + u.search && performance.timeOrigin - t.at < 120000) r.tap = performance.timeOrigin - t.at; } catch (_) {}
    }
    q.push(r); clearTimeout(timer); timer = setTimeout(send, 1500);
  }
  function send() { if (!q.length) return; var b = q.splice(0, 50); try { navigator.sendBeacon("/perf", new Blob([JSON.stringify(b)], { type: "text/plain" })); } catch (_) {} }
  // B18 (2026-11-25): a Back load is logged as "back", and a page the browser shows again from memory (no request at all)
  // as "bfc" with the time from the press of Back to the page showing, so the log tells a slow Back from a slow page.
  addEventListener("load", function () { setTimeout(function () { var n = performance.getEntriesByType("navigation")[0]; if (n) row(n, n.type === "back_forward" ? "back" : "page"); }, 0); });
  addEventListener("pageshow", function (e) {
    if (!e.persisted) return;
    try { var t = JSON.parse(sessionStorage.getItem("sbTap") || "null"), p = location.pathname + location.search; sessionStorage.removeItem("sbTap");
      if (t && t.p === p && Date.now() - t.wall < 120000) { q.push({ k: "bfc", p: p, total: Date.now() - t.wall }); clearTimeout(timer); timer = setTimeout(send, 1500); } } catch (_) {}
  });
  if (window.PerformanceObserver) try {
    new PerformanceObserver(function (l) { l.getEntries().forEach(function (e) { if (e.initiatorType === "xmlhttprequest" || e.initiatorType === "fetch") row(e, "part"); }); })
      .observe({ type: "resource", buffered: true });
  } catch (_) {}
  addEventListener("pagehide", send);
  var stuck = [];
  document.addEventListener("click", function (e) {   // when a link was tapped (the next page measures tap -> start)
    var a = e.target.closest && e.target.closest("a[href]"); if (!a || e.defaultPrevented || a.target || a.hasAttribute("download") || a.matches("[hx-get], [hx-post], [hx-delete], [hx-put], [hx-patch]")) return;
    var u = new URL(a.href, location.href); if (u.origin !== location.origin || u.hash && u.pathname === location.pathname) return;
    try { sessionStorage.setItem("sbTap", JSON.stringify({ p: u.pathname + u.search, at: performance.timeOrigin + e.timeStamp, wall: Date.now() })); } catch (_) {}
    // Stuck-tap recorder (2026-11-22 night): still on this page 3 s after tapping a link = logged; at 10 s, one retry.
    stuck.forEach(clearTimeout);
    var p = u.pathname + u.search;
    stuck = [setTimeout(function () { q.push({ k: "stuck", p: p, after: 3000 }); send(); }, 3000),
             setTimeout(function () { q.push({ k: "stuck", p: p, after: 10000 }); send(); location.assign(u.href); }, 10000)];
  });
  addEventListener("pagehide", function () { stuck.forEach(clearTimeout); });
})();

/* Keep-alive (2026-11-22 night): the M14 is on a public Wi-Fi hotspot, which drops quiet connections. Then the first
   tap waited 1-2 s to reconnect, or hung on the dead connection. While the app is open, a tiny check every 25 s keeps
   the connection alive, and one goes out the moment the app comes back to the screen. */
/* B7 (Brian, 2026-11-25: delete on the phone, the computer should update): the same check asks the M14 what was removed
   since this page was drawn, on any device, and takes those rows off the screen. On the page of a talk or person that
   was removed, a note says so. Kinds and ids only; nothing else comes back. */
(function () {
  var meta = document.querySelector('meta[name="sb-gone"]'), mark = meta && meta.content !== "" ? +meta.content : null;
  function drop(el) {
    if (!el || !el.parentNode) return;
    el.querySelectorAll("details[open]").forEach(function (d) { d.open = false; });   // an open sheet closes (and unlocks the page)
    el.classList.add("gone-out"); setTimeout(function () { el.remove(); }, 220);
  }
  function note(text, href, label) {
    if (document.querySelector(".gone-note")) return;
    var n = document.createElement("div"); n.className = "gone-note"; n.setAttribute("role", "status");
    n.textContent = text + " ";
    var a = document.createElement("a"); a.href = href; a.textContent = label; n.appendChild(a);
    var c = document.getElementById("content") || document.body; c.insertBefore(n, c.firstChild);
  }
  function apply(g) {
    var id = String(g.id | 0), path = location.pathname;
    if (g.k === "talk" || g.k === "talk_purged") {
      document.querySelectorAll('.conv-row[data-id="' + id + '"], a.row1[href="/conversations/' + id + '"]').forEach(drop);
      if (g.k === "talk_purged") document.querySelectorAll('.tr-row[data-pick-id="c' + id + '"]').forEach(drop);
      if (path === "/conversations/" + id) note(g.k === "talk" ? "This talk was moved to Trash on another device." : "This talk was deleted on another device.",
        g.k === "talk" ? "/conversations?trash=1" : "/conversations", g.k === "talk" ? "Open Trash" : "Back to Talks");
    } else if (g.k === "task") {
      drop(document.getElementById("task-" + id));
    } else if (g.k === "person") {
      document.querySelectorAll('#people-list [data-pick-id="' + id + '"]').forEach(drop);
      if (path === "/people/" + id) note("This person was deleted on another device.", "/people", "Back to People");
    }
  }
  function ping() {
    if (document.visibilityState !== "visible") return;
    var u = mark === null ? "/ping" : "/gone?since=" + mark;
    try {
      fetch(u, { cache: "no-store", credentials: "same-origin" }).then(function (r) { return mark !== null && r.ok ? r.json() : null; })
        .then(function (d) { if (!d || mark === null) return; (d.gone || []).forEach(apply); if (d.last > mark) mark = d.last; }).catch(function () {});
    } catch (_) {}
  }
  setInterval(ping, 25000);
  addEventListener("focus", ping);   // B7: back to this window: catch up at once
  document.addEventListener("visibilitychange", ping);
  addEventListener("online", ping);   // R4-01: the computer's network changed (Wi-Fi switch, wake): warm the new connection before the next tap
  addEventListener("pageshow", function (e) { if (e.persisted) ping(); });
})();

/* A tapped row keeps its rounded "pressed" look until the next page shows (Brian, 2026-11-22: it flashed back to a square
   box between letting go and the page changing). Cleared if the page comes back from the back button. */
(function () {
  var SEL = ".tap-row, .row1, .in-row > summary, .sp-row > summary, .mention-row, a.ts-row";
  document.addEventListener("click", function (e) {
    var a = e.target.closest && e.target.closest("a[href]"); if (!a || e.defaultPrevented || a.target || a.hasAttribute("download")) return;
    var row = a.matches(SEL) ? a : a.closest(SEL); if (!row) return;
    document.querySelectorAll(".tap-go").forEach(function (x) { x.classList.remove("tap-go"); });
    row.classList.add("tap-go");
    setTimeout(function () { row.classList.remove("tap-go"); }, 8000);   // never stuck lit if the page doesn't change
  });
  addEventListener("pageshow", function () { document.querySelectorAll(".tap-go").forEach(function (x) { x.classList.remove("tap-go"); }); });
})();

/* Person picker (Brian picked A, 2026-11-22 night: "the list is getting long"). Any person list (a speaker's "Who is
   it?", task Who, People pickers) opens a sheet instead of the phone's long native list: a search box, "Suggested"
   (voice match, people from talks that week, people named most), then everyone A-Z with a letter strip. Typing
   narrows it; "＋ Add … as a new person" appears for a name not on the list. The list's own extra choices
   ("✕ Not X", "Don't know") show at the bottom. Picking sets the list and saves exactly as before. */
(function () {
  var COLORS = ["#2563eb", "#7c3aed", "#0d9488", "#db2777", "#ea580c", "#16a34a", "#ca8a04", "#4f46e5", "#0891b2", "#be123c"];
  function isPicker(s) { return s && s.tagName === "SELECT" && (s.hasAttribute("data-people") || !!s.querySelector('option[value="__new"]')); }
  var TITLE = /^(?:(?:dr|doctor|mr|mrs|ms|mx|miss|prof|professor|rev|reverend|pastor|rabbi|fr|sir|dame|hon|judge|coach|capt|captain|sgt|lt|col|gen|officer|nurse|dean)\.?\s+)+(?=\S+\s+\S)/i;   // B8: titles in general
  function initials(n) { return n.replace(/^Me \(Brian\)$/, "Brian").replace(TITLE, "").split(/\s+/).filter(function (w) { return /^\p{L}/u.test(w); }).map(function (w) { return w[0]; }).join("").slice(0, 2).toUpperCase(); }
  function color(n) { var h = 0; for (var i = 0; i < n.length; i++) h = (h * 31 + n.charCodeAt(i)) >>> 0; return COLORS[h % COLORS.length]; }
  function esc(t) { return String(t).replace(/[&<>"]/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]; }); }   // R5-06: quotes too (used in attributes)
  var sheet = null, sel = null, data = null, cache = {};
  function row(p, cls) {
    return '<button type="button" class="pp-row ' + (cls || "") + '" data-id="' + p.id + '"><span class="pp-av" style="background:' + color(p.name) + '">' + esc(initials(p.name)) +
      '</span><span class="pp-tx"><b>' + esc(p.name) + '</b>' + (p.sub ? '<small>' + esc(p.sub) + '</small>' : "") + '</span></button>';
  }
  function render(q) {
    var list = sheet.querySelector(".pp-list"), rail = sheet.querySelector(".pp-rail"), h = "", letters = [];
    q = (q || "").trim().toLowerCase();
    var extras = [].filter.call(sel.options, function (o) { return /^__(?!new)/.test(o.value); });
    var canNew = !!sel.querySelector('option[value="__new"]');
    if (!data) { list.innerHTML = '<p class="pp-msg">Loading…</p>'; return; }
    if (q) {
      var hits = data.all.filter(function (p) { return p.name.toLowerCase().indexOf(q) >= 0; })
        .sort(function (a, b) { return (a.name.toLowerCase().indexOf(q) === 0 ? 0 : 1) - (b.name.toLowerCase().indexOf(q) === 0 ? 0 : 1); });
      h = hits.slice(0, 40).map(function (p) { return row(p); }).join("");
      if (canNew && !data.all.some(function (p) { return p.name.toLowerCase() === q; }))
        h += '<button type="button" class="pp-row pp-new" data-new="1"><span class="pp-av pp-plus">＋</span><span class="pp-tx"><b>Add “' + esc(sheet.querySelector(".pp-q").value.trim()) + '” as a new person</b></span></button>';
      if (!hits.length && !canNew) h += '<p class="pp-msg">No one by that name</p>';
      rail.hidden = true;
    } else {
      if (data.suggested.length) h += '<h4 class="pp-sec">Suggested</h4>' + data.suggested.map(function (p) { return row(p); }).join("");
      h += '<h4 class="pp-sec">Everyone · A–Z</h4>';
      var last = "";
      data.all.forEach(function (p) { var L = (p.name.replace(TITLE, "")[0] || "#").toUpperCase(); if (L !== last) { last = L; letters.push(L); L = esc(L); h += '<span class="pp-l" id="pp-l-' + L + '"></span>'; } h += row(p); });
      if (canNew) h += '<button type="button" class="pp-row pp-new" data-new="1"><span class="pp-av pp-plus">＋</span><span class="pp-tx"><b>Someone new…</b></span></button>';
      rail.innerHTML = letters.length > 6 ? letters.map(function (L) { L = esc(L); return '<button type="button" data-l="' + L + '">' + L + '</button>'; }).join("") : "";
      rail.hidden = letters.length <= 6;
    }
    // B9 (2026-11-25): "Don't know" (and "Not X") sit at the top, above Suggested, where they're quick to reach;
    // while searching they stay at the bottom, under the names that match.
    var ex = extras.map(function (o) { return '<button type="button" class="pp-row pp-extra" data-val="' + esc(o.value) + '"><span class="pp-tx"><b>' + esc(o.textContent) + '</b></span></button>'; }).join("");
    list.innerHTML = q ? h + ex : (ex ? '<div class="pp-extras">' + ex + '</div>' : "") + h;
  }
  function build() {
    sheet = document.createElement("div"); sheet.className = "pp-sheet"; sheet.hidden = true;
    sheet.innerHTML = '<div class="pp-back"></div><div class="pp-body" role="dialog" aria-label="Pick a person"><div class="pp-grab"></div>' +
      '<div class="pp-head"><b class="pp-title">Who is it?</b><button type="button" class="pp-x" aria-label="Close">✕</button></div>' +
      '<label class="pp-search"><svg class="icon"><use href="#i-search"/></svg><input class="pp-q" type="search" autocomplete="off" placeholder="Search or type a new name"></label>' +
      '<div class="pp-scroll"><div class="pp-list"></div></div><div class="pp-rail" hidden></div></div>';
    document.body.appendChild(sheet);
    sheet.querySelector(".pp-q").addEventListener("input", function (e) { render(e.target.value); });
    sheet.querySelector(".pp-q").addEventListener("keydown", function (e) { if (e.key === "Enter") { e.preventDefault(); var f = sheet.querySelector(".pp-list .pp-row"); if (f) f.click(); } });
    sheet.addEventListener("click", function (e) {
      if (e.target.closest(".pp-back, .pp-x")) return close();
      var L = e.target.closest(".pp-rail button"); if (L) { jump(L); return; }
      var r = e.target.closest(".pp-row"); if (!r) return;
      var s = sel; close();
      if (r.dataset.new) {   // a new person: use the list's own "new name" box and save
        var name = sheet.querySelector(".pp-q").value.trim(), box = s.form && s.form.querySelector(".qp-new, [name=new_name]");
        s.value = "__new"; s.dispatchEvent(new Event("change", { bubbles: true }));
        if (box && name) { box.hidden = false; box.value = name; s.value = ""; s.form.requestSubmit(); }
        return;
      }
      var v = r.dataset.val || r.dataset.id;
      if (![].some.call(s.options, function (o) { return o.value === v; })) { var o = document.createElement("option"); o.value = v; o.textContent = r.querySelector("b").textContent; s.appendChild(o); }
      s.value = v; s.dispatchEvent(new Event("change", { bubbles: true }));
    });
    // Letter strip (2026-11-22 night, Brian: "the letters don't scroll"): tap a letter or drag along the strip
    function jump(L) {
      var t = document.getElementById("pp-l-" + L.dataset.l), sc = sheet.querySelector(".pp-scroll"); if (!t) return;
      sc.scrollTop = t.offsetTop - 4;
      sheet.querySelectorAll(".pp-rail .on").forEach(function (x) { x.classList.remove("on"); }); L.classList.add("on");
    }
    var rail = sheet.querySelector(".pp-rail"), lastL = null;
    function railAt(e) { var c = e.touches ? e.touches[0] : e, el = document.elementFromPoint(rail.getBoundingClientRect().left + rail.offsetWidth / 2, c.clientY);
      var b = el && el.closest && el.closest(".pp-rail button"); if (b && b !== lastL) { lastL = b; jump(b); } }
    rail.addEventListener("touchstart", function (e) { lastL = null; railAt(e); e.preventDefault(); }, { passive: false });
    rail.addEventListener("touchmove", function (e) { railAt(e); e.preventDefault(); }, { passive: false });
    var y0 = null;   // swipe the sheet down to close, like every other sheet
    sheet.addEventListener("touchstart", function (e) { if (e.target.closest(".pp-grab, .pp-head")) y0 = e.touches[0].clientY; }, { passive: true });
    sheet.addEventListener("touchend", function (e) { if (y0 != null && e.changedTouches[0].clientY - y0 > 60) close(); y0 = null; });
  }
  function close() { if (sheet) { sheet.hidden = true; document.documentElement.classList.remove("pop-lock"); } sel = null; }
  function open(s) {
    if (!sheet) build();
    sel = s; data = null;
    var m = s.form && /\/speakers\/(\d+)\//.exec(s.form.getAttribute("action") || s.form.getAttribute("hx-post") || ""), c = /\/conversations\/(\d+)/.exec(location.pathname);
    var key = (m ? m[1] : 0) + ":" + (c ? c[1] : 0);
    sheet.querySelector(".pp-title").textContent = s.getAttribute("aria-label") || "Who is it?";
    sheet.querySelector(".pp-q").value = ""; sheet.querySelector(".pp-scroll").scrollTop = 0;
    sheet.hidden = false; document.documentElement.classList.add("pop-lock"); render("");
    if (cache[key]) { data = cache[key]; render(""); return; }
    fetch("/people/pick?sid=" + (m ? m[1] : 0) + "&cid=" + (c ? c[1] : 0), { credentials: "same-origin" }).then(function (r) { return r.json(); })
      .then(function (j) { cache[key] = j; if (sel === s) { data = j; render(sheet.querySelector(".pp-q").value); } })
      .catch(function () { close(); s.focus(); if (window.sbNote) window.sbNote("Couldn't load the list of people. Check the connection, then try again."); });
  }
  function grab(e) { var s = e.target.closest && e.target.closest("select"); if (!isPicker(s) || s.disabled) return; e.preventDefault(); open(s); }
  document.addEventListener("mousedown", grab, true);
  var t0 = null;   // a scroll that happens to start on the list must not open it
  document.addEventListener("touchstart", function (e) { t0 = [e.touches[0].clientX, e.touches[0].clientY]; }, { capture: true, passive: true });
  document.addEventListener("touchend", function (e) {
    var s = e.target.closest && e.target.closest("select"); if (!isPicker(s)) return;
    var c = e.changedTouches[0]; if (t0 && Math.hypot(c.clientX - t0[0], c.clientY - t0[1]) > 10) return;
    e.preventDefault(); open(s);
  }, { capture: true, passive: false });
  document.addEventListener("keydown", function (e) {
    if (sheet && !sheet.hidden && e.key === "Escape") { close(); return; }
    var s = e.target; if (isPicker(s) && (e.key === "Enter" || e.key === " " || e.key === "ArrowDown")) { e.preventDefault(); open(s); }
  }, true);
  document.addEventListener("htmx:afterSettle", function () { cache = {}; });   // a new person may have been added
  // A button that opens a picker in the same pop-up ("Move to someone else" on a note, 2026-11-22)
  document.addEventListener("click", function (e) {
    var b = e.target.closest && e.target.closest("[data-pick-for]"); if (!b) return;
    var s = document.getElementById(b.dataset.pickFor); if (!s) return;
    e.preventDefault(); s.dispatchEvent(new Event("pointerdown", { bubbles: true })); open(s);
  });
})();

/* Phone: mark the audio player while it's pinned under the top bar, so CSS can cover everything behind it (2026-11-23) */
(function () {
  var raf = 0;
  function check() {
    raf = 0;
    var pl = document.querySelector(".player"); if (!pl) return;
    var bar = document.querySelector(".topbar"), t = bar && getComputedStyle(bar).display !== "none" ? bar.getBoundingClientRect().bottom : 0;
    pl.classList.toggle("stuck", scrollY > 0 && pl.getBoundingClientRect().top <= t + 1.5);
  }
  addEventListener("scroll", function () { if (!raf) raf = requestAnimationFrame(check); }, { passive: true });
})();

/* Hold for a label (Brian, 2026-11-23, like Google's apps): press and hold an icon or button that has no hold action of
   its own and a small label says what it does ("Move to Trash"), then fades. Rows with their own long-press (select,
   sheets, swipes) are left alone. Phones/touch only; desktop has hover titles. */
(function () {
  var t = 0, tip = null, sx = 0, sy = 0, shown = false;
  var OWN = ".tap-row, .sw-host, [data-open-sheet], [data-pick-id], .line, .item-main";
  function label(el) {
    var s = el.getAttribute("aria-label") || el.getAttribute("title") || el.dataset.tip || "";
    if (!s && el.textContent.trim().length <= 2) s = "";   // icon with no words and no label: nothing to say
    return s.trim();
  }
  function hide() { clearTimeout(t); t = 0; if (tip) { var x = tip; tip = null; x.classList.remove("on"); setTimeout(function () { x.remove(); }, 200); } }
  document.addEventListener("touchstart", function (e) {
    hide(); shown = false;
    var el = e.target.closest && e.target.closest("button, a, summary, [role=button], .icon-btn");
    if (!el || el.closest(OWN) && !el.matches(".icon-btn, .btn-icon, [aria-label]")) return;
    var s = label(el); if (!s) return;
    sx = e.touches[0].clientX; sy = e.touches[0].clientY;
    t = setTimeout(function () {
      shown = true;
      tip = document.createElement("div"); tip.className = "hold-tip"; tip.textContent = s; document.body.appendChild(tip);
      var r = el.getBoundingClientRect(), w = tip.offsetWidth, h = tip.offsetHeight;
      var x = Math.max(8, Math.min(innerWidth - w - 8, r.left + r.width / 2 - w / 2)), y = r.top - h - 8;
      if (y < 8) y = r.bottom + 8;
      tip.style.left = x + "px"; tip.style.top = y + "px";
      requestAnimationFrame(function () { if (tip) tip.classList.add("on"); });
      setTimeout(hide, 1800);
    }, 450);
  }, { passive: true });
  document.addEventListener("touchmove", function (e) { if (t && !shown && Math.hypot(e.touches[0].clientX - sx, e.touches[0].clientY - sy) > 10) hide(); }, { passive: true });
  document.addEventListener("touchend", function (e) { if (!shown) { clearTimeout(t); t = 0; } else { e.preventDefault(); } }, { passive: false });
  document.addEventListener("contextmenu", function (e) { if (shown) e.preventDefault(); });
  document.addEventListener("click", function (e) { if (shown) { e.preventDefault(); e.stopPropagation(); shown = false; } }, true);
})();

/* A menu item that opens something on the page (e.g. ⋯ › Summarize opens "Summarize this talk?") closes its menu first,
   so the menu doesn't sit on top of what it opened (Brian, 2026-11-23: the cost and Confirm were hidden behind it). */
document.addEventListener("click", function (e) {
  var it = e.target.closest && e.target.closest("details .menu-item[hx-get], details .menu-item[data-open-sheet]");
  if (!it) return;
  var d = it.closest("details[open]"); if (d) d.open = false;
  var slot = it.getAttribute("hx-target"); if (slot) setTimeout(function () { var s = document.querySelector(slot); if (s) s.scrollIntoView({ block: "nearest", behavior: "smooth" }); }, 250);
});

/* Talks "See more" (Brian, 2026-11-23): a date group with lots of talks (19 memos in one week) shows the first 5 and
   "See 14 more"; tapping it shows the rest ("Hide" folds them again). */
(function () {
  var KEEP = 5;
  function fold(list) {
    if (!list || !list.hasAttribute("data-fold-groups")) return;
    list.querySelectorAll(".grp-toggle").forEach(function (b) { b.remove(); });
    var groups = [], cur = null;
    Array.prototype.forEach.call(list.children, function (el) {
      if (el.classList.contains("list-head")) { cur = { head: el, rows: [] }; groups.push(cur); }
      else if (cur && el.classList.contains("item") && !el.classList.contains("show-more")) cur.rows.push(el);
    });
    groups.forEach(function (g) {
      if (g.rows.length <= KEEP + 1) return;
      var extra = g.rows.slice(KEEP), open = false;
      var b = document.createElement("button"); b.type = "button"; b.className = "item show-more grp-toggle";
      function paint() { extra.forEach(function (r) { r.hidden = !open; }); b.textContent = open ? "Hide" : "See " + extra.length + " more"; }
      b.addEventListener("click", function () { open = !open; paint(); if (!open) g.head.scrollIntoView({ block: "nearest" }); });
      extra[extra.length - 1].after(b); paint();
    });
  }
  function run() { fold(document.getElementById("conv-list")); }
  if (document.readyState !== "loading") run(); else document.addEventListener("DOMContentLoaded", run);
  document.addEventListener("htmx:afterSettle", run);
})();

/* U11 (Brian, 2026-11-25): confirm questions use the app's own themed box (base.html #ask), never the browser's.
   sbConfirm(question, okLabel) -> Promise<boolean>. Forms with hx-confirm or data-confirm ask before they're sent
   (forms are plain posts since 2026-11-22, so htmx's own hx-confirm never ran on them), and so do htmx requests. */
(function () {
  window.sbConfirm = function (text, ok) {
    var d = document.getElementById("ask");
    if (!d || !d.showModal) return Promise.resolve(window.confirm(text));
    document.getElementById("ask-text").textContent = text;
    var yes = d.querySelector('[data-ask="yes"]'), no = d.querySelector('[data-ask="no"]');
    yes.textContent = ok || "OK";
    return new Promise(function (resolve) {
      function done(v) { yes.onclick = no.onclick = d.onclose = d.onclick = null; if (d.open) d.close(); resolve(v); }
      yes.onclick = function () { done(true); };
      no.onclick = function () { done(false); };
      d.onclose = function () { done(false); };                                  // Escape
      d.onclick = function (e) { if (e.target === d) done(false); };            // a tap on the backdrop
      d.showModal(); no.focus();
    });
  };
  document.addEventListener("submit", function (e) {
    var f = e.target, q = f.getAttribute && (f.getAttribute("data-confirm") || f.getAttribute("hx-confirm"));
    if (!q || f.dataset.asked) return;
    e.preventDefault();
    var by = e.submitter;
    window.sbConfirm(q, by && by.textContent.trim().length < 24 ? by.textContent.trim() : "OK").then(function (ok) {
      if (!ok) return;
      f.dataset.asked = "1";
      if (f.requestSubmit) f.requestSubmit(by || undefined); else f.submit();
      delete f.dataset.asked;
    });
  }, true);
  document.addEventListener("htmx:confirm", function (e) {
    if (!e.detail.question || e.target.tagName === "FORM") return;
    e.preventDefault();
    window.sbConfirm(e.detail.question).then(function (ok) { if (ok) e.detail.issueRequest(true); });
  });
})();

/* R4-18: a speaker's Voice button (macros.voice). One shared player: tap to play, tap again to stop; the button fills
   as it plays. Starting it pauses the talk's own player, and the talk's player stops it. */
(function () {
  var au = null, btn = null;
  function label(b, on) { b.classList.toggle("playing", on); b.querySelector(".vb-t").textContent = on ? "Stop" : "Voice";
    b.setAttribute("aria-label", on ? "Stop their voice" : "Play their voice"); if (!on) b.style.removeProperty("--vp"); }
  function stop() { if (au) au.pause(); if (btn) { btn.classList.remove("loading"); label(btn, false); } btn = null; }
  document.addEventListener("click", function (e) {
    var b = e.target.closest && e.target.closest("[data-voice]");
    if (!b) { if (btn && e.target.closest && e.target.closest("[data-sp-close], [data-fx-close]")) stop(); return; }
    e.preventDefault(); e.stopPropagation();
    if (btn === b) { stop(); return; }
    stop();
    if (!au) {
      au = new Audio(); au.preload = "none";
      au.addEventListener("timeupdate", function () { if (btn && au.duration) btn.style.setProperty("--vp", (au.currentTime / au.duration * 100).toFixed(1) + "%"); });
      au.addEventListener("playing", function () { if (btn) btn.classList.remove("loading"); });
      au.addEventListener("ended", stop);
      au.addEventListener("error", function () { if (btn) { stop(); if (window.sbNote) window.sbNote("Couldn't play this voice sample. Its file may be gone."); } });   // R5-15: never a silent tap
    }
    var main = document.getElementById("player"); if (main && !main.paused) main.pause();
    btn = b; label(b, true); b.classList.add("loading");
    au.src = b.dataset.voice; au.currentTime = 0;
    var p = au.play(); if (p && p.catch) p.catch(function () { stop(); });
  }, true);
  document.addEventListener("play", function (e) { if (e.target && e.target.id === "player") stop(); }, true);
  document.addEventListener("htmx:beforeSwap", stop);
  document.addEventListener("toggle", function (e) { if (btn && e.target.tagName === "DETAILS" && !e.target.open && e.target.contains(btn)) stop(); }, true);
})();
