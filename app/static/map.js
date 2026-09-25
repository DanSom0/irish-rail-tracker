/* Live map enhancement; the server-rendered station list remains the fallback. */
(() => {
  const mapElement = document.getElementById('network-map');
  if (!mapElement || !window.L) return;

  const enhancement = document.getElementById('map-enhancement');
  const panel = document.getElementById('map-panel');
  const panelContent = document.getElementById('map-panel-content');
  const closeButton = document.getElementById('map-panel-close');
  const selector = document.getElementById('map-station');
  const form = document.getElementById('map-station-form');
  const resetButton = document.getElementById('map-reset');
  const list = document.getElementById('map-station-list');
  const freshness = document.getElementById('map-freshness');
  const error = document.getElementById('map-error');
  const map = L.map(mapElement, { scrollWheelZoom: false });
  const markers = new Map();
  let stations = [];
  let opener = null;
  let selectedCode = selector.value;
  let initialSelection = selectedCode;
  enhancement.hidden = false;
  resetButton.hidden = false;
  L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
    attribution: '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap contributors</a>',
    maxZoom: 18,
  }).addTo(map);
  fetch('/static/rail-lines.geojson').then(response => {
    if (!response.ok) throw new Error('rail geometry unavailable');
    return response.json();
  }).then(geometry => L.geoJSON(geometry, { style: { color: '#637c83', weight: 2, opacity: 0.75 }, interactive: false }).addTo(map))
    .catch(() => {});

  function node(tag, className, value) {
    const item = document.createElement(tag);
    if (className) item.className = className;
    if (value !== undefined) item.textContent = value;
    return item;
  }
  function append(parent, tag, className, value) {
    const item = node(tag, className, value);
    parent.append(item);
    return item;
  }
  function localTime(iso) {
    return new Intl.DateTimeFormat('en-IE', { timeZone: 'Europe/Dublin', dateStyle: 'medium', timeStyle: 'short' }).format(new Date(iso));
  }
  function age(iso) {
    if (!iso) return 'No observations yet';
    const minutes = Math.max(0, Math.floor((Date.now() - new Date(iso).getTime()) / 60000));
    if (minutes === 0) return 'Updated just now';
    if (minutes >= 1440) { const n = Math.floor(minutes / 1440); return `Updated ${n} day${n === 1 ? '' : 's'} ago`; }
    if (minutes >= 60) { const n = Math.floor(minutes / 60); return `Updated ${n} hour${n === 1 ? '' : 's'} ago`; }
    return `Updated ${minutes} minute${minutes === 1 ? '' : 's'} ago`;
  }
  function time(parent, iso) {
    const item = append(parent, 'time', '', age(iso));
    if (iso) { item.dateTime = iso; item.title = localTime(iso); }
    return item;
  }
  function delayText(delay) {
    if (delay === 0) return 'On time';
    const amount = Number(Math.abs(delay).toFixed(1));
    return `${amount} min ${delay < 0 ? 'early' : 'late'}`;
  }
  function statusClass(status) { return 'map-' + status.replaceAll(' ', '-'); }
  function summary(parent, station) {
    append(parent, 'span', 'map-status', station.status);
    append(parent, 'p', '', station.average_reported_delay === null ? 'No recent data' :
      `Average reported delay ${delayText(station.average_reported_delay)} · ${station.current_trains.length} current train${station.current_trains.length === 1 ? '' : 's'}`);
    const updated = append(parent, 'p', 'freshness');
    time(updated, station.latest_observation_at);
  }
  function trains(parent, station) {
    if (!station.current_trains.length) {
      append(parent, 'p', 'empty', 'No trains in the latest stored station poll seen in the last 10 minutes.');
    } else {
      const scroll = append(parent, 'div', 'table-scroll');
      const table = append(scroll, 'table');
      const head = append(table, 'thead');
      const header = append(head, 'tr');
      for (const label of ['Scheduled', 'Expected', 'Destination', 'Status']) append(header, 'th', '', label);
      const body = append(table, 'tbody');
      for (const train of station.current_trains) {
        const row = append(body, 'tr');
        append(row, 'td', 'numeric', train.scheduled);
        const expected = append(row, 'td', 'numeric' + (train.expected === train.scheduled && !train.expected_day_offset ? ' unchanged' : ''), train.expected || '—');
        if (train.expected_day_offset) append(expected, 'small', '', ` ${train.expected_day_offset > 0 ? '+' : ''}${train.expected_day_offset} day`);
        append(row, 'th', '', train.destination).scope = 'row';
        const state = append(row, 'td', '', delayText(train.delay));
        const reading = append(state, 'small', 'map-reading', `${train.train_code} · From ${train.origin} · `);
        const stamp = append(reading, 'time', '', localTime(train.reading_at));
        stamp.dateTime = train.reading_at;
      }
    }
    const link = append(parent, 'a', 'map-station-link', `View ${station.name} station page →`);
    link.href = `/stations/${encodeURIComponent(station.code)}`;
  }
  function show(station, source) {
    selectedCode = station.code;
    selector.value = station.code;
    opener = source;
    panelContent.replaceChildren();
    const heading = append(panelContent, 'h2', '', station.name);
    heading.id = 'map-panel-title';
    summary(panelContent, station);
    trains(panelContent, station);
    panel.hidden = false;
    if (station.lat !== null && station.lon !== null) map.flyTo([station.lat, station.lon], Math.max(map.getZoom(), 13), { animate: false });
    closeButton.focus();
  }
  function close() {
    panel.hidden = true;
    if (opener && opener.isConnected) opener.focus();
  }
  closeButton.addEventListener('click', close);
  document.addEventListener('keydown', event => { if (event.key === 'Escape' && !panel.hidden) close(); });
  function select(source) {
    const station = stations.find(item => item.code === selector.value);
    if (station) show(station, source);
  }
  selector.addEventListener('change', () => select(selector));
  form.addEventListener('submit', event => { event.preventDefault(); select(form.querySelector('button[type="submit"]')); });
  function fit() {
    const points = stations.filter(item => item.lat !== null && item.lon !== null).map(item => [item.lat, item.lon]);
    if (points.length) map.fitBounds(points, { padding: [24, 24] });
  }
  resetButton.addEventListener('click', () => { fit(); close(); });
  function render(data) {
    const open = new Set([...list.querySelectorAll('details[open]')].map(item => item.id));
    stations = data.stations;
    for (const marker of markers.values()) marker.remove();
    markers.clear();
    list.replaceChildren();
    for (const station of stations) {
      if (station.lat !== null && station.lon !== null) {
        const icon = node('div', 'map-marker');
        append(icon, 'i', 'map-dot ' + statusClass(station.status));
        append(icon, 'span', 'map-train-count', String(station.current_trains.length));
        const marker = L.marker([station.lat, station.lon], { icon: L.divIcon({ html: icon, className: '', iconSize: [38, 28] }), keyboard: false });
        marker.addTo(map).on('click', () => { const element = marker.getElement(); element.tabIndex = -1; show(station, element); });
        markers.set(station.code, marker);
      }
      const detail = append(list, 'details', 'map-list-item');
      detail.id = `station-${station.code}`;
      detail.open = open.has(detail.id) || station.code === selectedCode;
      const label = append(detail, 'summary');
      const name = append(label, 'span', 'map-list-name');
      append(name, 'i', 'map-dot ' + statusClass(station.status));
      append(name, 'span', '', station.name);
      append(label, 'span', '', station.status);
      append(label, 'span', '', station.average_reported_delay === null ? 'No recent data' : `Average reported delay ${delayText(station.average_reported_delay)}`);
      time(append(label, 'span'), station.latest_observation_at);
      trains(detail, station);
    }
    const newest = stations.map(item => item.latest_observation_at).filter(Boolean).sort().at(-1);
    freshness.replaceChildren();
    time(freshness, newest);
    if (!map._loaded) fit();
    if (initialSelection) {
      const selected = stations.find(item => item.code === initialSelection);
      initialSelection = '';
      if (selected) show(selected, selector);
    }
    if (!panel.hidden && selectedCode) {
      const selected = stations.find(item => item.code === selectedCode);
      if (selected) {
        panelContent.replaceChildren();
        append(panelContent, 'h2', '', selected.name).id = 'map-panel-title';
        summary(panelContent, selected);
        trains(panelContent, selected);
      }
    }
  }
  async function refresh() {
    try {
      const response = await fetch('/api/network', { cache: 'no-store' });
      if (!response.ok) throw new Error('network unavailable');
      render(await response.json());
      error.hidden = true;
    } catch (_) { error.hidden = false; }
  }
  refresh();
  setInterval(refresh, 60000);
})();
