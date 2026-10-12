// Run with: node --test tests/test_dashboard_js.cjs
// Executes the actual dashboard script with a small DOM/Chart harness, not a browser.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const template = fs.readFileSync(path.join(__dirname, '../app/templates/_dashboard_script.html'), 'utf8');

class Element {
    constructor() { this.children = []; this.style = {}; this.value = ''; this.className = ''; }
    appendChild(child) { this.children.push(child); return child; }
    replaceChildren(...children) { this.children = children; }
    set textContent(value) { this.text = String(value); this.children = []; }
    get textContent() { return this.text || ''; }
    setAttribute(key, value) { this[key] = value; }
    setCustomValidity(value) { this.validation = value; }
    reportValidity() { this.reported = true; }
    select() {}
    getContext() { return {}; }
}

function dashboard(device = 'esp32_sensor') {
    const elements = new Map();
    const element = id => {
        if (!elements.has(id)) elements.set(id, new Element());
        return elements.get(id);
    };
    class Chart {
        static defaults = {};
        constructor(_, config) { Object.assign(this, config); this.updates = 0; }
        update() { this.updates++; }
    }
    const context = vm.createContext({
        document: {
            getElementById: element,
            createElement: () => new Element(),
            createDocumentFragment: () => new Element(),
            querySelectorAll: () => [],
            addEventListener: () => {},
        },
        Chart, URLSearchParams, AbortController,
        console: { log() {}, error() {} },
        fetch: async () => { throw new Error('Unexpected network call'); },
    });
    const source = template.replace(/^<script>\s*/, '').replace(/<\/script>\s*$/, '')
        .replace("{{ (current_device_id or '')|tojson }}", JSON.stringify(device));
    assert.ok(!source.includes('{{'), 'all Jinja values must be supplied');
    vm.runInContext(source, context);
    return { context, element, run: code => vm.runInContext(code, context) };
}

test('full script parses and creates all five charts', () => {
    const { run } = dashboard();
    assert.equal(run('Object.keys(charts).length'), 4);
    assert.equal(run('growthChart.type'), 'line');
});

test('appendTextCell returns the cell and history table renders safely', () => {
    const { context, element } = dashboard();
    const row = new Element();
    assert.equal(context.appendTextCell(row, '<img onerror=alert(1)>'), row.children[0]);
    context.renderHistoryTable([{device_id: '<script>bad</script>', temperature: 23}], 2, 10);
    const cells = element('history-tbody').children[0].children;
    assert.equal(cells.length, 7);
    assert.equal(cells[0].textContent, '11');
    assert.equal(cells[0].style.color, '#475569');
    assert.equal(cells[2].textContent, '<script>bad</script>');
    assert.equal(cells[4].textContent, '—');
});

test('device ID query encoding and all-devices mode are preserved', () => {
    const url = dashboard('a&b').context.apiUrl('/api/dashboard/latest', {period: 'daily'});
    assert.equal(new URLSearchParams(url.split('?')[1]).get('device_id'), 'a&b');
    assert.equal(dashboard('').context.apiUrl('/api/dashboard/latest'), '/api/dashboard/latest');
});

test('digital light is never evaluated as lux', () => {
    const { run, context } = dashboard();
    run("_lightUnit = 'digital'");
    assert.equal(context.formatLightValue(0), '밝음');
    assert.equal(context.formatLightValue(1), '어두움');
    assert.equal(context.basilStatus('light', 1).cls, 'status-unknown');
    assert.equal(context.formatLightValue(null), '—');
});

test('events show sensor values, recovery and offline durations', () => {
    const { context, element } = dashboard();
    context.renderEventsTable([
        {metric: 'temperature', value: 40, unit: '°C', severity: 'danger', threshold_min: 18, threshold_max: 25},
        {metric: 'device', status: 'recovered', value: 120, threshold_max: 60},
    ]);
    const rows = element('events-tbody').children;
    assert.equal(rows[0].children[4].textContent, '40°C');
    assert.equal(rows[0].children[5].textContent, '18 ~ 25°C');
    assert.match(rows[0].children[3].children[0].className, /event-danger/);
    assert.equal(rows[1].children[3].children[0].textContent, '복구');
    assert.equal(rows[1].children[4].textContent, '2분 전');
});

test('empty event list renders a six-column placeholder', () => {
    const { context, element } = dashboard();
    context.renderEventsTable([]);
    assert.equal(element('events-tbody').children[0].children[0].colSpan, 6);
});

test('event pagination remains bounded for a million pages', () => {
    const { context, element } = dashboard();
    context.renderEventsPagination(500000, 1000000, 10000000);
    const html = element('events-pagination').innerHTML;
    assert.ok(html.length < 5000);
    assert.match(html, /max="1000000"/);
});

