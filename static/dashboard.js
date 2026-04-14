(function () {
  "use strict";

  // ── WebSocket / station cards ──────────────────────────────────────────────
  var BACKOFF = [1000, 2000, 4000, 8000];
  var attempt = 0;
  var ws = null;
  var cardsEl = document.getElementById("cards");
  var bannerEl = document.getElementById("conn-banner");
  var emptyEl = document.getElementById("empty-state");
  var cards = {};

  function wsUrl() {
    var proto = (location.protocol === "https:") ? "wss:" : "ws:";
    return proto + "//" + location.host + "/ws?role=dashboard";
  }
  function setConnected(ok) { bannerEl.classList[ok ? "add" : "remove"]("hidden"); }
  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, function(c) {
      return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c];
    });
  }
  function ensureCard(id) {
    if (cards[id]) return cards[id];
    var card = document.createElement("article");
    card.className = "card online";
    card.dataset.stationId = id;
    card.innerHTML = '<h2></h2><span class="badge free">free</span>' +
      '<div class="event-line"><span class="muted">no cycles yet</span></div>' +
      '<div class="offline-overlay">OFFLINE</div>';
    card.querySelector("h2").textContent = id;
    cardsEl.appendChild(card);
    cards[id] = card;
    emptyEl.classList.add("hidden");
    return card;
  }
  function renderUpdate(msg) {
    var card = ensureCard(msg.station_id);
    var badge = card.querySelector(".badge");
    badge.className = "badge " + msg.status;
    badge.textContent = msg.status.replace(/_/g, " ");
    var line = card.querySelector(".event-line");
    if (msg.path_blocked) {
      line.innerHTML = '<span class="obstacle-warn">&#9888; Objeto detectado: <strong>' +
        escapeHtml(msg.blocking_object || "objeto") +
        "</strong> — robot en espera</span>";
    } else if (msg.last_class && msg.last_destination != null) {
      line.innerHTML = "Last: <strong>" + escapeHtml(msg.last_class) +
        "</strong> → bin <strong>" + msg.last_destination + "</strong>";
    } else {
      line.innerHTML = '<span class="muted">no cycles yet</span>';
    }
    card.classList[msg.online === false ? "remove" : "add"]("online");
  }
  function connect() {
    ws = new WebSocket(wsUrl());
    ws.addEventListener("open", function() { setConnected(true); attempt = 0; });
    ws.addEventListener("message", function(ev) {
      try {
        var msg = JSON.parse(ev.data);
        if (msg && msg.type === "station_update" && msg.station_id) renderUpdate(msg);
      } catch(e) {}
    });
    ws.addEventListener("close", function() {
      setConnected(false);
      setTimeout(connect, BACKOFF[Math.min(attempt++, BACKOFF.length-1)]);
    });
    ws.addEventListener("error", function() { try { ws.close(); } catch(e) {} });
  }
  connect();

  // ── Camera + QR scan ──────────────────────────────────────────────────────
  var video   = document.getElementById("cam-video");
  var canvas  = document.getElementById("cam-canvas");
  var overlay = document.getElementById("cam-overlay");
  var result  = document.getElementById("qr-result");
  var selEl   = document.getElementById("cam-select");
  var btnStart= document.getElementById("cam-start");
  var btnStop    = document.getElementById("cam-stop");
  var btnRefresh = document.getElementById("cam-refresh");
  var stream  = null;
  var scanTimer = null;

  function populateCameras(devices) {
    var prev = selEl.value;
    selEl.innerHTML = '<option value="">Select camera…</option>';
    devices.filter(function(d){ return d.kind === "videoinput"; }).forEach(function(d, i) {
      var opt = document.createElement("option");
      opt.value = d.deviceId;
      opt.textContent = d.label || ("Camera " + (i+1));
      if (d.deviceId === prev) opt.selected = true;
      selEl.appendChild(opt);
    });
  }

  // Initial enumerate (labels may be empty until permission is granted)
  navigator.mediaDevices.enumerateDevices().then(populateCameras).catch(function(){});

  // Refresh: request permission then re-enumerate so iPhone/Continuity Camera appears
  btnRefresh.addEventListener("click", function() {
    navigator.mediaDevices.getUserMedia({ video: true }).then(function(tmpStream) {
      tmpStream.getTracks().forEach(function(t){ t.stop(); });
      return navigator.mediaDevices.enumerateDevices();
    }).then(populateCameras).catch(function(e) {
      overlay.textContent = "Permission denied: " + e.message;
    });
  });

  btnStart.addEventListener("click", function() {
    var deviceId = selEl.value;
    var constraints = { video: deviceId ? { deviceId: { exact: deviceId } } : true };
    navigator.mediaDevices.getUserMedia(constraints).then(function(s) {
      stream = s;
      video.srcObject = s;
      video.style.display = "block";
      overlay.style.display = "none";
      btnStart.disabled = true;
      btnStop.disabled = false;
      startScan();
      // Re-enumerate with labels after permission granted
      navigator.mediaDevices.enumerateDevices().then(function(devices) {
        selEl.innerHTML = '<option value="">Select camera…</option>';
        devices.filter(function(d){ return d.kind === "videoinput"; }).forEach(function(d,i){
          var opt = document.createElement("option");
          opt.value = d.deviceId;
          opt.textContent = d.label || ("Camera " + (i+1));
          if (stream && stream.getVideoTracks()[0] &&
              stream.getVideoTracks()[0].label === d.label) opt.selected = true;
          selEl.appendChild(opt);
        });
      });
    }).catch(function(e) {
      overlay.textContent = "Camera error: " + e.message;
    });
  });

  btnStop.addEventListener("click", stopCam);

  function stopCam() {
    if (stream) { stream.getTracks().forEach(function(t){ t.stop(); }); stream = null; }
    video.style.display = "none";
    overlay.style.display = "flex";
    overlay.textContent = "No camera — click Start";
    btnStart.disabled = false;
    btnStop.disabled = true;
    clearInterval(scanTimer);
    result.classList.add("hidden");
  }

  function startScan() {
    if (!window.BarcodeDetector) { result.textContent = "BarcodeDetector not supported in this browser — use Chrome/Edge"; result.classList.remove("hidden"); return; }
    var detector = new window.BarcodeDetector({ formats: ["qr_code"] });
    scanTimer = setInterval(function() {
      if (!stream || video.readyState < 2) return;
      canvas.width = video.videoWidth;
      canvas.height = video.videoHeight;
      canvas.getContext("2d").drawImage(video, 0, 0);
      detector.detect(canvas).then(function(codes) {
        if (codes.length === 0) { result.classList.add("hidden"); return; }
        var payload = codes[0].rawValue.trim();
        result.classList.remove("hidden");
        if (payload === "A" || payload === "B" || payload === "C") {
          result.className = "qr-result";
          result.textContent = "QR detectado: Paquete " + payload + " → Bin " + {A:1,B:2,C:3}[payload];
        } else if (payload === "ROBOT") {
          result.className = "qr-result";
          result.textContent = "QR detectado: ROBOT";
        } else {
          result.className = "qr-result unknown";
          result.textContent = "QR desconocido: " + payload;
        }
      }).catch(function(){});
    }, 300);
  }

})();
