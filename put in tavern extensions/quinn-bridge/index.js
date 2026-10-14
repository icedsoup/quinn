/**
 * quinn-bridge/index.js — Quinn Bridge SillyTavern Extension  v3
 *
 * What changed from v2 → v3:
 *   • Token stored in localStorage (survives tab refreshes) — this was the
 *     silent killer: sessionStorage was wiped every refresh, so polling
 *     never auto-started and all bridge jobs just sat in the queue forever.
 *   • Polling auto-starts on page load when a saved token + character exist.
 *   • Per-user ST chat files: every Discord user gets their own .jsonl chat
 *     under Quinn's character in ST. Conversations appear in ST's UI.
 *   • User registry persisted in localStorage so chat mappings survive
 *     ST restarts.
 *   • New-user detection: the system prompt tells Quinn this is a first
 *     conversation so she naturally introduces herself.
 *
 * Install: copy this folder to SillyTavern/public/extensions/quinn-bridge/
 * You also need a manifest.json in the same folder (see repo).
 *
 * ST API endpoints used (same-origin fetch, no CORS needed):
 *   POST /api/chats/save   – create / update a chat file
 *   POST /api/chats/get    – load a chat file
 *
 * If your ST version uses the older /savechat and /getchat routes
 * the extension falls back to those automatically.
 */

const EXT_ID   = "quinn-bridge";
const POLL_MS  = 1400;
const DEF_BRIDGE = "http://127.0.0.1:8765";

// ─── persistent settings (localStorage) ──────────────────────────────────────
// We use localStorage so settings survive tab refreshes AND ST restarts.
// sessionStorage was the silent killer: token was wiped on every refresh,
// polling never auto-started, jobs queued forever.

const LS = {
    get token()   { return localStorage.getItem("quinn_bridge_token")  || ""; },
    set token(v)  { v ? localStorage.setItem("quinn_bridge_token", v)
                      : localStorage.removeItem("quinn_bridge_token"); },

    get bridge()  { return localStorage.getItem("quinn_bridge_url") || DEF_BRIDGE; },
    set bridge(v) { localStorage.setItem("quinn_bridge_url", v); },

    get charName() { return localStorage.getItem("quinn_bridge_char") || ""; },
    set charName(v){ localStorage.setItem("quinn_bridge_char", v); },

    // Per-user chat registry: { discord_user_id: { chatName, displayName, since } }
    get users() {
        try { return JSON.parse(localStorage.getItem("quinn_user_registry") || "{}"); }
        catch { return {}; }
    },
    set users(v) { localStorage.setItem("quinn_user_registry", JSON.stringify(v)); },

    registerUser(userId, chatName, displayName) {
        const reg = this.users;
        reg[userId] = { chatName, displayName, since: Date.now() };
        this.users = reg;
    },
    lookupUser(userId) { return this.users[userId] ?? null; },
    clearUsers() { localStorage.removeItem("quinn_user_registry"); },
};

// ─── state ────────────────────────────────────────────────────────────────────

let pollTimer  = null;
let enabled    = false;
let busy       = false;

// ─── logging / status ─────────────────────────────────────────────────────────

const log = (...a) => console.log("[Quinn Bridge]", ...a);
const err = (...a) => console.error("[Quinn Bridge]", ...a);

function status(text, isError = false) {
    const el = document.getElementById("qb_status");
    if (!el) return;
    el.textContent = text;
    el.style.color = isError ? "#e06c75" : "#aaa";
}

// ─── SillyTavern context ──────────────────────────────────────────────────────

function getSTContext() {
    const ctx = window.SillyTavern?.getContext?.();
    if (!ctx) throw new Error("SillyTavern context not ready.");
    return ctx;
}

/**
 * Find Quinn's character card by the configured name.
 * Falls back to the currently selected character.
 */
function getCharacter() {
    const ctx = getSTContext();
    const preferred = LS.charName;

    // Try by saved name first
    if (preferred) {
        const found = ctx.characters?.find(c => c.name === preferred);
        if (found) return found;
    }

    // Fall back to whatever is currently selected
    const id = ctx.characterId;
    if (id === undefined || id === null || id < 0)
        throw new Error("No character selected in SillyTavern. Select Quinn first.");
    const c = ctx.characters?.[id];
    if (!c) throw new Error("Could not read selected character.");
    return c;
}

