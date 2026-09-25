// Interaction review of the live map's train layer against the disposable stack in README.md.
// Runs every scenario in Chromium and WebKit, at 1280x800 and 375x812, in light and dark mode.
import { mkdirSync } from 'node:fs';
import { chromium, webkit } from 'playwright';

const BASE = process.env.BASE || 'http://localhost:8001';
const SHOTS = process.env.SHOTS || new URL('./shots/', import.meta.url).pathname;
mkdirSync(SHOTS, { recursive: true });
const combos = [];
const ONLY = process.env.ONLY; // e.g. ONLY=webkit-375-dark
for (const [browserName, browserType] of [['chromium', chromium], ['webkit', webkit]])
  for (const [vw, vh] of [[1280, 800], [375, 812]])
    for (const scheme of ['light', 'dark']) if (!ONLY || ONLY === `${browserName}-${vw}-${scheme}`) combos.push({ browserName, browserType, vw, vh, scheme });

const iso = minutesAgo => new Date(Date.now() - minutesAgo * 60000).toISOString().replace(/\.\d+Z$/, '+00:00');
const train = (code, lat, lon, message, extra = {}) => ({ train_code: code, latitude: lat, longitude: lon, direction: 'Northbound', public_message: message, ...extra });
const json = body => route => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });

function check(results, name, ok, detail = '') { results.push({ name, ok: !!ok, detail }); }
const visibleIcons = page => page.locator('.train-icon:visible').count();
const active = page => page.evaluate(() => {
  const el = document.activeElement;
  return { label: el.getAttribute('aria-label') || '', text: el.textContent.trim().slice(0, 40), station: !!el.querySelector?.('.map-marker') };
});

async function newPage(browser, c) {
  const context = await browser.newContext({ viewport: { width: c.vw, height: c.vh }, colorScheme: c.scheme });
  const page = await context.newPage();
  page.on('pageerror', e => page._errors = [...(page._errors || []), e.message]);
  return page;
}

async function run(c) {
  const tag = `${c.browserName}-${c.vw}-${c.scheme}`;
  const results = [];
  const shots = [];
  const shot = async (page, name) => { const path = `${SHOTS}${tag}-${name}.png`; await page.screenshot({ path, fullPage: false }); shots.push(path); };
  const browser = await c.browserType.launch();
  try {
    // Both flows wait for a 60-second refresh, so they run side by side.
    await Promise.all([mainFlow(browser, c, results, shot), disappearingTrains(browser, c, results, shot)]);
  } catch (e) {
    check(results, 'script completed', false, e.message.split('\n').slice(0, 12).join(' | '));
  } finally {
    await browser.close();
  }
  return { tag, results, shots };
}

