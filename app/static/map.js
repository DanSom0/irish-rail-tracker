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
  const trainToggle = document.getElementById('map-show-trains');
  const trainFreshness = document.getElementById('train-freshness');
  const trainError = document.getElementById('train-error');
  const trainSection = document.getElementById('train-list-section');
  const trainList = document.getElementById('train-list');
  const trainListStatus = document.getElementById('train-list-status');
  const trainCount = document.getElementById('train-count');
  enhancement.hidden = false; // Before creating the map, so Leaflet measures a visible container.
  const map = L.map(mapElement, { scrollWheelZoom: false }).setView([53.35, -6.2], 10);
  const markers = new Map();
  const trainLayer = L.layerGroup().addTo(map);
  const trainMarkerElements = new Map();
  const trainListButtons = new Map();
  let stations = [];
  let stationsUpdatedAt = freshness.querySelector('time')?.dateTime || null;
  let fitted = false;
  let trains = [];
  let trainsFetchedAt = null;
  let trainState = 'loading';
  // The last copy of each train and the fetch it came from, so a panel can say when a train left the feed.
  const lastSeen = new Map();
  // What the panel shows ({ type: 'station' | 'train' | 'group', ... }) and how to refocus its opener.
  let view = null;
  let opener = null;
  let openerRef = null;
  let selectedCode = selector.value;
  let initialSelection = selectedCode;
  resetButton.hidden = false;
  for (const id of ['train-toggle', 'train-legend', 'train-note']) document.getElementById(id).hidden = false;
  trainSection.hidden = false;
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
  function ago(iso) {
    const minutes = Math.max(0, Math.floor((Date.now() - new Date(iso).getTime()) / 60000));
    if (minutes === 0) return 'just now';
    if (minutes >= 1440) { const n = Math.floor(minutes / 1440); return `${n} day${n === 1 ? '' : 's'} ago`; }
    if (minutes >= 60) { const n = Math.floor(minutes / 60); return `${n} hour${n === 1 ? '' : 's'} ago`; }
    return `${minutes} min ago`;
  }
  function age(iso) {
    if (!iso) return 'No observations yet';
    const minutes = Math.max(0, Math.floor((Date.now() - new Date(iso).getTime()) / 60000));
    if (minutes === 0) return 'Updated just now';
    if (minutes >= 1440) { const n = Math.floor(minutes / 1440); return `Updated ${n} day${n === 1 ? '' : 's'} ago`; }
    if (minutes >= 60) { const n = Math.floor(minutes / 60); return `Updated ${n} hour${n === 1 ? '' : 's'} ago`; }
    return `Updated ${minutes} minute${minutes === 1 ? '' : 's'} ago`;
  }
  function stamp(parent, iso, text) {
    const item = append(parent, 'time', '', text);
    if (iso) { item.dateTime = iso; item.title = localTime(iso); }
    return item;
  }
  function time(parent, iso) { return stamp(parent, iso, age(iso)); }
  function delayText(delay) {
    if (delay === 0) return 'On time';
    const amount = Number(Math.abs(delay).toFixed(1));
    return `${amount} min ${delay < 0 ? 'early' : 'late'}`;
  }
  function statusClass(status) { return 'map-' + status.replaceAll(' ', '-'); }
  function summary(parent, station) {
    append(parent, 'span', 'map-status', station.status);
    append(parent, 'p', '', station.average_reported_delay === null ? 'No recent data' :
      `Average delay: ${station.average_reported_delay.toFixed(1)} min`);
    append(parent, 'p', 'map-trains-count', `${station.current_trains.length} train${station.current_trains.length === 1 ? '' : 's'}`);
    const updated = append(parent, 'p', 'freshness');
    time(updated, station.latest_observation_at);
  }
  function stationTrains(parent, station) {
    if (!station.current_trains.length) {
      append(parent, 'p', 'empty', 'No recent trains at this station.');
    } else {
      for (const [direction, heading] of [['arrival', 'Arrivals'], ['departure', 'Departures']]) {
        const group = station.current_trains.filter(train => train.direction === direction);
        if (!group.length) continue;
        const section = append(parent, 'section', 'map-service-group');
        append(section, 'h3', '', heading);
        const rows = append(section, 'ul', 'map-services');
        for (const train of group) {
          const row = append(rows, 'li', 'map-service');
          const main = append(row, 'div', 'map-service-main');
          append(main, 'strong', '', direction === 'arrival' ? `Arriving from ${train.origin}` : `Departing to ${train.destination}`);
          append(main, 'span', '', `Expected ${train.expected || '—'}${train.expected_day_offset ? ` (${train.expected_day_offset > 0 ? '+' : ''}${train.expected_day_offset} day)` : ''}`);
          append(main, 'span', 'map-service-delay' + (train.delay >= 6 ? ' badge major' : ''), delayText(train.delay));
          const reading = append(row, 'small', 'map-reading', `${train.train_code} · ${train.origin} → ${train.destination} · `);
          const serviceDate = append(reading, 'time', '', train.train_date);
          serviceDate.dateTime = train.train_date;
          reading.append(` · Scheduled ${train.scheduled} · `);
          time(reading, train.reading_at);
        }
      }
    }
    const link = append(parent, 'a', 'map-station-link', `View ${station.name} station page →`);
    link.href = `/stations/${encodeURIComponent(station.code)}`;
  }

  // Train details. Feed text is only ever set with textContent.
  function trainGlyph(parent) {
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('class', 'train-glyph');
    svg.setAttribute('viewBox', '0 0 16 16');
    svg.setAttribute('aria-hidden', 'true');
    const use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
    use.setAttribute('href', '#train-glyph');
    svg.append(use);
    parent.append(svg);
  }
  function messageLines(train) { return train.public_message ? train.public_message.split('\n') : []; }
  function positionLabel(parent, fetchedAt = trainsFetchedAt) {
    parent.append('Last reported position · data fetched ');
    stamp(parent, fetchedAt, ago(fetchedAt));
  }
  function currentTrain(code) { return trains.find(item => item.train_code === code); }
  function plural(count) { return `${count} train${count === 1 ? '' : 's'}`; }
  function destination(parent, train, className) {
    // The live feed has no destination field; only show one when the feed provides it.
    if ('destination' in train) append(parent, 'p', className, train.destination ? `To ${train.destination}` : 'Destination unavailable');
  }
  function trainDetails(parent, code, group) {
    // A train missing from the latest data keeps its last details, labelled with the fetch it last appeared in.
    const current = currentTrain(code);
    const { train, fetchedAt } = current ? { train: current, fetchedAt: trainsFetchedAt } : lastSeen.get(code);
    if (group) {
      const remaining = group.filter(currentTrain).length;
      const back = append(parent, 'button', 'map-panel-back', remaining ? `← All ${plural(remaining)} here` : '← Back to train list');
      back.type = 'button';
      back.addEventListener('click', () => { renderPanel({ type: 'group', codes: group }); focusGroupItem(code); });
    }
    append(parent, 'h2', '', `Train ${code}`).id = 'map-panel-title';
    if (!current) append(parent, 'p', 'train-gone', 'No longer in the latest train data.');
    if (train.direction) append(parent, 'p', 'train-direction', train.direction);
    destination(parent, train, 'train-destination');
    const lines = messageLines(train);
    append(parent, 'p', 'train-message', lines.length ? lines.join('\n') : 'No message from Irish Rail.');
    positionLabel(append(parent, 'p', 'train-position'), fetchedAt);
  }
  function groupDetails(parent, codes) {
    // Membership is rebuilt from the latest data on every render; trains that left are named, not offered.
    const current = codes.map(currentTrain).filter(Boolean);
    const gone = codes.filter(code => !currentTrain(code));
    if (!current.length) {
      append(parent, 'h2', '', 'Trains no longer reported').id = 'map-panel-title';
      append(parent, 'p', 'train-gone', 'These trains are no longer reported.');
      append(parent, 'p', 'note', `Last seen: ${gone.join(', ')}.`);
      const back = append(parent, 'button', 'map-panel-back', 'Back to map');
      back.type = 'button';
      back.addEventListener('click', close);
      return;
    }
    append(parent, 'h2', '', plural(current.length)).id = 'map-panel-title';
    append(parent, 'p', 'note', 'Reported at the same place at this zoom level. Choose a train for details.');
    const items = append(parent, 'ul', 'train-choices');
    for (const train of current) {
      const choice = append(append(items, 'li'), 'button', 'train-choice');
      choice.type = 'button';
      choice.dataset.code = train.train_code;
      append(choice, 'strong', '', train.train_code);
      append(choice, 'span', '', [train.direction, messageLines(train).at(-1)].filter(Boolean).join(' · '));
      choice.addEventListener('click', () => { renderPanel({ type: 'train', code: train.train_code, group: codes }); closeButton.focus(); });
    }
    if (gone.length) append(parent, 'p', 'train-gone', `No longer in the latest train data: ${gone.join(', ')}.`);
    positionLabel(append(parent, 'p', 'train-position'));
  }
  function focusGroupItem(code) {
    const item = [...panelContent.querySelectorAll('.train-choice')].find(button => button.dataset.code === code);
    (item || closeButton).focus();
  }

  function renderPanel(next) {
    view = next;
    panelContent.replaceChildren();
    if (view.type === 'station') {
      const station = stations.find(item => item.code === view.code);
      append(panelContent, 'h2', '', station.name).id = 'map-panel-title';
      summary(panelContent, station);
      stationTrains(panelContent, station);
      closeButton.setAttribute('aria-label', 'Close station details');
    } else if (view.type === 'train') {
      trainDetails(panelContent, view.code, view.group);
      closeButton.setAttribute('aria-label', 'Close train details');
    } else {
      groupDetails(panelContent, view.codes);
      closeButton.setAttribute('aria-label', 'Close train list');
    }
    panel.hidden = false;
  }
  function open(next, source, ref) {
    opener = source;
    openerRef = ref;
    renderPanel(next);
    if (!mapElement.contains(source)) panel.scrollIntoView({ block: 'nearest' });
    closeButton.focus();
  }
  function show(station, source) {
    selectedCode = station.code;
    selector.value = station.code;
    open({ type: 'station', code: station.code }, source, { kind: 'station', code: station.code });
    if (station.lat !== null && station.lon !== null) map.flyTo([station.lat, station.lon], Math.max(map.getZoom(), 13), { animate: false });
  }
  function focusable(element) { return element && element.isConnected && element.offsetParent !== null; }
  function openerElement() {
    if (focusable(opener)) return opener;
    // Markers and list rows are rebuilt on refresh; find the replacement for the same station or train.
    if (!openerRef) return null;
    if (openerRef.kind === 'station') {
      const element = markers.get(openerRef.code)?.getElement();
      if (element) element.tabIndex = -1;
      return element;
    }
    const element = (openerRef.kind === 'list' ? trainListButtons : trainMarkerElements).get(openerRef.code);
    return focusable(element) ? element : trainToggle;
  }
  function close() {
    panel.hidden = true;
    view = null;
    const target = openerElement();
    if (target) target.focus();
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
    if (points.length) { map.fitBounds(points, { padding: [24, 24] }); fitted = true; }
  }
  function updateLabels() {
    const shown = [];
    for (const marker of markers.values()) {
      const label = marker.getTooltip().getElement();
      if (!label) continue;
      label.hidden = map.getZoom() < 12;
      if (label.hidden) continue;
      const box = label.getBoundingClientRect();
      label.hidden = shown.some(other => box.left < other.right && box.right > other.left && box.top < other.bottom && box.bottom > other.top);
      if (!label.hidden) shown.push(box);
    }
  }
  map.on('zoomend moveend', updateLabels);
  resetButton.addEventListener('click', () => { fit(); if (!panel.hidden) close(); });
  function renderStationFreshness(failed) {
    freshness.replaceChildren();
    freshness.append('Showing station updates from the last 10 minutes');
    if (stationsUpdatedAt) { freshness.append(' · '); time(freshness, stationsUpdatedAt); }
    error.hidden = !failed;
    if (failed) {
      error.textContent = stationsUpdatedAt
        ? `Couldn't refresh station delays — showing readings last updated ${ago(stationsUpdatedAt)}.`
        : "Couldn't refresh station delays.";
    }
  }
  function render(data) {
    const openDetails = new Set([...list.querySelectorAll('details[open]')].map(item => item.id));
    stations = data.stations;
    for (const marker of markers.values()) marker.remove();
    markers.clear();
    list.replaceChildren();
    for (const station of stations) {
      if (station.lat !== null && station.lon !== null) {
        const empty = station.average_reported_delay === null;
        const icon = node('div', 'map-marker' + (empty ? ' map-marker-empty' : ''));
        append(icon, 'i', 'map-dot ' + statusClass(station.status));
        if (!empty) append(icon, 'span', 'map-marker-delay', `${station.average_reported_delay.toFixed(1)} min`);
        const marker = L.marker([station.lat, station.lon], { icon: L.divIcon({ html: icon, className: '', iconSize: empty ? [24, 24] : [82, 28] }), keyboard: false, zIndexOffset: empty ? 0 : 1000 });
        marker.bindTooltip(station.name, { permanent: true, direction: 'top', offset: [0, -10], className: 'map-station-label' });
        marker.addTo(map).on('click', () => { const element = marker.getElement(); element.tabIndex = -1; show(station, element); });
        markers.set(station.code, marker);
      }
      const detail = append(list, 'details', 'map-list-item');
      detail.id = `station-${station.code}`;
      detail.open = openDetails.has(detail.id) || station.code === selectedCode;
      const label = append(detail, 'summary');
      const name = append(label, 'span', 'map-list-name');
      append(name, 'i', 'map-dot ' + statusClass(station.status));
      append(name, 'span', '', station.name);
      append(label, 'span', '', station.status);
      append(label, 'span', '', station.average_reported_delay === null ? 'No recent data' : `Average delay: ${station.average_reported_delay.toFixed(1)} min`);
      time(append(label, 'span'), station.latest_observation_at);
      const cue = append(label, 'span', 'map-view-cue');
      append(cue, 'span', '', '⌄').setAttribute('aria-hidden', 'true');
      cue.append(' View trains');
      stationTrains(detail, station);
    }
    stationsUpdatedAt = stations.map(item => item.latest_observation_at).filter(Boolean).sort().at(-1) || null;
    renderStationFreshness(false);
    if (!fitted) fit();
    requestAnimationFrame(updateLabels);
    if (initialSelection) {
      const selected = stations.find(item => item.code === initialSelection);
      initialSelection = '';
      if (selected) show(selected, selector);
    }
    if (view?.type === 'station' && stations.some(item => item.code === view.code)) renderPanel(view);
  }
  async function refresh() {
    try {
      const response = await fetch('/api/network', { cache: 'no-store' });
      if (!response.ok) throw new Error('network unavailable');
      render(await response.json());
    } catch (_) { renderStationFreshness(true); }
  }

  // Train icons. Positions are drawn exactly where Irish Rail last reported them: no animation or route inference.
  const TRAIN_ICON = 26;
  const TRAIN_BADGE = 88;
  const TRAIN_OFFSET = 16; // Below the reported point, so the station's delay marker stays readable.
  function activate(element, handler) {
    element.addEventListener('keydown', event => {
      if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); handler(); }
    });
  }
  function renderTrainMarkers() {
    const focusedCode = [...trainMarkerElements].find(([, element]) => element === document.activeElement)?.[0];
    trainLayer.clearLayers();
    trainMarkerElements.clear();
    // Merge icons that would overlap at this zoom until none do; a merged group becomes a wider badge.
    const groups = trains.map(train => ({
      point: map.latLngToLayerPoint([train.latitude, train.longitude]), latlng: [train.latitude, train.longitude], trains: [train],
    }));
    const halfWidth = group => (group.trains.length > 1 ? TRAIN_BADGE : TRAIN_ICON) / 2;
    const overlaps = (a, b) => Math.abs(a.point.x - b.point.x) < halfWidth(a) + halfWidth(b) + 2 && Math.abs(a.point.y - b.point.y) < TRAIN_ICON + 2;
    for (let merged = true; merged;) {
      merged = false;
      for (let i = 0; i < groups.length && !merged; i++) {
        const j = groups.findIndex((other, index) => index > i && overlaps(groups[i], other));
        if (j !== -1) { groups[i].trains.push(...groups[j].trains); groups.splice(j, 1); merged = true; }
      }
    }
    for (const group of groups) {
      const single = group.trains.length === 1;
      const icon = node('div', single ? 'train-marker' : 'train-badge');
      trainGlyph(icon);
      if (!single) append(icon, 'span', '', `${group.trains.length} trains`);
      const width = single ? TRAIN_ICON : TRAIN_BADGE;
      const marker = L.marker(group.latlng, {
        icon: L.divIcon({ html: icon, className: 'train-icon', iconSize: [width, TRAIN_ICON], iconAnchor: [width / 2, -TRAIN_OFFSET] }),
        keyboard: true,
        zIndexOffset: 2000,
      }).addTo(trainLayer);
      const element = marker.getElement();
      const codes = group.trains.map(train => train.train_code);
      element.setAttribute('aria-label', single
        ? `Train ${codes[0]}${group.trains[0].direction ? `, ${group.trains[0].direction}` : ''}`
        : `${codes.length} trains: ${codes.join(', ')}`);
      for (const code of codes) trainMarkerElements.set(code, element);
      const select = () => open(single ? { type: 'train', code: codes[0] } : { type: 'group', codes },
        element, { kind: 'marker', code: codes[0] });
      marker.on('click', select);
      activate(element, select);
    }
    if (focusedCode) trainMarkerElements.get(focusedCode)?.focus();
  }
  map.on('zoomend', () => { if (trains.length) renderTrainMarkers(); });
  function renderTrainList() {
    trainList.replaceChildren();
    trainListButtons.clear();
    for (const train of trains) {
      const item = append(trainList, 'li', 'train-list-item');
      const heading = append(item, 'div', 'train-list-heading');
      append(heading, 'strong', '', train.train_code);
      if (train.direction) append(heading, 'span', '', train.direction);
      destination(item, train, 'train-destination');
      append(item, 'p', 'train-message-compact', messageLines(train).join(' · ') || 'No message from Irish Rail.');
      positionLabel(append(item, 'p', 'train-position'));
      const button = append(item, 'button', 'train-show', 'Details');
      button.type = 'button';
      button.setAttribute('aria-label', `Show details for train ${train.train_code}`);
      button.addEventListener('click', () => {
        open({ type: 'train', code: train.train_code }, button, { kind: 'list', code: train.train_code });
      });
      trainListButtons.set(train.train_code, button);
    }
  }
  function renderTrainStatus() {
    const failed = trainState === 'stale' || trainState === 'error';
    // When a refresh fails the error line carries the age, so the freshness line is not repeated.
    trainFreshness.hidden = failed || (!trainsFetchedAt && trainState !== 'loading');
    trainFreshness.replaceChildren();
    if (trainState === 'loading') trainFreshness.append('Loading train positions…');
    else if (trainsFetchedAt) {
      if (trainState === 'ok' && !trains.length) trainFreshness.append('No running trains in this area · ');
      trainFreshness.append('Train positions fetched ');
      stamp(trainFreshness, trainsFetchedAt, ago(trainsFetchedAt));
    }
    trainError.hidden = !failed;
    if (failed) {
      trainError.textContent = trainsFetchedAt
        ? `Couldn't refresh train positions — showing positions fetched ${ago(trainsFetchedAt)}.`
        : "Couldn't refresh train positions — none available yet.";
    }
    trainListStatus.textContent = failed ? trainError.textContent
      : trainState === 'loading' ? 'Loading train positions…'
        : trains.length ? 'Each train is shown at the last station Irish Rail reported.' : 'No running trains in this area.';
    trainCount.textContent = trainsFetchedAt ? `${trains.length} running` : '';
  }
  function showTrains(data) {
    trains = [...data.trains].sort((a, b) => a.train_code.localeCompare(b.train_code));
    trainsFetchedAt = data.fetched_at;
    for (const train of trains) lastSeen.set(train.train_code, { train, fetchedAt: trainsFetchedAt });
    renderTrainMarkers();
    renderTrainList();
  }
  async function refreshTrains() {
    let data = null;
    try {
      const response = await fetch('/api/trains', { cache: 'no-store' });
      if (response.ok) data = await response.json();
    } catch (_) { data = null; }
    const valid = data && Array.isArray(data.trains) && ['ok', 'stale', 'error'].includes(data.status);
    trainState = valid ? data.status : 'error';
    // Each web process caches separately, so never replace newer positions with an older stale copy.
    if (trainState === 'ok' || (trainState === 'stale' && data.fetched_at && (!trainsFetchedAt || data.fetched_at >= trainsFetchedAt))) {
      showTrains(data);
    } else if (trains.length) {
      renderTrainList(); // Keep the icons, but refresh the ages shown in the list.
    }
    renderTrainStatus();
    const focusInside = panelContent.contains(document.activeElement);
    if (view && view.type !== 'station' && !focusInside) renderPanel(view);
  }
  // Hidden with CSS rather than removing the layer, so grouping and markers stay current while hidden.
  trainToggle.addEventListener('change', () => mapElement.classList.toggle('trains-hidden', !trainToggle.checked));
  renderTrainStatus();
  map.whenReady(() => {
    refresh();
    refreshTrains();
    setInterval(() => { refresh(); refreshTrains(); }, 60000);
  });
})();
