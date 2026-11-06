// The sidebar: draws apps.json (sent by Rust), opens apps in the app view, marks the open one.
(function () {
  var T = window.__TAURI__, invoke = T.core.invoke, listen = T.event.listen;
  if (/Mac/.test(navigator.platform)) document.documentElement.classList.add("mac");
  // The home page's groups, in its order (site/index.html)
  var GROUPS = [["watch", "Watch, listen, read"], ["docs", "Documents and files"], ["home", "Home and tools"], ["account", "Account"], ["admin", "Admin"]];
  var groups = document.getElementById("groups"), foot = document.getElementById("foot");
  var welcome = document.getElementById("welcome"), side = document.getElementById("side");
  var signin = document.getElementById("browser-signin"), wait = document.getElementById("wait"), err = document.getElementById("err");

  // Signed out: sign in through the browser (passkeys), or here with an authenticator code
  signin.addEventListener("click", function () {
    signin.disabled = true; wait.hidden = false; err.hidden = true;
    invoke("browser_sign_in").catch(function (e) { err.textContent = String(e); err.hidden = false; })
      .finally(function () { signin.disabled = false; wait.hidden = true; });
  });
  document.getElementById("here-signin").addEventListener("click", function () { invoke("sign_in_here"); });
  var current = "", drawn = "";

  function el(tag, cls, text) { var e = document.createElement(tag); if (cls) e.className = cls; if (text) e.textContent = text; return e; }
  function hostOf(u) { try { return new URL(u).host; } catch (e) { return ""; } }

  function draw(d) {
    var key = JSON.stringify(d && d.apps);
    welcome.hidden = !!d; side.hidden = !d;
    if (!d) { groups.replaceChildren(); foot.hidden = true; drawn = ""; return; }
    if (key !== drawn) {
      drawn = key;
      var frag = document.createDocumentFragment(), i = 0;
      GROUPS.forEach(function (g) {
        var items = d.apps.filter(function (a) { return a.group === g[0]; });
        if (!items.length) return;
        var sec = el("section", "group"); sec.appendChild(el("h2", "rise", g[1]));
        sec.lastChild.style.setProperty("--i", i++);
        items.forEach(function (a) {
          var b = el("button", "app rise"); b.type = "button"; b.dataset.url = a.url; b.dataset.status = a.status;
          b.style.setProperty("--i", i++);
          var ic = el("span", "icon"), img = el("img"); img.src = a.icon.replace(/^\//, ""); img.alt = ""; ic.appendChild(img);
          b.append(ic, el("span", "n", a.name), el("span", "dot"));
          if (a.netbird) { b.title = "NetBird only"; b.dataset.netbird = "1"; }
          b.addEventListener("click", function () { invoke("open_app", { url: a.url }); });
          sec.appendChild(b);
        });
        frag.appendChild(sec);
      });
      groups.replaceChildren(frag);
      if (document.documentElement.classList.contains("rail")) setRail(true);
    }
    // The person and the outside status
    foot.hidden = false;
    var name = d.user.name || d.user.username || "";
    document.getElementById("name").textContent = name;
    var av = document.getElementById("avatar"), hash = 0, who = d.user.username || name;
    for (var k = 0; k < who.length; k++) hash = (hash * 31 + who.charCodeAt(k)) >>> 0;
    av.textContent = name.charAt(0).toUpperCase(); av.style.background = "hsl(" + (hash % 360) + " 55% 86%)";
    var st = document.getElementById("status"), h = d.health || {};
    st.className = "status " + (!h.known ? "" : h.ok ? "up" : "bad");
    document.getElementById("status-text").textContent = !h.known ? "No outside check yet" : h.ok ? "Everything is up" : (h.problems && h.problems[0]) || "Something isn't working";
    mark(current);
  }

  // The open app: same host as the app view's page
  function mark(url) {
    current = url; var host = hostOf(url);
    document.querySelectorAll(".app[data-url]").forEach(function (b) { b.classList.toggle("active", hostOf(b.dataset.url) === host); });
    document.getElementById("home").classList.toggle("active", /^home\./.test(host));
  }

  document.getElementById("home").addEventListener("click", function () { invoke("go_home"); });

  // Collapse to an icon rail and back; remembered here (the sidebar's own storage)
  var railBtn = document.getElementById("rail");
  function setRail(on) {
    document.documentElement.classList.toggle("rail", on);
    railBtn.setAttribute("aria-pressed", on);
    railBtn.title = railBtn.ariaLabel = on ? "Show sidebar" : "Collapse sidebar";
    document.querySelectorAll(".app").forEach(function (b) {
      var n = b.querySelector(".n"); if (n) b.title = on ? n.textContent : (b.dataset.netbird ? "NetBird only" : "");
    });
    try { localStorage.setItem("rail", on ? "1" : ""); } catch (e) {}
    invoke("set_rail", { rail: on });
  }
  var startRail = false;
  try { startRail = localStorage.getItem("rail") === "1"; } catch (e) {}
  railBtn.addEventListener("click", function () { setRail(!document.documentElement.classList.contains("rail")); });
  document.querySelectorAll("[data-nav]").forEach(function (b) {
    b.addEventListener("click", function () {
      invoke("nav", { to: b.dataset.nav });
      if (b.dataset.nav === "reload") { b.classList.remove("spin"); void b.offsetWidth; b.classList.add("spin"); }
    });
  });
  // Keyboard, while the sidebar has focus: Alt+← / Alt+→, Ctrl/⌘+R, Alt+Home
  addEventListener("keydown", function (e) {
    var mod = e.ctrlKey || e.metaKey;
    if (e.altKey && e.key === "ArrowLeft") invoke("nav", { to: "back" });
    else if (e.altKey && e.key === "ArrowRight") invoke("nav", { to: "forward" });
    else if (mod && e.key === "r") { e.preventDefault(); invoke("nav", { to: "reload" }); }
    else if (e.altKey && e.key === "Home") invoke("go_home");
    else return;
  });
  document.getElementById("browser").addEventListener("click", function () { invoke("open_in_browser"); });
  document.getElementById("signout").addEventListener("click", function () { invoke("sign_out"); });
  listen("apps", function (e) { draw(e.payload); });
  listen("nav", function (e) { mark(e.payload); });
  invoke("apps").then(draw);
  if (startRail) setRail(true);
})();
