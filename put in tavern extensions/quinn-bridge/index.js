
const EXT_ID = "quinn-bridge";
const BRIDGE_BASE = "http://127.0.0.1:8765";
const POLL_MS = 1200;

let pollTimer = null;
let enabled = false;
let busy = false;
let bridgeToken = sessionStorage.getItem("quinn_bridge_token") || "";
let stModulesPromise = null;

function log(...args) {
    console.log("[Quinn Bridge]", ...args);
}

function status(text) {
    const node = document.getElementById("quinn_bridge_status");
    if (node) node.textContent = text;
}

async function getSTModules() {
    if (!stModulesPromise) {
        stModulesPromise = import("/script.js").then((script) => ({
            generateRaw: script.generateRaw,
        }));
    }

    return stModulesPromise;
}

function getSelectedCharacter() {
    const ctx = window.SillyTavern?.getContext?.();

    if (!ctx) {
        throw new Error("SillyTavern context is not available yet.");
    }

    const id = ctx.characterId;

    if (id === undefined || id === null || id < 0) {
        throw new Error("Select Quinn's character card in SillyTavern first.");
    }

    const character = ctx.characters?.[id];

    if (!character) {
        throw new Error("Couldn't read the selected character card.");
    }

    return character;
}

function cardToSystemPrompt(character) {
    const name = character.name || "Quinn";

    return [
        `You are ${name}, responding in a private conversation outside the normal SillyTavern chat.`,
        character.description
            ? `Character description:\n${character.description}`
            : "",
        character.personality
            ? `Personality:\n${character.personality}`
            : "",
        character.scenario
            ? `Scenario:\n${character.scenario}`
            : "",
        character.mes_example
            ? `Example dialogue:\n${character.mes_example}`
            : "",
        character.creator_notes
            ? `Creator notes:\n${character.creator_notes}`
            : "",
        "Stay in character. Respond naturally to the latest message. Do not claim to have sent a Discord message; the calling application handles delivery.",
        "The supplied conversation transcript is the complete conversation history for this external contact. Do not assume access to any other chat or contact's history.",
    ].filter(Boolean).join("\n\n");
}

async function bridgeFetch(path, options = {}) {
    const headers = new Headers(options.headers || {});
    headers.set("Content-Type", "application/json");

    if (bridgeToken) {
        headers.set("Authorization", `Bearer ${bridgeToken}`);
    }

    const response = await fetch(`${BRIDGE_BASE}${path}`, {
        ...options,
        headers,
        cache: "no-store",
    });

    if (!response.ok) {
        const detail = await response.text().catch(() => "");
        throw new Error(
            `Bridge returned HTTP ${response.status}${detail ? `: ${detail}` : ""}`
        );
    }

    return response.status === 204 ? null : response.json();
}

async function processOneJob() {
    if (!enabled || busy) return;

    busy = true;
    let activeJob = null;

    try {
        const job = await bridgeFetch("/v1/next", {
            method: "GET",
        });

        if (!job || !job.job_id) {
            status("Connected · waiting for a message");
            return;
        }

        activeJob = job;

        const contactName = job.display_name || "Contact";
        status(`Generating reply for ${contactName}…`);

        const character = getSelectedCharacter();
        const characterName = character.name || "Quinn";

        const transcript = (job.history || [])
            .map((turn) => {
                const speaker =
                    turn.role === "assistant"
                        ? characterName
                        : contactName;

                return `${speaker}: ${turn.content}`;
            })
            .join("\n\n");

        const prompt = [
            "Reply to the newest message in this separate conversation.",
            "Use the transcript for continuity. Do not include a speaker label unless it naturally belongs in the reply.",
            transcript
                ? `Conversation so far:\n${transcript}`
                : "This is the beginning of this conversation.",
            `Newest message from ${contactName}:\n${job.message}`,
            `${characterName}'s reply:`,
        ].join("\n\n");

        const { generateRaw } = await getSTModules();

        if (typeof generateRaw !== "function") {
            throw new Error("SillyTavern's generateRaw function is unavailable.");
        }

        const response = await generateRaw({
            systemPrompt: cardToSystemPrompt(character),
            prompt,
        });

        const reply = String(response ?? "").trim();

        if (!reply) {
            throw new Error("SillyTavern returned an empty reply.");
        }

        await bridgeFetch(
            `/v1/jobs/${encodeURIComponent(job.job_id)}/complete`,
            {
                method: "POST",
                body: JSON.stringify({ reply }),
            }
        );

        activeJob = null;
        status(`Reply generated for ${contactName}`);
    } catch (error) {
        const message = String(error?.message || error);
        status(`Error: ${message}`);
        log(error);

        // Tell the backend that the claimed job failed. If completion
        // already succeeded but its response was lost, the backend
        // rejects this update because the job is no longer processing.
        if (activeJob?.job_id) {
            try {
                await bridgeFetch(
                    `/v1/jobs/${encodeURIComponent(activeJob.job_id)}/fail`,
                    {
                        method: "POST",
                        body: JSON.stringify({ error: message }),
                    }
                );

                log("Reported failed job:", activeJob.job_id);
            } catch (reportError) {
                log(
                    "Could not report job failure; stale-job recovery remains available:",
                    reportError
                );
            }
        }
    } finally {
        busy = false;
    }
}

