// Location, compass heading and screen wake lock.
// `?sim=1` replaces real GPS with a position you set by tapping the map.
const Geo = (() => {
  const params = new URLSearchParams(location.search);
  const sim = params.has('sim');
  const SIM_CENTER = [51.5073, -0.1657]; // Hyde Park, London

  let watchId = null;
  let onPosition = () => {};
  let onError = () => {};
  let last = null;              // {lat, lon, acc, ts}
  let heading = null;           // degrees clockwise from north, smoothed
  let headingVec = null;        // [x, y] low-pass filtered unit vector
  let compassListening = false;
  let wakeLock = null;
  let wakeWanted = false;

  function emit(lat, lon, acc) {
    last = { lat, lon, acc, ts: Date.now() };
    onPosition(last);
  }

  function start() {
    if (sim) {
      if (!last) {
        // Random spot within ~600 m of the centre so tabs start apart.
        const saved = sessionStorage.getItem('simpos');
        if (saved) {
          const [lat, lon] = JSON.parse(saved);
          emit(lat, lon, 5);
        } else {
          setSimPosition(SIM_CENTER[0] + (Math.random() - 0.5) * 0.01,
                         SIM_CENTER[1] + (Math.random() - 0.5) * 0.016);
        }
      }
      return true;
    }
    if (!('geolocation' in navigator)) { onError('This browser has no geolocation'); return false; }
    if (watchId !== null) return true;
    watchId = navigator.geolocation.watchPosition(
      (p) => emit(p.coords.latitude, p.coords.longitude, p.coords.accuracy),
      (err) => onError(err.code === 1 ? 'Location permission denied: enable it in your browser settings'
                                     : 'Could not get your location (' + err.message + ')'),
      { enableHighAccuracy: true, maximumAge: 5000, timeout: 30000 },
    );
    return true;
  }

  function setSimPosition(lat, lon) {
    sessionStorage.setItem('simpos', JSON.stringify([lat, lon]));
    emit(lat, lon, 5);
  }

  // ---- compass ----
  function needsCompassPermission() {
    return typeof DeviceOrientationEvent !== 'undefined' &&
           typeof DeviceOrientationEvent.requestPermission === 'function';
  }

  async function startCompass() {
    if (compassListening) return true;
    if (typeof DeviceOrientationEvent === 'undefined') return false;
    if (needsCompassPermission()) {
      try {
        const r = await DeviceOrientationEvent.requestPermission(); // must run inside a tap on iOS
        if (r !== 'granted') return false;
      } catch { return false; }
    }
    const evt = 'ondeviceorientationabsolute' in window ? 'deviceorientationabsolute' : 'deviceorientation';
    window.addEventListener(evt, onOrientation, true);
    compassListening = true;
    return true;
  }

  function onOrientation(e) {
    let h = null;
    if (typeof e.webkitCompassHeading === 'number' && !isNaN(e.webkitCompassHeading)) {
      h = e.webkitCompassHeading;                          // iOS: already clockwise from north
    } else if ((e.absolute || e.type === 'deviceorientationabsolute') && e.alpha != null) {
      h = (360 - e.alpha) % 360;                           // Android: alpha is counter-clockwise
    }
    if (h == null) return;
    const screenAngle = (screen.orientation && screen.orientation.angle) || window.orientation || 0;
    h = (h + screenAngle + 360) % 360;
    // Low-pass filter on the unit vector so 359°→0° doesn't jump.
    const r = h * Math.PI / 180;
    const v = [Math.sin(r), Math.cos(r)];
    headingVec = headingVec ? [headingVec[0] * 0.8 + v[0] * 0.2, headingVec[1] * 0.8 + v[1] * 0.2] : v;
    heading = (Math.atan2(headingVec[0], headingVec[1]) * 180 / Math.PI + 360) % 360;
  }

  // ---- wake lock ----
  async function keepAwake() {
    wakeWanted = true;
    if (!('wakeLock' in navigator) || wakeLock) return !!wakeLock;
    try {
      wakeLock = await navigator.wakeLock.request('screen');
      wakeLock.addEventListener('release', () => { wakeLock = null; });
      return true;
    } catch { return false; }
  }
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible' && wakeWanted) keepAwake();
  });

  return {
    sim,
    start,
    setSimPosition,
    startCompass,
    needsCompassPermission,
    keepAwake,
    hasWakeLock: () => !!wakeLock,
    compassActive: () => heading !== null,
    get heading() { return heading; },
    get last() { return last; },
    set onPosition(fn) { onPosition = fn; },
    set onError(fn) { onError = fn; },
  };
})();
