// Leaflet map: own position + teammates (from server snapshots).
const GameMap = (() => {
  let map = null;
  let me = null;
  let meAcc = null;
  let mates = L.layerGroup();
  let centred = false;
  let onTap = null;
  let onMateTap = null;

  function init(el) {
    if (map) return;
    map = L.map(el, { zoomControl: true, attributionControl: true }).setView([51.5073, -0.1657], 15);
    L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
      maxZoom: 19,
      attribution: '&copy; OpenStreetMap contributors',
    }).addTo(map);
    mates.addTo(map);
    map.on('click', (e) => onTap && onTap(e.latlng.lat, e.latlng.lng));

    const RecenterControl = L.Control.extend({
      onAdd() {
        const b = L.DomUtil.create('button', 'map-btn');
        b.textContent = '◎';
        b.title = 'Centre on me';
        L.DomEvent.on(b, 'click', (ev) => { L.DomEvent.stop(ev); if (me) map.setView(me.getLatLng(), Math.max(map.getZoom(), 16)); });
        return b;
      },
    });
    new RecenterControl({ position: 'topright' }).addTo(map);
  }

  function setMe(lat, lon, acc) {
    if (!map) return;
    const ll = [lat, lon];
    if (!me) {
      meAcc = L.circle(ll, { radius: acc || 10, color: '#2f7cf6', weight: 1, fillOpacity: 0.12 }).addTo(map);
      me = L.circleMarker(ll, { radius: 8, color: '#fff', weight: 3, fillColor: '#2f7cf6', fillOpacity: 1 }).addTo(map);
    } else {
      me.setLatLng(ll);
      meAcc.setLatLng(ll).setRadius(acc || 10);
    }
    if (!centred) { map.setView(ll, 16); centred = true; }
  }

  function fmtAge(sec) {
    if (sec == null) return '';
    if (sec < 60) return 'just now';
    return Math.round(sec / 60) + ' min ago';
  }

  // players: [{id, name, lat, lon, fix_age_s}], snapshotAt: server epoch seconds.
  // colour: a CSS colour, or a function (player) => colour.
  function setMates(players, myId, colour, serverNow, snapshotAt) {
    if (!map) return;
    mates.clearLayers();
    for (const p of players || []) {
      if (p.id === myId || p.lat == null) continue;
      const age = (p.fix_age_s || 0) + Math.max(0, serverNow - (snapshotAt || serverNow));
      // bubblingMouseEvents:false so tapping a teammate doesn't also count as a map tap (sim-mode move).
      L.circleMarker([p.lat, p.lon], { radius: 11, color: '#fff', weight: 2, fillColor: typeof colour === 'function' ? colour(p) : colour, fillOpacity: 0.95, bubblingMouseEvents: false })
        .bindTooltip(`${escapeHtml(p.name)} · ${fmtAge(age)}`, { permanent: true, direction: 'top', offset: [0, -10], className: 'mate-label' })
        .on('click', () => onMateTap && onMateTap(p))
        .addTo(mates);
    }
  }

  function clearMates() { mates.clearLayers(); }
  function resize() { if (map) setTimeout(() => map.invalidateSize(), 50); }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  return {
    init, setMe, setMates, clearMates, resize, escapeHtml,
    set onTap(fn) { onTap = fn; },
    set onMateTap(fn) { onMateTap = fn; },
  };
})();
