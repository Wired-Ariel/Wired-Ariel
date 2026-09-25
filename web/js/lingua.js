/* lingua.js - IT / EN per le pagine del sito (2026-09-24).
 *
 * Ogni pagina esiste in due file: pagina.html (italiano) e pagina-en.html
 * (inglese). Questo script, caricato nel <head>:
 *   1. alla prima visita sceglie la lingua dal browser (italiano solo se il
 *      browser e' in italiano) e la ricorda;
 *   2. se la pagina aperta non e' nella lingua scelta, passa all'altra
 *      SUBITO, prima di disegnare niente;
 *   3. mette il bottone IT | EN nel segnaposto #lingua-slot della pagina.
 * La chiave "gen3pm-lang" e' la stessa della Mappa live: la scelta vale
 * anche li'.
 */
(function () {
  "use strict";
  var K = "gen3pm-lang";
  var pagina = location.pathname.split("/").pop() || "index.html";
  var en = /-en\.html$/.test(pagina);
  var base = en ? pagina.replace(/-en\.html$/, ".html") : pagina;
  var percorso = { it: base, en: base.replace(/\.html$/, "-en.html") };
  var mia = en ? "en" : "it";

  var pref = null;
  // "passotile-lang": il nome di prima (2026-09-25), letto come ripiego.
  try { pref = localStorage.getItem(K) || localStorage.getItem("passotile-lang"); } catch (e) { pref = null; }
  if (pref !== "it" && pref !== "en") {
    pref = /^it\b/i.test(navigator.language || "") ? "it" : "en";
    try { localStorage.setItem(K, pref); } catch (e) { /* niente */ }
  }
  if (pref !== mia) {
    location.replace(percorso[pref] + location.search + location.hash);
    return;
  }

  function vai(l) {
    try { localStorage.setItem(K, l); } catch (e) { /* niente */ }
    if (l !== mia) location.href = percorso[l] + location.search + location.hash;
  }

  var css = ".lingua-sw{display:inline-flex;border:1px solid currentColor;border-radius:9px;overflow:hidden;" +
    "font:700 12.5px/1 system-ui,sans-serif;letter-spacing:.06em;opacity:.92}" +
    ".lingua-sw button{all:unset;cursor:pointer;padding:7px 11px;color:inherit}" +
    ".lingua-sw button[aria-pressed=true]{background:currentColor}" +
    ".lingua-sw button[aria-pressed=true] span{filter:invert(1);mix-blend-mode:normal}" +
    ".lingua-sw button:focus-visible{outline:2px solid currentColor;outline-offset:-3px}" +
    ".lingua-fissa{position:fixed;top:10px;right:12px;z-index:50}";

  document.addEventListener("DOMContentLoaded", function () {
    var st = document.createElement("style"); st.textContent = css; document.head.appendChild(st);
    var box = document.createElement("span");
    box.className = "lingua-sw";
    box.setAttribute("role", "group");
    box.setAttribute("aria-label", mia === "en" ? "Language" : "Lingua");
    [["it", "IT", "Italiano"], ["en", "EN", "English"]].forEach(function (x) {
      var b = document.createElement("button");
      b.type = "button";
      b.title = x[2];
      b.setAttribute("aria-pressed", String(x[0] === mia));
      b.setAttribute("lang", x[0]);
      var s = document.createElement("span"); s.textContent = x[1];
      b.appendChild(s);
      b.onclick = function () { vai(x[0]); };
      box.appendChild(b);
    });
    var slot = document.getElementById("lingua-slot");
    if (slot) slot.appendChild(box); else { box.classList.add("lingua-fissa"); document.body.appendChild(box); }
  });
})();