test('shared pagination preserves legacy buttons and ellipses on every boundary', () => {
    const { context, element } = dashboard();
    function legacy(page, pages, total, handler) {
        const btn = (label, p, active = false, disabled = false) =>
            `<button class="page-btn${active ? ' active' : ''}" ${disabled ? 'disabled' : ''}
            onclick="${disabled || active ? '' : `${handler}(${p})`}">${label}</button>`;
        let html = btn('‹', page - 1, false, page === 1);
        if (pages <= 1) html += btn(1, 1, true);
        else {
            let previous = -1;
            for (let p = 1; p <= pages; p++) {
                if (p === 1 || p === pages || Math.abs(p - page) <= 2) {
                    if (previous !== -1 && p - previous > 1) html += '<span class="page-info">…</span>';
                    html += btn(p, p, p === page);
                    previous = p;
                }
            }
        }
        return html + btn('›', page + 1, false, page === pages) + `<span class="page-info">총 ${total}건</span>`;
    }
    for (let pages = 1; pages <= 50; pages++) {
        for (let page = 1; page <= pages; page++) {
            context.renderPagination(page, pages, pages * 10);
            assert.equal(element('pagination').innerHTML, legacy(page, pages, pages * 10, 'loadHistory'));
            context.renderEventsPagination(page, pages, pages * 10);
            assert.ok(element('events-pagination').innerHTML.startsWith(legacy(page, pages, pages * 10, 'loadEvents')));
            assert.match(element('events-pagination').innerHTML, /jumpToEventsPage\(event\)/);
        }
    }
});

test('history pagination handles a million pages without traversing all pages', () => {
    const { context, element } = dashboard();
    context.renderPagination(500000, 1000000, 10000000);
    const html = element('pagination').innerHTML;
    assert.ok(html.length < 4000);
    assert.match(html, /loadHistory\(1000000\)/);
    assert.equal((html.match(/<button /g) || []).length, 9);
});

test('page jump validates bounds before issuing a request', () => {
    const { run, context, element } = dashboard();
    run('eventsMeta.pages = 8');
    let requested;
    context.loadEvents = page => { requested = page; };
    for (const invalid of ['0', '9', '1.5', '']) {
        element('events-page-input').value = invalid;
        context.jumpToEventsPage({preventDefault() {}});
        assert.equal(requested, undefined);
        assert.ok(element('events-page-input').validation);
    }
    element('events-page-input').value = '7';
    context.jumpToEventsPage({preventDefault() {}});
    assert.equal(requested, 7);
    assert.equal(element('events-page-input').validation, '');
});

test('events API and CSV use the same date range', async () => {
    const { context, run } = dashboard();
    run("eventsFilters = {metric: 'temperature', date_from: '2026-07-01', date_to: '2026-08-31', time_from: '09:00', time_to: '18:00'}");
    let requestUrl, csv;
    context.fetchJson = async (_, url) => { requestUrl = url; return undefined; };
    context.downloadCsv = (_, values) => { csv = values; };
    await context.loadEvents(3);
    context.exportEventsCsv();
    const params = new URLSearchParams(requestUrl.split('?')[1]);
    for (const field of ['metric', 'date_from', 'date_to', 'time_from', 'time_to']) {
        assert.equal(params.get(field), csv[field]);
    }
    assert.equal(params.get('page'), '3');
});

test('late averages and chart responses cannot replace current period', async () => {
    const { context, run, element } = dashboard();
    context.fetchJson = async () => ({period: 'daily', temperature: 99});
    run("currentAvgPeriod = 'monthly'; currentChartPeriod = 'weekly'");
    element('avg-temp').textContent = 'unchanged';
    await context.loadAverages('daily');
    await context.loadCharts('daily');
    assert.equal(element('avg-temp').textContent, 'unchanged');
    assert.equal(run('charts.temp.updates'), 0);
});

test('fetchJson aborts superseded requests and cleans up controllers', async () => {
    const { context, run } = dashboard();
    let oldSignal;
    context.fetch = (_, options) => new Promise((resolve, reject) => {
        oldSignal = options.signal;
        options.signal.addEventListener('abort', () => reject(Object.assign(new Error(), {name: 'AbortError'})));
    });
    const first = context.fetchJson('events', '/first');
    context.fetch = async () => ({ok: true, json: async () => ({value: 2})});
    const second = await context.fetchJson('events', '/second');
    assert.equal(await first, undefined);
    assert.equal(oldSignal.aborted, true);
    assert.equal(second.value, 2);
    assert.equal(run('Object.keys(REQUESTS).length'), 0);
});

