import { setUserAvatar } from '/scripts/personas.js';
import { saveSettings as persistSettings, setUserName, closeCurrentChat } from '/script.js';
const ctx = () => SillyTavern.getContext();
const literal = value => String(value ?? '').replace(/\{{2,}/g, s => '｛'.repeat(s.length)).replace(/\}{2,}/g, s => '｝'.repeat(s.length)).replace(/<(user|bot|char|group|charifnotgroup)>/gi, '＜$1＞');
const safe = value => String(value).replace(/[^a-zA-Z0-9 _-]/g, '_').slice(0, 60);
const settings = () => ctx().extensionSettings.quinnNative ??= { routes: {}, avatars: {} };
let running = false, busy = false, active = null, error = '', pending = 0, uncertain = 0;
let releaseWorker = null;
const channel = new BroadcastChannel('quinn-native-worker');
let remoteBusy = false;
let remoteUpdated = 0;
channel.onmessage = event => { remoteUpdated = Date.now(); remoteBusy = event.data.busy; document.body.classList.toggle('qb_remote_busy', remoteBusy); };
const config = key => localStorage.getItem('quinn_bridge_' + key) || (key === 'url' ? 'http://127.0.0.1:8765' : key === 'char' ? 'Quinn' : '');
function render() {
    const el = document.getElementById('qb_status');
    if (el) el.textContent = `${error ? 'Error: ' + error : running ? 'Running' : busy ? 'Pausing' : 'Paused'} · ${pending} pending · ${uncertain} uncertain deliveries`;
}
async function api(path, data, timeout = 20000) {
    const response = await fetch(config('url').replace(/\/+$/, '') + path, {
        method: data === undefined ? 'GET' : 'POST',
        headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + config('token') },
        body: data === undefined ? undefined : JSON.stringify(data), signal: AbortSignal.timeout(timeout),
    });
    const body = await response.json();
    if (!response.ok) throw Error(body.error || `Bridge HTTP ${response.status}`);
    return body;
}
async function st(path, data) {
    const response = await fetch('/api/' + path, { method: 'POST', headers: ctx().getRequestHeaders(), body: JSON.stringify(data) });
    if (!response.ok) throw Error(`ST ${path}: HTTP ${response.status}`);
    return response.json();
}
async function saveSettings() { await persistSettings(); }
async function loadChats(avatar) {
    const list = await st('chats/search', { avatar_url: avatar, query: '' });
    if (!Array.isArray(list)) throw Error('Invalid ST chat listing');
    const result = [];
    for (const item of list) {
        const file = item.file_name.replace(/\.jsonl$/, '');
        const rows = await st('chats/get', { avatar_url: avatar, file_name: file });
        if (!Array.isArray(rows) || !rows[0]?.chat_metadata) throw Error('Unreadable chat; refusing to reset');
        result.push({ file, meta: rows[0].chat_metadata });
    }
    return result;
}
async function reset(uid, operation = 'reset', job = null) {
    const result = await api('/v1/people/' + encodeURIComponent(uid), { operation, ...(job ? { preserve_job: job.job_id, claim_token: job.claim_token } : {}) });
    delete settings().routes[uid]; await saveSettings();
    return result;
}
async function persona(job) {
    const c = ctx(), p = c.powerUserSettings, id = `discord_${safe(job.user_id)}.png`;
    const absent = !(id in p.personas);
    p.personas[id] = literal(job.display_name);
    if (absent) p.persona_descriptions[id] = { description: '', position: 0, depth: 2, role: 0, lorebook: '', connections: [] };
    if (absent || settings().avatars[id] !== job.avatar_url) {
        try {
            const url = job.avatar_url || '/img/user-default.png';
            if (job.avatar_url && !/^https:\/\/(cdn|media)\.discordapp\.(com|net)\//.test(url)) throw Error('Unsupported avatar URL');
            const response = await fetch(url, { signal: AbortSignal.timeout(8000) });
            if (!response.ok) throw Error('Avatar unavailable');
            const form = new FormData(); form.append('avatar', await response.blob(), 'avatar.png'); form.append('overwrite_name', id);
            const headers = { ...c.getRequestHeaders() }; delete headers['Content-Type']; delete headers['content-type'];
            const uploaded = await fetch('/api/avatars/upload', { method: 'POST', headers, body: form });
            if (!uploaded.ok) throw Error('Avatar upload failed');
            settings().avatars[id] = job.avatar_url;
        } catch (e) { console.warn('[Quinn] Avatar refresh skipped', e); }
    }
    await saveSettings(); return id;
}
function guard(event) {
    if ((!running && !busy && !remoteBusy) || event.target.closest('#qb_panel')) return;
    event.preventDefault(); event.stopImmediatePropagation();
}
for (const event of ['click', 'keydown', 'beforeinput', 'paste', 'drop']) document.addEventListener(event, guard, true);
function pause() { running = false; if (!busy) releaseWorker?.(); render(); }
async function process(job) {
    const c = ctx();
    let index = c.characters.findIndex(x => x.avatar === job.route?.avatar);
    if (index < 0 && job.route?.avatar) throw Error('Mapped Quinn character is missing');
    if (index < 0) index = c.characters.findIndex(x => x.name === config('char'));
    if (index < 0) throw Error('Quinn character not found');
    const avatar = c.characters[index].avatar;
    const chats = await loadChats(avatar);
    const matches = chats.filter(x => x.meta.quinnNative?.userId === job.user_id && x.meta.quinnNative?.epoch === job.epoch);
    if (matches.length > 1) throw Error('Multiple mapped chats; resolve duplicates before starting');
    if (!matches.length && job.route?.file) {

        const result = await reset(job.user_id, 'reset', job);
        job.epoch = result.epoch; job.route = {};
    }
    let file = matches[0]?.file;
    const pid = await persona(job);
    await c.selectCharacterById(index);
    if (!file) {
        file = `Discord - ${safe(job.display_name)} - ${safe(job.user_id).slice(-6)} - ${crypto.randomUUID()}`;
        await st('chats/save', { avatar_url: avatar, file_name: file, chat: [{ user_name: literal(job.display_name), character_name: c.characters[index].name, create_date: new Date().toISOString(), chat_metadata: { persona: pid, quinnNative: { userId: job.user_id, epoch: job.epoch } } }] });
    }
    await ctx().openCharacterChat(file);
    await setUserAvatar(pid, { toastPersonaNameChange: false });
    setUserName(literal(job.display_name), { toastPersonaNameChange: false });
    let current = ctx();
    const descriptor = current.powerUserSettings.persona_descriptions[pid];
    for (const [target, source] of Object.entries({ persona_description: 'description', persona_description_position: 'position', persona_description_depth: 'depth', persona_description_role: 'role', persona_description_lorebook: 'lorebook' })) {
        current.powerUserSettings[target] = descriptor[source];
    }

    if (!matches.length) current.chat.splice(0);
    current.chatMetadata.persona = pid;
    current.chatMetadata.quinnNative = { userId: job.user_id, epoch: job.epoch };
    await current.saveMetadata();
    const route = { avatar, file, persona: pid };
    await api('/v1/people/' + encodeURIComponent(job.user_id), { epoch: job.epoch, route });
    settings().routes[job.user_id] = { ...route, epoch: job.epoch }; await saveSettings();
    active = { job, file, avatar, controller: new AbortController(), generating: false };
    const assertContext = () => {
        if (ctx().chatId !== file || ctx().characters[ctx().characterId]?.avatar !== avatar || active.controller.signal.aborted) throw Error('Chat context changed during generation');
    };
    let user = current.chat.find(x => x.extra?.quinnNative?.jobId === job.job_id && x.is_user);
    let reply = current.chat.find(x => x.extra?.quinnNative?.jobId === job.job_id && !x.is_user);
    if (reply && reply.extra.quinnNative.complete !== true) throw Error('Interrupted reply found. Pause, remove that partial reply, then restart after claim expiry.');
    if (!user) {
        user = { name: literal(job.display_name), is_user: true, is_system: false, mes: literal(job.message), send_date: new Date().toISOString(), force_avatar: current.getThumbnailUrl('persona', pid), extra: { quinnNative: { jobId: job.job_id, discordId: job.discord_message_id } } };
        current.chat.push(user); current.addOneMessage(user); await current.saveChat();
    }
    if (!reply) {
        const before = current.chat.length;
        if (document.querySelector('#send_textarea')?.value) throw Error('Clear or save the ST draft before starting the worker');
        active.generating = true;
        // SillyTavern summarization can trigger nested generation.
        const memory = current.extensionSettings.memory;
        const frozen = memory?.memoryFrozen;
        if (memory) memory.memoryFrozen = true;
        try { await current.generate('normal', { automatic_trigger: true, signal: active.controller.signal }); }
        finally { if (memory) memory.memoryFrozen = frozen; }
        active.generating = false;
        assertContext();
        const added = ctx().chat.slice(before).filter(x => !x.is_user && !x.is_system && x.mes?.trim());
        if (added.length !== 1) throw Error('Expected exactly one native assistant reply');
        reply = added[0];
        reply.mes = literal(reply.mes);
        reply.extra ??= {}; reply.extra.quinnNative = { jobId: job.job_id, discordId: job.discord_message_id, complete: true };
        await ctx().saveChat();
    }
    assertContext();
    const saved = await st('chats/get', { avatar_url: avatar, file_name: file });
    if (!Array.isArray(saved) || !saved.some(x => !x.is_user && x.extra?.quinnNative?.jobId === job.job_id && x.mes === reply.mes)) {
        throw Error('Reply was not persisted; delivery suppressed');
    }
    await api(`/v1/jobs/${job.job_id}/complete`, { claim_token: job.claim_token, reply: reply.mes, route });
}
async function tick() {
    if (!running || busy) return;
    busy = true; channel.postMessage({ busy: true }); render();
    let job, heartbeat;
    try {
        job = await api('/v1/next?worker=4');
        if (job.job_id) {
            heartbeat = setInterval(() => api(`/v1/jobs/${job.job_id}/heartbeat`, { claim_token: job.claim_token }).catch(e => { active?.controller.abort(); error = e.message; pause(); }), 15000);
            await process(job);
        }
    } catch (e) {
        error = e.message; running = false;

        console.error('[Quinn Native]', e);
    } finally {
        clearInterval(heartbeat); active = null; busy = false; channel.postMessage({ busy: running });
        if (!running) releaseWorker?.(); render();
    }
}
function start() {
    if (running) return Promise.resolve();
    if (busy || !config('token')) return Promise.reject(Error('Set the bridge token and wait until paused'));
    return new Promise((resolve, reject) => navigator.locks.request('quinn-bridge-worker', { ifAvailable: true }, async lock => {
        if (!lock) { error = 'Another worker owns this browser session'; render(); reject(Error(error)); return; }
        error = ''; running = true; channel.postMessage({ busy: true }); render();
        resolve();
        await new Promise(resolve => { releaseWorker = resolve; });
        releaseWorker = null;
    }).catch(reject));
}

async function managedLaunch() {
    const fragment = new URLSearchParams(location.hash.slice(1));
    const incoming = fragment.get('quinn-launch');
    if (incoming) {
        sessionStorage.setItem('quinn-launch', incoming);
        history.replaceState(history.state, '', location.pathname + location.search);
    }
    const session = sessionStorage.getItem('quinn-launch');
    if (!session) return;
    let lastSeen, closing = false, initialized = false;
    const report = (state, message = '') => api('/v1/launcher', { operation: 'report', session, role: 'browser', state, error: message }, 2000);
    window.addEventListener('pagehide', () => {
        fetch(config('url').replace(/\/+$/, '') + '/v1/launcher', {
            method: 'POST', keepalive: true,
            headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + config('token') },
            body: JSON.stringify({ operation: 'report', session, role: 'browser', state: closing ? 'closed' : 'left' }),
        }).catch(() => {});
    });
    const close = async () => {
        if (closing) return;
        closing = true; pause();
        while (busy) await new Promise(resolve => setTimeout(resolve, 100));
        await report('closing').catch(() => {});
        window.close();
        setTimeout(() => { report('blocked', 'Firefox blocked automatic tab closure').catch(() => {}); }, 500);
    };
    const poll = async () => {
        try {
            const state = await api('/v1/launcher?session=' + encodeURIComponent(session), undefined, 2000);
            lastSeen = Date.now();
            if (state.desired === 'close') { await close(); return; }
            if (state.desired === 'drain') pause();
            if (!initialized && state.desired === 'run') {
                initialized = true;
                if (state.autostart) {
                    if (!ctx().characters.some(c => c.name === config('char'))) throw Error('Configured Quinn character was not found');
                    if (document.querySelector('#send_textarea')?.value) throw Error('Clear or save the ST draft before starting Quinn');
                    await start();
                }
            }
            await report(error ? 'failed' : busy ? 'busy' : 'ready', error);
        } catch (e) {
            error = e.message; render();
            await report('failed', error).catch(() => {});
            if (lastSeen !== undefined && Date.now() - lastSeen > 15000) { await close(); return; }
        }
        if (!closing) setTimeout(poll, 1000);
    };
    await poll();
}
async function forget() {
    if (running || busy) throw Error('Pause the worker first');
    const uid = ctx().chatMetadata.quinnNative?.userId;
    if (!uid) throw Error('Open a managed DM chat first');
    if (!confirm('Forget this person? This deletes the managed chat and persona facts. Discord messages and migration backups remain.')) return;
    const route = settings().routes[uid];
    if (!route) throw Error('Missing conversation mapping');
    await reset(uid, 'forget');
    await closeCurrentChat();
    await st('chats/delete', { avatar_url: route.avatar, chatfile: route.file });
    const p = ctx().powerUserSettings;
    delete p.personas[route.persona]; delete p.persona_descriptions[route.persona];
    await st('avatars/delete', { avatar: route.persona }).catch(console.warn);
    await saveSettings();
}
function init() {
    const c = ctx();
    for (const key of ['generate','openCharacterChat','saveChat','saveMetadata','selectCharacterById','powerUserSettings']) if (!c[key]) { error = 'Unsupported ST: missing ' + key; return; }
    const host = document.getElementById('extensions_settings2') || document.getElementById('extensions_settings');
    const panel = document.createElement('div'); panel.id = 'qb_panel';
    panel.innerHTML = '<h4>Quinn Native DMs</h4><label>Bridge URL<input id="qb_url" class="text_pole"></label><label>Token<input id="qb_token" class="text_pole" type="password"></label><label>Quinn character<input id="qb_char" class="text_pole"></label><button id="qb_save" class="menu_button">Save</button><button id="qb_start" class="menu_button">Start</button><button id="qb_pause" class="menu_button">Pause to manage</button><button id="qb_forget" class="menu_button">Forget person in this chat</button><div id="qb_status"></div><p>DMs only. Pause before managing chats. Manual ST replies stay local.</p>';
    host.append(panel);
    for (const key of ['url','token','char']) document.getElementById('qb_' + key).value = config(key);
    document.getElementById('qb_save').onclick = () => { if (running || busy) return; for (const key of ['url','token','char']) localStorage.setItem('quinn_bridge_' + key, document.getElementById('qb_' + key).value.trim()); };
    document.getElementById('qb_start').onclick = () => start().catch(e => { error = e.message; render(); });
    document.getElementById('qb_pause').onclick = pause;
    document.getElementById('qb_forget').onclick = () => forget().catch(e => { error = e.message; render(); });
    const events = c.eventTypes;
    c.eventSource.on(events.GENERATION_STARTED, () => {
        if (!active) return;
        if (!active.generating || active.started) { active.controller.abort(); throw Error('Competing generation blocked'); }
        active.started = true;
    });
    c.eventSource.on(events.MESSAGE_RECEIVED, index => {
        if (!active?.generating || ctx().chatId !== active.file) return;
        const message = ctx().chat[index];
        if (message && !message.is_user) {
            message.extra ??= {};
            message.extra.quinnNative = { jobId: active.job.job_id, discordId: active.job.discord_message_id, complete: false };
        }
    });
    c.eventSource.on(events.CHAT_CHANGED, () => { if (active && ctx().chatId !== active.file) { active.controller.abort(); pause(); } });
    c.eventSource.on(events.CHAT_DELETED, async name => {
        for (const [uid, route] of Object.entries(settings().routes)) if (route.file === String(name).replace(/\.jsonl$/, '')) {
            active?.controller.abort(); pause();
            try { await reset(uid); } catch (e) { error = e.message; render(); }
        }
    });
    c.eventSource.on(events.CHAT_RENAMED, async event => {
        for (const [uid, route] of Object.entries(settings().routes)) {
            if (route.avatar === event.avatarId && route.file === event.oldFileName.replace(/\.jsonl$/, '')) {
                route.file = event.newFileName.replace(/\.jsonl$/, '');
                try { await api('/v1/people/' + encodeURIComponent(uid), { epoch: route.epoch, route }); await saveSettings(); }
                catch (e) { error = e.message; pause(); }
            }
        }
    });
    const cap = data => { if (active?.generating) for (const key of ['max_length','max_new_tokens','max_tokens','max_completion_tokens']) if (key in data) data[key] = Math.min(Number(data[key]) || 160, 160); };
    c.eventSource.on(events.GENERATE_AFTER_DATA, cap);
    c.eventSource.on(events.CHAT_COMPLETION_SETTINGS_READY, cap);
    setInterval(() => {
        if (remoteBusy && Date.now() - remoteUpdated > 5000) { remoteBusy = false; document.body.classList.remove('qb_remote_busy'); }
        if (running || busy) channel.postMessage({ busy: true });
        api('/v1/status').then(s => { pending = s.pending; uncertain = s.uncertain; render(); }).catch(() => {}); tick();
    }, 1400);
    render();
    managedLaunch().catch(e => { error = e.message; render(); });
}
ctx().eventSource.once(ctx().eventTypes.APP_READY, init);
