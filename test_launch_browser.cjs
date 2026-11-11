const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('put in tavern extensions/quinn-bridge/index.js', 'utf8');
const managed = source.slice(source.indexOf('async function managedLaunch()'), source.indexOf('async function forget()'));

async function fixture(options = {}) {
    const timers = [], reports = [], storage = new Map();
    let starts = 0, closes = 0, pauses = 0, replaced = false;
    const state = { desired: 'run', autostart: true, ...options };
    const context = {
        URLSearchParams, Promise, Date,
        location: { hash: '#quinn-launch=test-session', pathname: '/', search: '' },
        history: { state: null, replaceState() { replaced = true; } },
        sessionStorage: { setItem: (key, value) => storage.set(key, value), getItem: key => storage.get(key) },
        ctx: () => ({ characters: [{ name: 'Quinn' }] }),
        config: key => key === 'char' ? 'Quinn' : 'http://localhost:8765',
        document: { querySelector: () => ({ value: '' }) },
        window: { addEventListener() {}, close() { closes++; } },
        fetch: async () => {},
        setTimeout: fn => { timers.push(fn); },
        pause: () => { pauses++; }, render() {},
        start: async () => { starts++; if (options.startError) throw Error(options.startError); },
        api: async (path, body) => {
            if (body) { reports.push(body); return {}; }
            return state;
        },
        busy: false, error: '',
    };
    vm.createContext(context);
    vm.runInContext(managed, context);
    await context.managedLaunch();
    return { context, state, timers, reports, storage, counts: () => ({ starts, closes, pauses, replaced }) };
}

(async () => {
    const normal = await fixture();
    assert.equal(normal.counts().starts, 1);
    assert.equal(normal.counts().replaced, true);
    assert.equal(normal.storage.get('quinn-launch'), 'test-session');
    assert.equal(normal.reports.at(-1).state, 'ready');
    normal.state.desired = 'drain';
    await normal.timers.shift()();
    assert.equal(normal.counts().pauses, 1);
    normal.state.desired = 'close';
    await normal.timers.shift()();
    assert.equal(normal.counts().closes, 1);
    await normal.timers.shift()();
    assert.equal(normal.reports.at(-1).state, 'blocked');
    const standalone = await fixture({ autostart: false });
    assert.equal(standalone.counts().starts, 0);
    const conflict = await fixture({ startError: 'Another worker owns this browser session' });
    assert.equal(conflict.reports.at(-1).state, 'failed');
    assert.match(conflict.reports.at(-1).error, /Another worker/);
    const ordinary = await fixture({ autostart: false });
    ordinary.context.location.hash = '';
    ordinary.storage.clear();
    const count = ordinary.reports.length;
    await ordinary.context.managedLaunch();
    assert.equal(ordinary.reports.length, count);
    console.log('Browser lifecycle scenarios passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
