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

Set the Discord account token in each local config. Quinn can also read `QUINN_DISCORD_TOKEN` or fall back to the token in `config.json`. Keep the tokens and local configs out of Git. [Discord prohibits automating regular accounts.](https://support.discord.com/hc/en-us/articles/115002192352-Automated-User-Accounts-Self-Bots)

## Launcher

On Windows, double-click **Start Quinn.bat**. It uses the project virtual environment if one exists. You can also run `python launcher.py`.

- **1** opens Sniper in its own terminal.
- **2** starts Quinn, including KoboldCpp, the bridge, SillyTavern, and Discord.
- **3** starts Quinn and Sniper.
- **T** opens standalone SillyTavern for setup or editing.
- **D** toggles Quinn dry run for the next launch. Dry run logs incoming DMs without replying.
- **S** stops new work and gives an active reply up to 120 seconds to finish. Press **S** again to force the stop.
- **Q** stops everything and exits.

Quinn shares one terminal for backend logs. Firefox opens a managed SillyTavern tab. If Firefox blocks automatic closure, close that tab manually. Use **S** or **Q** to give replies time to finish before shutdown.

## Quinn

Install Firefox, Node.js, KoboldCpp, and SillyTavern. The default layout is `koboldcpp.exe`, a saved `config.kcpps` that points to a local model, and a `SillyTavern/` installation beside the launcher. Run SillyTavern's `Start.bat` once to install its dependencies, then stop it. The launcher installs the Quinn extension and backs up the previous copy when it changes.

1. Start the launcher and choose **T**. In SillyTavern, connect to KoboldCpp at `http://127.0.0.1:5001` and create or import the Quinn character. If the connection needs to be tested during setup, run KoboldCpp with the saved config and stop it again before choosing **2**.
2. In **Extensions → Quinn Native DMs**, enter the bridge URL (default `http://127.0.0.1:8765`), the token from `QuinnBridge/bridge/bridge_token.txt`, and the exact character name. Save the settings in the Firefox profile the launcher opens.
3. Clear any unsent draft, stop standalone mode with **S**, and choose **2**. The launcher starts Native DMs automatically when the managed tab is ready.

Use **Pause to manage** and wait for **Paused** before editing a chat or persona. Use one working tab. Edits and manual generations stay in SillyTavern. Deleting a chat cancels its pending work and starts a fresh chat on the next DM. **Forget person in this chat** also removes the associated persona details. A delivery marked uncertain needs checking in Discord before any manual resend.

Set `respond_to_dms` to `false` to disable DM intake. `typing_simulation` and the `typing_*` settings control the reply delay. The `launcher` section configures executable paths, ports, the SillyTavern URL, and timeouts. The SillyTavern URL must match the origin where the extension settings were saved. Stop independently started services before launching Quinn on the same ports.

See [bridge maintenance](QuinnBridge/README.md) for migration, recovery, and isolated browser tests.

## Sniper

Fill in `servers` and `channels` with the Discord IDs to watch. Run it from the launcher or with `python main.py`. EasyOCR downloads its English model on the first scan.

Edit the files in `keywords/`, one entry per line. `characters.txt` and `animes.txt` are the match lists. `charblacklist.txt` and `aniblacklist.txt` block matches. Use `character, series` to qualify a character by series. Sniper reloads these files while running.

Sniper checks character matches first, then series, low print numbers, and wishlist counts. Set `accuracy` and `blaccuracy` for fuzzy matching. `check_print` and `print_number` control low-print claims. Enable `wishlist_lookup` with CardCompanion and set `wishlist_watching_channels`, `min_wishlist`, and `lookup_delay`. Wishlist queries use `autodropchannel` when available, otherwise the drop channel.

`autodrop` uses `autodropchannel`, `dropdelay`, `randmin`, and `randmax`. `autofarm` uses `resourcechannel`. The `log_*` flags control console output and `log.txt`. `debug` and `very_verbose` add more detail. Changes to accuracy and wishlist channel settings reload while running. Restart Sniper for other config changes.

## Checks

```powershell
python -m unittest test_launcher
python -m unittest discover -s QuinnBridge/bridge -p 'test_*.py'
node test_launch_browser.cjs
```

These checks use isolated bridge data and simulated Discord delivery.