// Character folder name for chat files: strip extension from avatar filename
function charFolder(character) {
    return (character.avatar || character.name || "Quinn").replace(/\.[^.]+$/, "");
}

// ─── bridge HTTP helpers ──────────────────────────────────────────────────────

async function bridgeFetch(path, opts = {}) {
    const headers = new Headers({ "Content-Type": "application/json" });
    if (LS.token) headers.set("Authorization", `Bearer ${LS.token}`);

    const r = await fetch(`${LS.bridge}${path}`, {
        ...opts,
        headers,
        cache: "no-store",
    });

    if (!r.ok) {
        const detail = await r.text().catch(() => "");
        throw new Error(`Bridge ${path} → HTTP ${r.status}${detail ? ": " + detail : ""}`);
    }
    return r.status === 204 ? null : r.json();
}

// ─── ST internal API helpers ──────────────────────────────────────────────────
// ST's own internal routes (same origin, port 8000).
// Newer ST (1.12+) uses /api/chats/save and /api/chats/get.
// Older ST uses /savechat and /getchat.  We try both.

function stHeaders() {
    // ST may require a CSRF token; getRequestHeaders is available globally in ST.
    if (typeof window.getRequestHeaders === "function") {
        return window.getRequestHeaders();
    }
    return { "Content-Type": "application/json" };
}

async function stPost(path, body) {
    const r = await fetch(path, {
        method: "POST",
        headers: stHeaders(),
        body: JSON.stringify(body),
    });
    return r; // caller checks .ok
}

/**
 * Load a chat from ST's filesystem.
 * Returns the message array (excludes the header object).
 * Returns [] if the chat doesn't exist yet or can't be read.
 */
async function loadChat(folder, chatName) {
    // Try newer API first
    let r = await stPost("/api/chats/get", {
        ch_name: folder,
        file_name: chatName,
    }).catch(() => null);

    // Fall back to older route
    if (!r || !r.ok) {
        r = await stPost("/getchat", {
            ch_name: folder,
            file_name: chatName,
        }).catch(() => null);
    }

    if (!r || !r.ok) return [];  // chat doesn't exist yet, or API not available

    let data;
    try { data = await r.json(); } catch { return []; }

    // ST returns either an array directly or { chat: [...] }
    const arr = Array.isArray(data) ? data : (data?.chat ?? []);
    // First element is the metadata header — drop it
    return arr.slice(1);
}

/**
 * Save a chat to ST's filesystem.
 * `messages` is the array of message objects (no header).
 * We prepend the required ST header object here.
 */
async function saveChat(charName, folder, chatName, messages, userDisplayName) {
    const header = {
        user_name        : userDisplayName || "Discord User",
        character_name   : charName,
        create_date      : new Date().toLocaleString("en-US"),
        chat_metadata    : {
            note_prompt     : "",
            note_interval   : 1,
            note_depth      : 100,
            note_position   : 1,
            summarize       : false,
            summarize_template: "",
        },
        this_chat_is_this_char_version: 0,
    };

    const payload = [header, ...messages];

    // Try newer API
    let r = await stPost("/api/chats/save", {
        ch_name  : folder,      // directory name
        file_name: chatName,
        chat     : payload,
        is_kawaii: 0,
    }).catch(() => null);

    // Fall back to older /savechat
    if (!r || !r.ok) {
        r = await stPost("/savechat", {
            ch_name  : charName,   // older route uses charName here
            file_name: chatName,
            chat     : payload,
            is_kawaii: 0,
        }).catch(() => null);
    }

    if (!r || !r.ok) {
        err("saveChat failed for", chatName, "— chat won't appear in ST UI");
        // Non-fatal: generation still proceeds, history just won't persist to ST
    }
}

/**
 * Ensure a chat file exists for this Discord user.
 * Creates an empty one if it doesn't exist yet.
 * Returns the chat filename (no .jsonl extension).
 */
