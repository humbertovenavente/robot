(function () {
  "use strict";

  var BACKOFF = [1000, 2000, 4000, 8000]; // D-17 matches station backoff
  var attempt = 0;
  var ws = null;

  var cardsEl = document.getElementById("cards");
  var bannerEl = document.getElementById("conn-banner");
  var emptyEl = document.getElementById("empty-state");
  var cards = {}; // station_id -> element

  function wsUrl() {
    var proto = (location.protocol === "https:") ? "wss:" : "ws:";
    return proto + "//" + location.host + "/ws?role=dashboard";
  }

  function setConnected(isConnected) {
    if (isConnected) {
      bannerEl.classList.add("hidden");
    } else {
      bannerEl.classList.remove("hidden");
    }
  }

  function ensureCard(stationId) {
    if (cards[stationId]) return cards[stationId];
    var card = document.createElement("article");
    card.className = "card online";
    card.dataset.stationId = stationId;
    card.innerHTML =
      '<h2></h2>' +
      '<span class="badge free">free</span>' +
      '<div class="event-line"><span class="muted">no cycles yet</span></div>' +
      '<div class="offline-overlay">OFFLINE</div>';
    card.querySelector("h2").textContent = stationId;
    cardsEl.appendChild(card);
    cards[stationId] = card;
    emptyEl.classList.add("hidden");
    return card;
  }

  function renderUpdate(msg) {
    var card = ensureCard(msg.station_id);
    var badge = card.querySelector(".badge");
    badge.className = "badge " + msg.status;
    badge.textContent = msg.status.replace("_", " ");
    var line = card.querySelector(".event-line");
    if (msg.last_class && msg.last_destination != null) {
      line.innerHTML = "Last: <strong>" + escapeHtml(msg.last_class) + "</strong> \u2192 bin <strong>" + msg.last_destination + "</strong>";
    } else {
      line.innerHTML = '<span class="muted">no cycles yet</span>';
    }
    if (msg.online === false) {
      card.classList.remove("online");
    } else {
      card.classList.add("online");
    }
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function connect() {
    ws = new WebSocket(wsUrl());
    ws.addEventListener("open", function () {
      setConnected(true);
      attempt = 0;
    });
    ws.addEventListener("message", function (ev) {
      try {
        var msg = JSON.parse(ev.data);
        if (msg && msg.type === "station_update" && msg.station_id) {
          renderUpdate(msg);
        }
      } catch (e) {
        console.warn("dashboard: bad message", e);
      }
    });
    ws.addEventListener("close", function () {
      setConnected(false);
      var delay = BACKOFF[Math.min(attempt, BACKOFF.length - 1)];
      attempt++;
      setTimeout(connect, delay);
    });
    ws.addEventListener("error", function () {
      try { ws.close(); } catch (e) {}
    });
  }

  connect();
})();
