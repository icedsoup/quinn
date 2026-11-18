const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const source = fs.readFileSync('put in tavern extensions/quinn-bridge/index.js', 'utf8');
const stream = source.slice(source.indexOf('    if (events.STREAM_TOKEN_RECEIVED)'), source.indexOf('    c.eventSource.on(events.CHAT_CHANGED'));
const updates = [];
let callback, now = 1000, chatId = 'conversation';
const active = { generating: true, file: chatId, job: { job_id: 'job' }, inferenceStarted: 900, lastStreamReport: 0, streamUpdates: 0 };
vm.runInNewContext(stream, {
    active, events: { STREAM_TOKEN_RECEIVED: 'stream' },
    c: { eventSource: { on: (_, handler) => { callback = handler; } } },
    ctx: () => ({ chatId }), performance: { now: () => now },
    telemetry: (...args) => updates.push(args),
});
callback('Hi 👋');
assert.equal(updates[0][4].output_chars, 4);
assert.equal(updates[0][4].first_output_ms, 100);
now = 1050;
callback('Hi 👋 there');
assert.equal(updates.length, 1);
now = 1300;
callback('Hi 👋 there.');
assert.equal(updates[1][4].output_chars, 11);
assert.equal(updates[1][4].stream_updates, 3);
assert.equal(updates[1][4].elapsed_ms, 400);
chatId = 'different conversation';
now = 1600;
callback('Unrelated text');
assert.equal(updates.length, 2);
chatId = active.file;
active.generating = false;
callback('Late callback');
assert.equal(updates.length, 2);

const transport = source.slice(source.indexOf('function telemetry('), source.indexOf('function phaseStarted('));
const sent = [];
const context = vm.createContext({
    Promise, telemetryTail: Promise.resolve(), telemetryPending: 0,
    api: async (_, data) => { sent.push(data); if (data.status === 'start') throw Error('bridge temporarily unavailable'); },
});
vm.runInContext(transport, context);
(async () => {
    context.telemetry({ job_id: 'job' }, 'inference', 'start', 'Generating');
    context.telemetry({ job_id: 'job' }, 'inference', 'complete', 'Finished', { output_chars: 11 });
    await context.telemetryTail;
    assert.deepEqual(sent.map(item => item.status), ['start', 'complete']);
    assert.equal(context.telemetryPending, 0);
    const requests = [];
    const processed = [];
    const jobs = ['job', 'next'];
    const worker = vm.createContext({
        running: true, busy: false, active: null, error: '', jobError: '', releaseWorker: null,
        channel: { postMessage() {} }, render() {}, telemetry() {}, console: { error() {} },
        AbortController,
        setTimeout: () => 1, clearTimeout() {}, setInterval: () => 2, clearInterval() {},
        process: async job => {
            processed.push(job.job_id);
            if (job.job_id === 'job') throw Error('Generation exceeded 60 seconds.');
        },
        api: async (path, body) => {
            requests.push({ path, body });
            return { job_id: path.startsWith('/v1/next') ? jobs.shift() : null, claim_token: 'claim' };
        },
    });
    vm.runInContext(source.slice(source.indexOf('async function tick()'), source.indexOf('function start()')), worker);
    await worker.tick();
    assert.equal(requests.at(-1).path, '/v1/jobs/job/fail');
    assert.equal(requests.at(-1).body.claim_token, 'claim');
    assert.equal(requests.at(-1).body.error, 'Generation exceeded 60 seconds.');
    assert.equal(worker.running, true);
    assert.equal(worker.busy, false);
    assert.equal(worker.error, '');
    assert.equal(worker.jobError, 'Generation exceeded 60 seconds.');
    await worker.tick();
    assert.deepEqual(processed, ['job', 'next']);
    assert.equal(worker.jobError, '');

    const chats = vm.createContext({
        st: async (_, { file_name: file }) => file === undefined
            ? [{ file_name: 'damaged.jsonl' }, { file_name: 'healthy.jsonl' }]
            : file === 'damaged' ? [] : [{ chat_metadata: { quinnNative: { epoch: 1 } } }],
    });
    vm.runInContext(source.slice(source.indexOf('async function loadChats('), source.indexOf('async function reset(')), chats);
    const healthy = await chats.loadChats('Quinn.png', 'healthy');
    assert.equal(healthy.length, 1);
    assert.equal(healthy[0].file, 'healthy');
    await assert.rejects(chats.loadChats('Quinn.png', 'damaged'), /Unreadable mapped chat/);
    console.log('Quinn stream measurements and best-effort telemetry passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