async function testConnection() {
    try {
        const result = await bridgeFetch("/health", {
            method: "GET",
        });

        status(
            result.ok
                ? "Connected to local bridge"
                : "Bridge responded unexpectedly"
        );
    } catch (error) {
        status(`Can't reach local bridge: ${error.message || error}`);
    }
}

function startPolling() {
    if (pollTimer) {
        clearInterval(pollTimer);
    }

    enabled = true;
    status("Connecting…");

    void testConnection();
    pollTimer = setInterval(processOneJob, POLL_MS);
}

function stopPolling() {
    enabled = false;

    if (pollTimer) {
        clearInterval(pollTimer);
    }

    pollTimer = null;
    status("Paused");
}

function renderSettings() {
    const host = document.getElementById("extensions_settings2");

    if (!host || document.getElementById("quinn_bridge_panel")) {
        return;
    }

    const panel = document.createElement("div");
    panel.id = "quinn_bridge_panel";
    panel.className = "quinn-bridge-panel";

    panel.innerHTML = `
      <div class="inline-drawer">
        <div class="inline-drawer-toggle inline-drawer-header">
          <b>Quinn Bridge (local testing)</b>
          <div class="inline-drawer-icon fa-solid fa-circle-chevron-down"></div>
        </div>
        <div class="inline-drawer-content">
          <label for="quinn_bridge_token">Local bridge token</label>
          <input id="quinn_bridge_token" class="text_pole" type="password" autocomplete="off"
                 placeholder="Paste token from bridge configuration" />
          <div class="quinn-bridge-buttons">
            <button id="quinn_bridge_save" class="menu_button">Save token</button>
            <button id="quinn_bridge_test" class="menu_button">Test connection</button>
            <button id="quinn_bridge_start" class="menu_button">Start processing</button>
            <button id="quinn_bridge_stop" class="menu_button">Pause</button>
          </div>
          <small id="quinn_bridge_status">Not connected</small>
          <p class="quinn-bridge-note">Keep SillyTavern open and Quinn selected. This extension polls your local bridge; it does not log in to Discord.</p>
        </div>
      </div>`;

    host.appendChild(panel);

    const tokenInput = panel.querySelector("#quinn_bridge_token");
    tokenInput.value = bridgeToken;

    panel.querySelector("#quinn_bridge_save").addEventListener("click", () => {
        bridgeToken = tokenInput.value.trim();

        if (bridgeToken) {
            sessionStorage.setItem("quinn_bridge_token", bridgeToken);
        } else {
            sessionStorage.removeItem("quinn_bridge_token");
        }

        status("Token saved for this tab. Test the connection.");
    });

    panel.querySelector("#quinn_bridge_test").addEventListener(
        "click",
        testConnection
    );

    panel.querySelector("#quinn_bridge_start").addEventListener(
        "click",
        startPolling
    );

    panel.querySelector("#quinn_bridge_stop").addEventListener(
        "click",
        stopPolling
    );
}

function onReady() {
    renderSettings();

    const observer = new MutationObserver(renderSettings);
    observer.observe(document.body, {
        childList: true,
        subtree: true,
    });

    log("Loaded. No Discord connection is configured.");
}

if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", onReady, {
        once: true,
    });
} else {
    onReady();
}
