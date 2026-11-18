# Quinn and Sniper

Quinn replies to Discord DMs using a local model. SillyTavern stores a separate chat and persona for each contact. Sniper watches Karuta drops, reads the cards with OCR, and claims matches from your keyword lists.

## Setup

Install Python 3.10+ and Git. From this folder, install the Python dependencies:

```powershell
python -m pip install -r requirements.txt
```

Copy the config templates for the parts you want to run:

```powershell
Copy-Item 'config template.json' config.json
Copy-Item quinn_config_template.json quinn_config.json
```

Set the Discord account token in each local config. Quinn can also read `QUINN_DISCORD_TOKEN` or fall back to the token in `config.json`.

## Launcher

Controls and launcher in **Start Quinn.pyw**

**Start**, **Quinn**, and **Sniper** launch the corresponding services. **SillyTavern** opens standalone setup.

Service log contains backend output and startup errors.

- Quinn tracks intake, conversation loading, generation, reply verification, and Discord delivery. Each **Generation time** bar shows the elapsed generation time for one reply.
- Sniper tracks the image download, OCR, character/series/print rules, wishlist checks, and claims. Each **OCR time** bar shows total OCR time for one drop.

For terminal interface, run `python launcher.py --terminal`

- **1** opens Sniper in its own terminal.
- **2** starts Quinn, including KoboldCpp, the bridge, SillyTavern, and Discord.
- **3** starts Quinn and Sniper.
- **T** opens standalone SillyTavern for setup or editing.
- **D** toggles Quinn dry run for the next launch. Dry run logs incoming DMs without replying.
- **S** stops new work and gives an active reply up to 120 seconds to finish. Press **S** again to force the stop.
- **Q** stops everything and exits.

Firefox opens a managed SillyTavern tab. If Firefox blocks automatic closure, close that tab manually. Use **Stop all**, **S**, or **Q** to give replies time to finish before shutdown.

## Quinn

Install Firefox, a supported Node.js LTS release, KoboldCpp, and SillyTavern. Put `koboldcpp.exe` and the `SillyTavern/` installation beside the launcher. Run SillyTavern's `Start.bat` once to install its dependencies, then stop it. The launcher installs the Quinn extension into the default SillyTavern profile and backs up the previous copy when it changes. [SillyTavern installation guide](https://docs.sillytavern.app/installation/windows/)

The launcher uses SillyTavern's configured `dataRoot` for both the extension and the server. Relative paths resolve from the SillyTavern directory. `SILLYTAVERN_DATAROOT` overrides `config.yaml`. Keep a custom data directory outside the Quinn checkout or add it to the local Git exclude file before storing chats or presets there.

1. Open KoboldCpp, choose a local GGUF model, and set its context size and hardware options. For local-only access, set the host to `127.0.0.1` and leave remote tunneling disabled. Save the profile as `config.kcpps` beside the launcher. Use `launcher.kobold_config` to point to a profile saved elsewhere. [KoboldCpp setup guide](https://docs.sillytavern.app/usage/api-connections/koboldcpp/)
2. Start the launcher and choose **SillyTavern** (or **T** in terminal mode). Complete the SillyTavern welcome screen if it appears. Select **Text Completion → KoboldCpp** in API Connections and enter `http://127.0.0.1:5001`. Create or import a character card and choose a text completion preset, context template, and instruct format for the model. To test the connection during setup, start KoboldCpp with the saved profile and stop it again before choosing **Quinn**.
3. In **Extensions → Quinn Native DMs**, enter the bridge URL (default `http://127.0.0.1:8765`), the token from `QuinnBridge/bridge/bridge_token.txt`, and the exact character name. Save the settings in the Firefox profile and at the SillyTavern address opened by the launcher.
4. Clear any unsent draft, stop standalone mode with **Stop all**, and choose **Quinn**. The launcher starts Native DMs automatically when the managed tab is ready.


Managed DMs use the active SillyTavern enviroment. Pause the worker before changing settings.

Each contact gets a native SillyTavern chat and persona. SillyTavern inserts active lore entries using its normal World Info rules. [SillyTavern World Info](https://docs.sillytavern.app/usage/core-concepts/worldinfo/) · [SillyTavern Summarize](https://docs.sillytavern.app/extensions/summarize/)

Use **Pause to manage** and wait for **Paused** before editing a chat or persona. Use one working tab. Edits and manual generations stay in SillyTavern. Deleting a chat cancels its pending work and starts a fresh chat on the next DM. **Forget person in this chat** also removes the associated persona details. A delivery marked uncertain needs checking in Discord before any manual resend.

Set `respond_to_dms` to `false` to disable DM intake. `typing_simulation` and the `typing_*` settings control the reply delay. The `launcher` section configures executable paths, ports, the SillyTavern URL, and timeouts. The SillyTavern URL must match the origin where the extension settings were saved. Stop independently started services before launching Quinn on the same ports.

See [bridge maintenance](QuinnBridge/README.md) for migration, recovery, and isolated browser tests.

## Sniper

Replace every `1234567890` placeholder used by enabled features with a real, unquoted Discord ID. `servers` takes server IDs. `channels` takes channel IDs and determines which drops Sniper scans. Set `wishlist_watching_channels` to the watched channels where wishlist checks should run. Run Sniper from the launcher or with `python main.py`. EasyOCR downloads its English model on the first scan.

Edit the files in `keywords/`, one entry per line. `characters.txt` and `animes.txt` are the match lists. `charblacklist.txt` and `aniblacklist.txt` block matches. Use `character, series` to qualify a character by series. Sniper reloads these files while running.

Sniper checks character matches first, then series, low print numbers, and wishlist counts. Set `accuracy` and `blaccuracy` for fuzzy matching. `check_print` and `print_number` control low-print claims. Enable `wishlist_lookup` with CardCompanion and set `wishlist_watching_channels`, `min_wishlist`, and `lookup_delay`. Wishlist queries use `autodropchannel` when available, otherwise the drop channel.

To enable `autodrop`, set it to `true` and set `autodropchannel` to a channel the account can access and send messages to. Include that ID in `channels` if Sniper should scan drops there. The cycle timing uses `dropdelay`, `randmin`, and `randmax`. Set `resourcechannel` to an accessible channel before enabling `autofarm`. If an automation channel is missing or inaccessible, Sniper logs the problem and continues watching for drops. The `log_*` flags control console output and `log.txt`. `debug` and `very_verbose` add more detail. Changes to accuracy and wishlist channel settings reload while running. Restart Sniper for other config changes.

## Checks

```powershell
python -m unittest test_launcher test_dashboard test_quinn_telemetry test_sniper_telemetry
python -m unittest discover -s QuinnBridge/bridge -p 'test_*.py'
node test_launch_browser.cjs
node test_quinn_visuals.cjs
```