test('401 opens authentication UI and failed requests clean up', async () => {
    const { context, run } = dashboard();
    let opened = false;
    context.openAuthModal = () => { opened = true; };
    context.fetch = async () => ({ok: false, status: 401, json: async () => ({error: 'Unauthorized'})});
    await assert.rejects(context.fetchJson('events', '/private'), /Unauthorized/);
    assert.equal(opened, true);
    assert.equal(run('Object.keys(REQUESTS).length'), 0);
});

test('alert settings show both webhook states without displaying secret URLs', async () => {
    const { context, element } = dashboard();
    context.fetchJson = async () => ({settings: {
        webhook_configured: true, summary_webhook_configured: true,
        summary_webhook_inherited: true,
    }});
    for (const id of ['alert-webhook', 'alert-summary-webhook']) {
        element(id).value = 'previous-secret';
        element(`${id}-clear`).checked = true;
    }
    await context.loadAlertSettings();
    assert.equal(element('alert-webhook').value, '');
    assert.equal(element('alert-summary-webhook').value, '');
    assert.match(element('alert-webhook').placeholder, /설정되어 있습니다/);
    assert.match(element('alert-summary-webhook').placeholder, /전체 요약 웹훅 사용 중/);
    assert.equal(element('alert-webhook-clear').checked, false);
    assert.equal(element('alert-summary-webhook-clear').checked, false);
});

test('webhook inputs save independently and blank inputs preserve stored URLs', async () => {
    const { context, element } = dashboard();
    for (const [id, value] of Object.entries({
        'alert-abnormal-count': '1', 'alert-duration': '0',
        'alert-recovery-count': '1', 'alert-danger-percent': '25',
        'alert-summary-hour': '8', 'alert-summary-weekday': '0',
    })) element(id).value = value;
    context.loadAlertSettings = async () => {};
    let payload;
    context.fetch = async (url, options) => {
        assert.equal(url, '/api/alert-settings');
        payload = JSON.parse(options.body);
        return {ok: true, json: async () => ({})};
    };
    element('alert-webhook').value = ' https://discord.com/api/webhooks/alert/token ';
    element('alert-summary-webhook').value = ' https://discord.com/api/webhooks/summary/token ';
    await context.saveAlertSettings();
    assert.equal(payload.webhook_url, 'https://discord.com/api/webhooks/alert/token');
    assert.equal(payload.summary_webhook_url, 'https://discord.com/api/webhooks/summary/token');
    element('alert-webhook').value = '';
    element('alert-summary-webhook').value = '  ';
    await context.saveAlertSettings();
    assert.equal('webhook_url' in payload, false);
    assert.equal('summary_webhook_url' in payload, false);
    element('alert-summary-webhook-clear').checked = true;
    await context.saveAlertSettings();
    assert.equal('webhook_url' in payload, false);
    assert.equal(payload.summary_webhook_url, null);
    element('alert-summary-webhook-clear').checked = false;
    element('alert-webhook-clear').checked = true;
    await context.saveAlertSettings();
    assert.equal(payload.webhook_url, null);
    assert.equal('summary_webhook_url' in payload, false);
});

test('settings load failure displays an error without erasing webhook edits', async () => {
    const { context, element } = dashboard();
    context.fetch = async () => ({
        ok: false, status: 503,
        json: async () => ({error: 'Database temporarily unavailable'}),
    });
    element('alert-webhook').value = 'unsaved-alert-url';
    element('alert-summary-webhook').value = 'unsaved-summary-url';
    let message, kind;
    context.setAlertMessage = (text, type) => { message = text; kind = type; };
    await context.loadAlertSettings();
    assert.equal(kind, 'error');
    assert.match(message, /불러오지 못했습니다/);
    assert.equal(element('alert-webhook').value, 'unsaved-alert-url');
    assert.equal(element('alert-summary-webhook').value, 'unsaved-summary-url');
});

test('failed settings reset stays an error without reloading or clearing inputs', async () => {
    const { context, element } = dashboard();
    context.confirm = () => true;
    context.fetch = async (_, options) => {
        assert.equal(options.method, 'DELETE');
        return {ok: false, status: 503};
    };
    let reloads = 0;
    context.loadAlertSettings = () => { reloads++; };
    const messages = [];
    context.setAlertMessage = (text, type) => messages.push({text, type});
    element('alert-summary-webhook').value = 'unsaved-summary-url';
    await context.resetAlertSettings();
    assert.equal(reloads, 0);
    assert.equal(messages.length, 1);
    assert.equal(messages[0].type, 'error');
    assert.match(messages[0].text, /되돌리기에 실패했습니다/);
    assert.equal(element('alert-summary-webhook').value, 'unsaved-summary-url');
});
