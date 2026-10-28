// The landing page's behaviour: a replay of a real session, two tabs, a copy button and a few reveals. No dependencies.
// The session itself (window.FDEMO) is written by scripts/site_demo.py from what the built `fetch` printed.
(function () {
  var doc = document, root = doc.documentElement;
  root.classList.remove("nojs");
  var reduce = window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches;
  function $(id) { return doc.getElementById(id); }
  function esc(s) { return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;"); }

  // ---- the replay ---------------------------------------------------------------------------------------------------------------
  var screen = $("screen"), verdict = $("verdict"), running = 0, timer = null, settled = false;
  var NODES = ["api", "other", "evil"], EDGES = ["api", "other", "evil"];
  function span(c, t) { return '<span class="' + c + '">' + esc(t) + "</span>"; }
  function setState(s) {
    if (!s) { return; }
    NODES.forEach(function (n) {
      var el = $("n-" + n), st = $("s-" + n), e = $("e-" + n), l = $("l-" + n), kind = (s.nodes || {})[n] || "";
      el.setAttribute("class", "node" + (kind ? " " + kind : ""));
      e.setAttribute("class", "edge" + (kind ? " " + kind : ""));
      st.setAttribute("class", "stamp" + (kind ? " " + kind + " show" : ""));
      st.textContent = (s.stamps || {})[n] || "";
      if (l) { l.setAttribute("class", "edge-label" + (kind ? " show" : "")); }
    });
    verdict.className = "verdict " + (s.verdict[1] || "");
    verdict.textContent = s.verdict[0];
  }
  function finish(items) {
    clearTimeout(timer); settled = true;
    screen.innerHTML = items.map(function (it) { return span(it.l[0], it.l[1]); }).join("\n");
    setState(items[items.length - 1].s);
  }
  function play() {
    var items = window.FDEMO.items, token = ++running;
    clearTimeout(timer); settled = false;
    if (reduce) { finish(items); return; }
    screen.innerHTML = "";
    setState(window.FDEMO.start);
    var i = 0, j = 0, html = "";
    (function tick() {
      if (token !== running) { return; }
      if (i >= items.length) { settled = true; return; }
      var it = items[i], text = it.l[1], fast = it.l[0] !== "d";
      j += fast ? 400 : 3;
      screen.innerHTML = html + span(it.l[0], text.slice(0, j));
      screen.scrollTop = screen.scrollHeight;
      if (j >= text.length) {
        html += span(it.l[0], text) + "\n"; if (it.s) { setState(it.s); }
        i++; j = 0;
        timer = setTimeout(tick, it.pause || 700);
      } else { timer = setTimeout(tick, 12); }
    })();
  }
  if (screen && window.FDEMO) {
    $("replay").addEventListener("click", play);
    window.cdemo = { play: play, done: function () { return settled; } };
    var started = false;
    function start() { if (!started) { started = true; play(); } }
    if ("IntersectionObserver" in window && !reduce) {
      new IntersectionObserver(function (es, o) { if (es[0].isIntersecting) { o.disconnect(); start(); } }, { threshold: 0.35 }).observe($("demo-stage"));
    } else { start(); }
  }

  // ---- humans and agents --------------------------------------------------------------------------------------------------
  var tabs = doc.querySelectorAll("#audience button"), panels = { humans: $("humans"), agents: $("agents") };
  function show(which, scroll) {
    for (var k = 0; k < tabs.length; k++) { tabs[k].setAttribute("aria-selected", tabs[k].dataset.panel === which ? "true" : "false"); }
    for (var name in panels) { if (panels[name]) { panels[name].classList.toggle("on", name === which); } }
    if (scroll) { try { history.replaceState(null, "", "#" + which); } catch (e) {} }
  }
  for (var t = 0; t < tabs.length; t++) { tabs[t].addEventListener("click", function () { show(this.dataset.panel, true); }); }
  function fromHash() {
    var h = location.hash.replace("#", "");
    if (h === "agents" || h === "humans") { show(h, false); $("audience-section").scrollIntoView(); }
    else { show("humans", false); }
  }
  window.addEventListener("hashchange", fromHash);
  fromHash();

  // ---- copy the prompt ------------------------------------------------------------------------------------------------------------
  var copyBtn = $("copyprompt");
  if (copyBtn) {
    copyBtn.addEventListener("click", function () {
      var text = $("agentprompt").textContent;
      function done(ok) { copyBtn.textContent = ok ? "Copied" : "Select and copy"; setTimeout(function () { copyBtn.textContent = "Copy"; }, 2000); }
      if (navigator.clipboard && navigator.clipboard.writeText) { navigator.clipboard.writeText(text).then(function () { done(true); }, function () { done(false); }); }
      else { var r = doc.createRange(); r.selectNodeContents($("agentprompt")); var sel = getSelection(); sel.removeAllRanges(); sel.addRange(r); done(false); }
    });
  }

  // ---- reveals ----------------------------------------------------------------------------------------------------------------------
  var reveal = doc.querySelectorAll(".reveal");
  if ("IntersectionObserver" in window && !reduce) {
    var io = new IntersectionObserver(function (es) { es.forEach(function (e) { if (e.isIntersecting) { e.target.classList.add("in"); io.unobserve(e.target); } }); }, { threshold: 0.25 });
    for (var r = 0; r < reveal.length; r++) { io.observe(reveal[r]); }
  } else { for (var q = 0; q < reveal.length; q++) { reveal[q].classList.add("in"); } }
})();