async function ensureUserChat(charName, folder, userId, displayName) {
    const existing = LS.lookupUser(userId);
    if (existing) return existing.chatName;

    // New user — mint a chat file name
    const safe = displayName.replace(/[^a-z0-9_\-]/gi, "_").slice(0, 40);
    const chatName = `discord_${userId}_${safe}`;

    // Create by saving an empty chat
    await saveChat(charName, folder, chatName, [], displayName);
    LS.registerUser(userId, chatName, displayName);
    log(`New user ${displayName} (${userId}) → chat "${chatName}"`);
    return chatName;
}

// ─── prompt building ──────────────────────────────────────────────────────────

/**
 * Build the system prompt from Quinn's character card + Discord context.
 * ST's generateRaw uses this as the "system" role or pre-prompt.
 */
function buildSystemPrompt(character, displayName, userId, isNewUser, historyLen) {
    const name = character.name || "Quinn";

    const sections = [
        character.system_prompt   || "",
        character.description     || "",
        character.personality ? `[Personality]\n${character.personality}` : "",
        character.scenario    ? `[Scenario]\n${character.scenario}`       : "",
        character.creator_notes   || "",
    ].filter(Boolean).join("\n\n");

    const discordCtx = [
        `[Discord context]`,
        `You are ${name}. You are currently in a private Discord conversation with ${displayName} (Discord user ID: ${userId}).`,
        isNewUser
            ? `This is the very first time ${displayName} has contacted you. You have never spoken before.`
            : `You have had ${historyLen} previous exchanges with ${displayName}.`,
        `Do not break character. Do not mention that you are an AI unless it is in character to do so.`,
        `Do not prefix your reply with your name (e.g. "Quinn:" — the sending system handles that).`,
    ].join("\n");

    return [sections, discordCtx].filter(Boolean).join("\n\n");
}

/**
 * Build the full prompt for generateRaw (string-based, works with KoboldCpp).
 * Format: system → example dialogue → history → [new message] → [reply cue].
 */
function buildPrompt(character, history, userDisplayName, userMessage, systemPrompt) {
    const charName = character.name || "Quinn";
    const lines    = [systemPrompt];

    // Example dialogue — keep as-is since it's already formatted
    if (character.mes_example?.trim()) {
        lines.push("[Example dialogue]\n" + character.mes_example.trim());
    }

    // Character greeting shown only in brand-new conversations (no history)
    if (history.length === 0 && character.first_mes?.trim()) {
        lines.push(`[${charName} opens the conversation]\n${charName}: ${character.first_mes.trim()}`);
    }

    // Prior messages from the ST chat file
    if (history.length > 0) {
        lines.push("[Conversation history]");
        for (const m of history) {
            const speaker = m.is_user ? (m.name || userDisplayName) : charName;
            lines.push(`${speaker}: ${m.mes}`);
        }
    }

    // The new Discord message and the reply cue
    lines.push(`[New message from ${userDisplayName}]`);
    lines.push(`${userDisplayName}: ${userMessage}`);
    lines.push(`${charName}:`);

    return lines.join("\n\n");
}

// ─── generation ───────────────────────────────────────────────────────────────

async function generate(systemPrompt, prompt) {
    // Dynamic-import ST's generateRaw from its script.js (same origin)
    let generateRaw;
    try {
        const mod = await import("/script.js");
        generateRaw = mod.generateRaw;
    } catch {
        // Older ST: generateRaw may be a global
        generateRaw = window.generateRaw;
    }

    if (typeof generateRaw !== "function")
        throw new Error("generateRaw not available — is Quinn selected in ST?");

    // Try object form first (newer ST extension API)
    let reply;
    try {
        reply = await generateRaw({ systemPrompt, prompt });
    } catch {
        // Fall back to positional string form
        reply = await generateRaw(systemPrompt + "\n\n" + prompt);
    }

    return String(reply ?? "").trim();
}

// ─── main job handler ─────────────────────────────────────────────────────────