async function mainFlow(browser, c, results, shot) {
  {
    // Main flow against the seeded fixture upstream.
    const page = await newPage(browser, c);
    await page.goto(`${BASE}/map`);
    await page.waitForSelector('.train-icon');
    await page.waitForTimeout(500);
    await page.locator('#network-map').scrollIntoViewIfNeeded();
    await shot(page, '01-overview');
    const listItems = await page.locator('.train-list-item').count();
    check(results, 'list shows all in-bounds trains', listItems === 10, `${listItems} items`);
    check(results, 'legend entry', await page.getByText('Train (last reported position)').isVisible());
    check(results, 'no destination row without feed field', await page.locator('.train-destination').count() === 0);

    check(results, 'no overlapping train icons at default zoom', await page.evaluate(() => { const r = [...document.querySelectorAll('.train-icon')].map(e => e.getBoundingClientRect()); let n = 0; for (let i = 0; i < r.length; i++) for (let j = i + 1; j < r.length; j++) if (r[i].left < r[j].right && r[i].right > r[j].left && r[i].top < r[j].bottom && r[i].bottom > r[j].top) n++; return n; }) === 0);
    // Toggle.
    await page.locator('#map-show-trains').uncheck();
    check(results, 'toggle hides train icons', await visibleIcons(page) === 0);
    await page.locator('#map-show-trains').check();
    check(results, 'toggle shows train icons', await visibleIcons(page) > 0);

    // Grouping at two zooms.
    const labelsAt = () => page.locator('.train-icon').evaluateAll(els => els.map(e => e.getAttribute('aria-label')));
    const initial = await labelsAt();
    await page.selectOption('#map-station', 'CNLLY');
    await page.keyboard.press('Escape');
    await page.locator('.leaflet-control-zoom-in').click();
    await page.waitForTimeout(400);
    await page.locator('.leaflet-control-zoom-in').click();
    await page.waitForTimeout(600);
    const zoomed = await labelsAt();
    check(results, 'no overlapping train icons when zoomed', await page.evaluate(() => { const r = [...document.querySelectorAll('.train-icon')].map(e => e.getBoundingClientRect()); let n = 0; for (let i = 0; i < r.length; i++) for (let j = i + 1; j < r.length; j++) if (r[i].left < r[j].right && r[i].right > r[j].left && r[i].top < r[j].bottom && r[i].bottom > r[j].top) n++; return n; }) === 0);
    const connollyBadge = zoomed.find(l => l.includes('D837'));
    check(results, 'grouping changes with zoom', JSON.stringify(initial) !== JSON.stringify(zoomed),
      `initial ${initial.filter(l => /^\d+ trains/.test(l)).map(l => l.split(':')[0]).join('/')} badges vs zoomed ${zoomed.filter(l => /^\d+ trains/.test(l)).map(l => l.split(':')[0]).join('/')}`);
    check(results, 'co-located trains stay grouped when zoomed in', connollyBadge?.startsWith('3 trains'), connollyBadge);
    check(results, 'nearby Tara Street train separates when zoomed in', zoomed.some(l => l.startsWith('Train E947')));

    // Badge expansion, keyboard selection, back, focus return.
    const badge = page.locator('.train-icon[aria-label*="D837"]');
    await badge.click();
    const choices = page.locator('#map-panel .train-choice');
    check(results, 'badge expands into list', await choices.count() === 3 && (await page.locator('#map-panel h2').textContent()) === '3 trains');
    await page.waitForTimeout(200);
    await shot(page, '02-group');
    await choices.first().focus();
    await page.keyboard.press('Enter');
    check(results, 'keyboard selects train from badge list', (await page.locator('#map-panel h2').textContent()) === 'Train D837');
    const detail = await page.locator('#map-panel').innerText();
    check(results, 'train panel shows message and position label', detail.includes('Arrived Dublin Connolly next stop Howth Junction') && detail.includes('Last reported position · data fetched'));
    await shot(page, '03-train');
    await page.locator('.map-panel-back').click();
    check(results, 'back returns to group with focus on train', (await active(page)).text.startsWith('D837'));
    await page.keyboard.press('Escape');
    check(results, 'closing returns focus to badge', (await active(page)).label.includes('D837'), (await active(page)).label);

    // Single marker by keyboard.
    await page.locator('.train-icon[aria-label^="Train E947"]').focus();
    await page.keyboard.press('Enter');
    check(results, 'Enter opens single train', (await page.locator('#map-panel h2').textContent()) === 'Train E947');
    await page.locator('#map-panel-close').click();
    check(results, 'close returns focus to train marker', (await active(page)).label.startsWith('Train E947'));

    // Station marker.
    await page.evaluate(() => document.getElementById('network-map').scrollIntoView({ block: 'center' }));
    await page.waitForTimeout(1500); // Let smooth scrolling settle before measuring what is in view.
    await page.evaluate(() => {
      const box = document.getElementById('network-map').getBoundingClientRect();
      const inView = [...document.querySelectorAll('.leaflet-marker-icon:has(.map-marker)')].find(el => {
        const r = el.getBoundingClientRect();
        return r.left > box.left + 40 && r.right < box.right - 40 && r.top > Math.max(box.top, 0) + 40 && r.bottom < Math.min(box.bottom, innerHeight) - 60;
      });
      inView.dataset.review = 'station';
    });
    await page.locator('[data-review="station"]').click();
    await page.keyboard.press('Escape');
    check(results, 'close returns focus to station marker', (await active(page)).station);

    // List item and escaping.
    await page.locator('button[aria-label="Show details for train X001"]').click();
    const panelText = await page.locator('#map-panel').innerText();
    check(results, 'feed text is escaped', panelText.includes('<img src=x') && await page.locator('#map-panel img, .train-list img').count() === 0 && (await page.title()) !== 'XSS');
    if (c.vw < 640) {
      const sheet = await page.locator('#map-panel').evaluate(e => { const s = getComputedStyle(e); const r = e.getBoundingClientRect(); return s.position === 'fixed' && Math.abs(r.bottom - innerHeight) < 2; });
      check(results, 'mobile bottom sheet', sheet);
      await shot(page, '04-sheet');
    }
    await page.keyboard.press('Escape');
    check(results, 'close returns focus to list item', (await active(page)).label === 'Show details for train X001');

    // Failure after a good load: both sources fail on the next 60 s cycle.
    const before = await visibleIcons(page);
    await page.route('**/api/trains', route => route.abort());
    await page.route('**/api/network', route => route.fulfill({ status: 500, body: 'down' }));
    await page.locator('#train-error').waitFor({ state: 'visible', timeout: 75000 });
    await page.locator('#map-error').waitFor({ state: 'visible', timeout: 5000 });
    check(results, 'failure keeps last icons', await visibleIcons(page) === before, `${before}`);
    check(results, 'train failure shows age', /Couldn't refresh train positions — showing positions fetched (\d+ min ago|just now)/.test(await page.locator('#train-error').textContent()), await page.locator('#train-error').textContent());
    check(results, 'station failure shows age', /last updated \d+ min ago/.test(await page.locator('#map-error').textContent()), await page.locator('#map-error').textContent());
    await page.locator('#network-map').scrollIntoViewIfNeeded();
    await shot(page, '05-failure');
    check(results, 'no page errors (main)', !page._errors, (page._errors || []).join('; '));

    // First-load states.
    const scenario = async (name, trainsBody, network) => {
      const p = await newPage(browser, c);
      await p.route('**/api/trains', json(trainsBody));
      if (network) await p.route('**/api/network', network);
      await p.goto(`${BASE}/map`);
      await p.waitForFunction(() => document.getElementById('train-freshness').textContent !== 'Loading train positions…');
      await p.waitForTimeout(700);
      return p;
    };
    let p = await scenario('empty', { status: 'ok', trains: [], fetched_at: iso(0) });
    check(results, 'empty: message and no icons', (await p.locator('#train-freshness').textContent()).includes('No running trains in this area') && await visibleIcons(p) === 0);
    await p.locator('#network-map').scrollIntoViewIfNeeded();
    await shot(p, '06-empty');
    p = await scenario('stale', { status: 'stale', fetched_at: iso(5), trains: [train('E846', 53.2364, -6.11691, 'Arrived Shankill'), train('E260', 53.4169, -6.1512, 'Departed Portmarnock')] });
    check(results, 'first-load stale shows icons with age', await visibleIcons(p) === 2 && (await p.locator('#train-error').textContent()).includes('5 min ago'), await p.locator('#train-error').textContent());
    await p.locator('#network-map').scrollIntoViewIfNeeded();
    await shot(p, '07-stale');
    p = await scenario('error', { status: 'error', reason: 'x', trains: [], fetched_at: null });
    check(results, 'first-load error without cache', (await p.locator('#train-error').textContent()).includes('none available yet') && await visibleIcons(p) === 0);
    check(results, 'train error leaves stations fresh', await p.locator('#map-error').isHidden());
    p = await scenario('stations-down', { status: 'ok', fetched_at: iso(0), trains: [train('E846', 53.2364, -6.11691, 'Arrived Shankill')] }, route => route.fulfill({ status: 500 }));
    check(results, 'station error leaves trains fresh', await p.locator('#map-error').isVisible() && await p.locator('#train-error').isHidden());
    p = await scenario('destination', { status: 'ok', fetched_at: iso(0), trains: [train('E947', 53.347, -6.25425, 'Arrived Tara Street', { destination: 'Howth' }), train('E131', 53.3531, -6.24591, 'Arrived Connolly', { destination: null })] });
    const destinations = await p.locator('.train-list .train-destination').allTextContents();
    check(results, 'destination field shown or "Destination unavailable"', JSON.stringify(destinations) === JSON.stringify(['Destination unavailable', 'To Howth']), JSON.stringify(destinations));
  }
}

// Trains leaving the feed while their panel is open: a single train, part of a group, and a whole group.
const CONNOLLY = [53.3531, -6.24591];
const DUN_LAOGHAIRE = [53.2951, -6.13498];
const BEFORE = [
  train('D837', ...CONNOLLY, 'Arrived Dublin Connolly next stop Howth Junction'),
  train('E131', ...CONNOLLY, 'Arrived Dublin Connolly next stop Tara Street'),
  train('P713', ...CONNOLLY, 'Arrived Dublin Connolly'),
  train('E947', 53.347, -6.25425, 'Arrived Tara Street next stop Dublin Connolly'),
  train('E130', ...DUN_LAOGHAIRE, 'Departed Dun Laoghaire next stop Sandycove'),
  train('E948', ...DUN_LAOGHAIRE, 'Arrived Dun Laoghaire next stop Salthill and Monkstown'),
  train('E846', 53.2364, -6.11691, 'Arrived Shankill next stop Killiney'),
];
async function disappearingTrains(browser, c, results, shot) {
  const before = { status: 'ok', fetched_at: iso(3), trains: BEFORE };
  const after = removed => ({ status: 'ok', fetched_at: iso(0), trains: [
    ...BEFORE.filter(item => !removed.includes(item.train_code)),
    train('X900', ...CONNOLLY, 'Arrived Dublin Connolly'), // A newcomer must not join a group that is already open.
  ] });
  const openPage = async () => {
    const page = await newPage(browser, c);
    page._body = before;
    await page.route('**/api/trains', route => json(page._body)(route));
    await page.goto(`${BASE}/map`);
    await page.waitForSelector('.train-icon');
    await page.waitForTimeout(500);
    return page;
  };
  const codesOf = async (page, code) => (await page.locator(`.train-icon[aria-label*="${code}"]`).getAttribute('aria-label'))
    .replace(/^.*?: /, '').split(', ');
  const panelText = page => page.locator('#map-panel').innerText();
  const refreshed = page => page.locator('#train-freshness time').filter({ hasText: 'just now' }).waitFor({ timeout: 75000 });

  const single = async () => {
    const page = await openPage();
    await page.locator('button[aria-label="Show details for train E947"]').click();
    page._body = after(['E947']);
    await refreshed(page);
    const text = await panelText(page);
    check(results, 'gone train: panel says it is no longer reported', text.includes('Train E947') && text.includes('No longer in the latest train data.'));
    const stamp = await page.locator('#map-panel .train-position time').getAttribute('datetime');
    check(results, 'gone train: label keeps the fetch it was last seen in', stamp === before.fetched_at && !text.includes('just now'), `${stamp} · ${text.split('\n').at(-1)}`);
    check(results, 'gone train: list row removed', await page.locator('button[aria-label="Show details for train E947"]').count() === 0);
    await shot(page, '08-train-gone');
    await page.keyboard.press('Escape');
    check(results, 'gone train: closing moves focus somewhere usable', (await page.evaluate(() => document.activeElement.id)) === 'map-show-trains');
  };
  const partial = async () => {
    const page = await openPage();
    const codes = await codesOf(page, 'D837');
    await page.locator('.train-icon[aria-label*="D837"]').click();
    page._body = after(['D837']);
    await refreshed(page);
    const choices = await page.locator('#map-panel .train-choice').evaluateAll(els => els.map(e => e.dataset.code));
    const heading = await page.locator('#map-panel h2').textContent();
    check(results, 'group change: departed train dropped, newcomer not added',
      JSON.stringify(choices) === JSON.stringify(codes.filter(code => code !== 'D837')), `${codes} -> ${choices}`);
    check(results, 'group change: heading counts current trains', heading === `${codes.length - 1} train${codes.length - 1 === 1 ? '' : 's'}`, heading);
    check(results, 'group change: departed train named', (await panelText(page)).includes('No longer in the latest train data: D837.'));
    await shot(page, '09-group-changed');
    await page.locator('#map-panel .train-choice').first().click();
    await page.locator('.map-panel-back').click();
    check(results, 'group change: back button uses current data', (await page.locator('#map-panel h2').textContent()) === heading);
  };
  const allGone = async () => {
    const page = await openPage();
    const codes = await codesOf(page, 'E130');
    await page.locator('.train-icon[aria-label*="E130"]').click();
    page._body = after(codes);
    await refreshed(page);
    const text = await panelText(page);
    check(results, 'group gone: says these trains are no longer reported', text.includes('These trains are no longer reported.') && await page.locator('#map-panel .train-choice').count() === 0, codes.join(','));
    await shot(page, '10-group-gone');
    await page.getByRole('button', { name: 'Back to map' }).click();
    check(results, 'group gone: back to map closes the panel', await page.locator('#map-panel').isHidden());
    check(results, 'group gone: focus moves somewhere usable', (await page.evaluate(() => document.activeElement.id)) === 'map-show-trains');
  };
  await Promise.all([single(), partial(), allGone()]);
}

const all = await Promise.all(combos.map(run));
let failures = 0;
for (const { tag, results } of all) {
  const failed = results.filter(r => !r.ok);
  failures += failed.length;
  console.log(`${tag}: ${results.length - failed.length}/${results.length} passed`);
  for (const r of results) if (!r.ok || r.detail) console.log(`  ${r.ok ? 'ok  ' : 'FAIL'} ${r.name}${r.detail ? ` — ${r.detail}` : ''}`);
}
console.log(`screenshots: ${all.reduce((n, a) => n + a.shots.length, 0)} in ${SHOTS}`);
process.exit(failures ? 1 : 0);
