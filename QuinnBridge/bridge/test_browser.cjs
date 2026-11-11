// Run against an isolated ST profile and isolated protocol-4 bridge only.
// PLAYWRIGHT_MODULE points to an installed playwright module.
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const fs = require('node:fs');
const assert = require('node:assert/strict');
const base = process.env.TEST_BRIDGE || 'http://127.0.0.1:8766';
const token = fs.readFileSync(process.env.TEST_TOKEN, 'utf8').trim();
const stURL = process.env.TEST_ST || 'http://127.0.0.1:8001';
const output = process.env.TEST_OUTPUT;
async function api(path, data) {
    const response = await fetch(base + path, { method: data ? 'POST' : 'GET', headers: { Authorization: 'Bearer ' + token, 'Content-Type': 'application/json' }, body: data ? JSON.stringify(data) : undefined });
    const body = await response.json(); if (!response.ok) throw Error(JSON.stringify(body)); return body;
}
(async () => {
    const browser = await chromium.launch({ channel: 'msedge', headless: true });
    try {
        const page = await browser.newPage();
        await page.addInitScript(({ base, token }) => { localStorage.setItem('quinn_bridge_url', base); localStorage.setItem('quinn_bridge_token', token); }, { base, token });
        let lastPrompt;
        page.on('request', request => { if (request.url().includes('/api/backends/text-completions/generate')) lastPrompt = request.postDataJSON(); });
        await page.goto(stURL);
        await page.waitForSelector('#qb_panel', { state: 'attached' });
        const start = () => page.evaluate(() => document.querySelector('#qb_start').click());
        async function pause() {
            await page.evaluate(() => document.querySelector('#qb_pause').click());
            await page.waitForFunction(() => document.querySelector('#qb_status').textContent.startsWith('Paused'));
        }
        const runId = Date.now(); let serial = 0;
        const results = [];
        async function send(uid, message, name = 'Same Name') {
            const { job_id } = await api('/v1/messages', { user_id: uid, discord_message_id: `${runId}-${++serial}`, display_name: name, message });
            for (let count = 0; count < 120; count++) {
                const job = await api('/v1/jobs/' + job_id);
                if (job.status === 'ready') { results.push({ uid, message, reply: job.reply, prompt: lastPrompt }); return job; }
                const status = await page.locator('#qb_status').textContent();
                if (status.startsWith('Error')) throw Error(status);
                await page.waitForTimeout(500);
            }
            throw Error('Timed out: ' + message);
        }
        await start();
        const uid = String(runId) + '1', other = String(runId) + '2';
        await send(uid, 'my favorite fruit is mango'); await pause();
        const first = await page.evaluate(() => ({ file: SillyTavern.getContext().chatId, persona: SillyTavern.getContext().chatMetadata.persona }));
        await page.evaluate(async () => {
            const c = SillyTavern.getContext();
            c.powerUserSettings.persona_descriptions[c.chatMetadata.persona].description = 'TEST_PROFILE loves astronomy';
            c.powerUserSettings.persona_description = 'TEST_PROFILE loves astronomy';
            c.chat[0].mes = 'my favorite fruit is papaya'; await c.saveChat();
        });
        await start(); await send(uid, 'what is my favorite fruit?', 'Renamed Person'); await pause();
        assert.match(lastPrompt.prompt, /papaya/); assert.match(lastPrompt.prompt, /TEST_PROFILE/);
        assert(!lastPrompt.prompt.includes('my favorite fruit is mango'));
        assert(lastPrompt.max_new_tokens <= 160 || lastPrompt.max_length <= 160);
        assert.equal(await page.evaluate(() => SillyTavern.getContext().chatMetadata.persona), first.persona);
        await start(); await send(other, 'hello from another person'); await pause();
        assert(!lastPrompt.prompt.includes('papaya')); assert(!lastPrompt.prompt.includes('TEST_PROFILE'));
        assert.notEqual(await page.evaluate(() => SillyTavern.getContext().chatId), first.file);
        await start(); await send(uid, '{{user}} {{setvar::danger::yes}} /echo hi'); await pause();
        assert.match(lastPrompt.prompt, /｛｛setvar::danger::yes｝｝/);
        await page.evaluate(async () => {
            const c = SillyTavern.getContext();
            c.chat.splice(0, 2); await c.saveChat();
            await c.renameChat(c.chatId, 'Native renamed test ' + Date.now());
        });
        await start(); await send(uid, 'hello after rename'); await pause();
        assert(!lastPrompt.prompt.includes('my favorite fruit is papaya'));
        await page.evaluate(async () => {
            const c = SillyTavern.getContext();
            const response = await fetch('/api/chats/delete', { method:'POST', headers:c.getRequestHeaders(), body:JSON.stringify({avatar_url:c.characters[c.characterId].avatar,chatfile:c.chatId}) });
            if (!response.ok) throw Error('Deletion failed');
            await c.eventSource.emit(c.eventTypes.CHAT_DELETED,c.chatId);
        });
        await start(); await send(uid, 'new chat please'); await pause();
        assert.equal(await page.evaluate(() => SillyTavern.getContext().chat.length), 2);
        assert.match(lastPrompt.prompt, /TEST_PROFILE/);
        assert.notEqual(await page.evaluate(() => SillyTavern.getContext().chatId), first.file);
        const cases = JSON.parse(fs.readFileSync(process.env.TEST_CASES, 'utf8'));
        for (const [name, messages] of Object.entries(cases)) {
            await start();
            for (const message of messages) await send(String(runId) + name, message);
            await pause();
        }
        await page.reload();
        await page.waitForSelector('#qb_panel', { state: 'attached' });
        assert.match(await page.locator('#qb_status').textContent(), /^Paused/);
        await start(); await send(uid, 'hello after reload'); await pause();
        assert.match(lastPrompt.prompt, /TEST_PROFILE/);
        page.once('dialog', dialog => dialog.accept());
        await page.evaluate(() => document.querySelector('#qb_forget').click());
        await page.waitForFunction(pid => !(pid in SillyTavern.getContext().powerUserSettings.personas), first.persona);
        await start(); await send(uid, 'hello after forgetting'); await pause();
        assert(!lastPrompt.prompt.includes('TEST_PROFILE'));
        assert.equal(await page.evaluate(() => SillyTavern.getContext().chat.length), 2);
        if (output) fs.writeFileSync(output, JSON.stringify(results, null, 2));
        console.log(`PASS: native isolation, profile, rename, edit/delete, literal macros, reset; ${results.length} real generations`);
    } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