async function handleJob(job) {
    const character   = getCharacter();
    const charName    = character.name || "Quinn";
    const folder      = charFolder(character);
    const userId      = String(job.user_id);
    const displayName = job.display_name || "Discord User";
    const userMessage = job.message;

    // ── 1. Ensure this user has a chat file in ST ──────────────────────────
    const chatName = await ensureUserChat(charName, folder, userId, displayName);
    const isNewUser = !LS.lookupUser(userId); // was just created if this is true

    // ── 2. Load existing history from ST chat file ─────────────────────────
    const history = await loadChat(folder, chatName);

    // ── 3. Build prompt + generate ─────────────────────────────────────────
    const systemPrompt = buildSystemPrompt(
        character, displayName, userId,
        isNewUser || history.length === 0,
        history.length
    );
    const prompt = buildPrompt(character, history, displayName, userMessage, systemPrompt);

    const reply = await generate(systemPrompt, prompt);
    if (!reply) throw new Error("generateRaw returned an empty reply.");

    // ── 4. Append both messages to the ST chat file ────────────────────────
    const now = new Date().toLocaleString("en-US");
    const updated = [
        ...history,
        { name: displayName, is_user: true,  mes: userMessage, send_date: now,
          swipe_id: 0, swipes: [userMessage], extra: { api: "discord" } },
        { name: charName,    is_user: false, mes: reply,       send_date: now,
          swipe_id: 0, swipes: [reply] },
    ];
    await saveChat(charName, folder, chatName, updated, displayName);

    log(`✓ Replied to ${displayName} — job ${job.job_id} (${reply.length} chars)`);
    return reply;
}

// ─── poll loop ────────────────────────────────────────────────────────────────

async function tick() {
    if (!enabled || busy) return;

    let job;
    try {
        job = await bridgeFetch("/v1/next");
    } catch (e) {
        status(`Bridge unreachable: ${e.message}`, true);
        return;
    }

    if (!job?.job_id) {
        // No job waiting — just stay connected
        status("Connected · waiting for a message…");
        return;
    }

    busy = true;
    const { job_id, display_name } = job;
    status(`Generating reply for ${display_name}…`);

    try {
        const reply = await handleJob(job);
        await bridgeFetch(`/v1/jobs/${encodeURIComponent(job_id)}/complete`, {
            method: "POST",
            body: JSON.stringify({ reply }),
        });
        status(`✓ Replied to ${display_name}`);
    } catch (e) {
        err("Job failed:", e);
        status(`Error: ${e.message}`, true);
        try {
            await bridgeFetch(`/v1/jobs/${encodeURIComponent(job_id)}/fail`, {
                method: "POST",
                body: JSON.stringify({ error: String(e.message) }),
            });
        } catch { /* best-effort */ }
    } finally {
        busy = false;
    }
}

function startPolling() {
    stopPolling();
    enabled = true;
    status("Starting…");
    void tick();  // immediate first tick
    pollTimer = setInterval(tick, POLL_MS);
    log("Polling bridge at", LS.bridge);
}

function stopPolling() {
    enabled = false;
    clearInterval(pollTimer);
    pollTimer = null;
}

// ─── settings panel UI ────────────────────────────────────────────────────────

function renderSettings() {
    // Only render once
    if (document.getElementById("qb_panel")) return;

    const host = document.getElementById("extensions_settings2")
               || document.getElementById("extensions_settings");
    if (!host) return;

    const panel = document.createElement("div");
    panel.id = "qb_panel";

    panel.innerHTML = `
<div class="inline-drawer">
  <div class="inline-drawer-toggle inline-drawer-header">
    <b>Quinn Bridge</b>
    <div class="inline-drawer-icon fa-solid fa-circle-chevron-down"></div>
  </div>
  <div class="inline-drawer-content" style="padding:8px 12px">

    <label style="display:block;margin-bottom:4px">Bridge URL</label>
    <input id="qb_url" class="text_pole" type="text"
           value="${escHtml(LS.bridge)}" style="width:100%;margin-bottom:8px">

    <label style="display:block;margin-bottom:4px">Bridge Token</label>
    <input id="qb_token" class="text_pole" type="password" autocomplete="off"
           placeholder="Paste token from launcher"
           value="${escHtml(LS.token)}" style="width:100%;margin-bottom:8px">

    <label style="display:block;margin-bottom:4px">Quinn character name in ST</label>
    <input id="qb_char" class="text_pole" type="text"
           placeholder="Leave blank to use currently selected character"
           value="${escHtml(LS.charName)}" style="width:100%;margin-bottom:8px">

    <div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:8px">
      <button id="qb_save"  class="menu_button">Save</button>
      <button id="qb_test"  class="menu_button">Test bridge</button>
      <button id="qb_start" class="menu_button" style="color:#98c379">▶ Start</button>
      <button id="qb_stop"  class="menu_button" style="color:#e06c75">■ Stop</button>
    </div>

    <div id="qb_status" style="font-size:0.82em;color:#aaa;margin-bottom:8px">
      Not started
    </div>

    <details style="font-size:0.82em">
      <summary id="qb_reg_summary" style="cursor:pointer;color:#aaa">
        User registry (${Object.keys(LS.users).length} users)
      </summary>
      <pre id="qb_reg_body" style="font-size:0.78em;max-height:120px;
           overflow:auto;background:#1a1a2e;padding:6px;border-radius:4px;
           margin:4px 0">${escHtml(JSON.stringify(LS.users, null, 2))}</pre>
      <button id="qb_clear_users" class="menu_button" style="font-size:0.8em">
        Clear registry
      </button>
    </details>

    <p style="font-size:0.78em;color:#666;margin-top:8px">
      Keep this ST tab open. Quinn Discord must also be running.
    </p>
  </div>
</div>`;

    host.appendChild(panel);

    // ── wire up controls ──
    panel.querySelector("#qb_save").addEventListener("click", () => {
        LS.bridge   = panel.querySelector("#qb_url").value.trim() || DEF_BRIDGE;
        LS.token    = panel.querySelector("#qb_token").value.trim();
        LS.charName = panel.querySelector("#qb_char").value.trim();
        status("Saved.");
    });

    panel.querySelector("#qb_test").addEventListener("click", async () => {
        status("Testing…");
        try {
            const r = await fetch(`${LS.bridge}/health`, { cache: "no-store" });
            const d = await r.json().catch(() => ({}));
            status(r.ok ? `✓ Bridge reachable — ${d.service || "ok"}` : `✗ HTTP ${r.status}`, !r.ok);
        } catch (e) {
            status(`✗ ${e.message}`, true);
        }
    });

    panel.querySelector("#qb_start").addEventListener("click", startPolling);
    panel.querySelector("#qb_stop").addEventListener("click", () => {
        stopPolling();
        status("Paused.");
    });

    panel.querySelector("#qb_clear_users").addEventListener("click", () => {
        LS.clearUsers();
        refreshRegistry(panel);
        status("User registry cleared.");
    });
}

function refreshRegistry(panel) {
    const users = LS.users;
    const count = Object.keys(users).length;
    const summary = panel?.querySelector("#qb_reg_summary");
    const body    = panel?.querySelector("#qb_reg_body");
    if (summary) summary.textContent = `User registry (${count} users)`;
    if (body)    body.textContent    = JSON.stringify(users, null, 2);
}

function escHtml(s) {
    return String(s)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;");
}

// ─── entry point ─────────────────────────────────────────────────────────────

function onReady() {
    renderSettings();

    // Re-render if ST lazy-loads the extensions panel
    new MutationObserver(renderSettings).observe(document.body, {
        childList: true, subtree: true,
    });

    // ── Auto-start: if we have a saved token, start polling immediately ──
    // This is the key fix: no more "click Start every time ST opens"
    if (LS.token) {
        log("Found saved token — auto-starting polling.");
        setTimeout(startPolling, 800);  // small delay so ST is fully loaded
    } else {
        log("No saved token. Open Extensions → Quinn Bridge, paste token, click Save + Start.");
    }
}

if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", onReady, { once: true });
} else {
    onReady();
}