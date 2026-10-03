# Minecraft Skill Builder

An AI companion for **Minecraft Education**, powered by **Azure AI Foundry**. Students chat
with the AI in game, ask it to build anything (including real places from a map) and whole
villages, and take building challenges that give formative feedback against a teacher's
rubric. Teachers follow along and manage rubrics in a web interface.

```
Minecraft Education ──/connect──▶ Minecraft Skill Builder ──HTTPS──▶ Azure AI Foundry
                                   └─ web interface: http://localhost:8080
```

## Quick start

You need **Minecraft Education** and a model deployed in **Azure AI Foundry** (its
endpoint, API key and deployment name). Everything else is installed on first run.

1. **Get the code:** `git clone https://github.com/juedwards/MinecraftSkillBuilder.git`,
   or download it as a ZIP from GitHub and unzip it.
2. **Start it:**
   - **Windows:** double-click **`Start Skill Builder.cmd`**.
   - **WSL, macOS or Linux:** run `./start.sh` in the folder.

   The first run installs [uv](https://docs.astral.sh/uv/) for your user (it brings its
   own Python) and the app's dependencies, which takes a minute. Then the
   **Minecraft Skill Builder** page opens in your browser. Keep the window open while you play.
3. **Connect the AI:** on the page, open **Settings**, enter your Azure AI Foundry
   endpoint, API key and model deployment name, and click **Test & save**.
4. **Connect Minecraft:** in Minecraft Education, open **Settings → General**, turn on
   **Enable Websockets** and turn off **Require Encrypted Websockets**. Open a world
   with cheats on, open chat and type `/connect localhost:3000`.
5. Type `!help` in chat to see what you can do.

You'll find the endpoint and key in the Azure AI Foundry portal under your project's
overview, or under **Models + endpoints** for your deployment. Any model you can call
through the Foundry OpenAI-compatible API works (GPT, and others such as Llama or Mistral).

### Running it by hand

```bash
uv run skillbuilder                   # start (same as `skillbuilder serve`)
uv run skillbuilder --open            # ...and open the web interface
uv run skillbuilder --trigger '!ai'   # only answer chat messages starting with !ai
uv run skillbuilder --private         # reply only to the player who asked
uv run skillbuilder --mock            # echo bot, no Azure needed (to test the Minecraft connection)
uv run skillbuilder check             # send one test prompt to Azure
uv run skillbuilder --no-console      # activity log only, no teacher console prompt
```

`mcchat` is an alias for `skillbuilder`.

### Teacher console (in the terminal)

When you run it in a terminal (WSL, macOS, Linux or Windows), a prompt stays at the bottom while
activity scrolls above it, with a status bar showing Minecraft, the AI and the number of quests.
Ask for things in your own words and the AI assistant proposes the change:

```
quest builder › write a quest about building a treehouse for 8 year olds
quest builder › add a criterion about using redstone to the bridge quest
quest builder › only answer chat that starts with !ai
```

Every change is shown first (settings as old → new, a new quest in full, a changed quest as a
diff) and only applied when you answer `y`. The assistant can change settings and write, change
or delete quests, but never the Azure endpoint, model or key. Its AI use appears on the Costs page
as "Teacher assistant".

Or use commands (Tab completes them and quest names):

| Command | What it does |
|---|---|
| `/status` | Minecraft, AI and quests in progress |
| `/settings` | Show the settings |
| `/set <setting> <value>` | Change a setting: `trigger`, `private`, `history`, `prompt`, `endpoint`, `model`, `key` (asked for, hidden), `currency`, `price-in`, `price-out`. `/set` alone lists them. |
| `/quests`, `/show <quest>` | List quests, show one |
| `/new [quest]`, `/edit <quest>` | Write or edit a quest in your editor (`$EDITOR`, or nano) |
| `/delete <quest>` | Delete a quest |
| `/players` | Players online and who has talked to the AI |
| `/say <message>` | Send a message to everyone in Minecraft |
| `/clear` | Start a new conversation with the assistant |
| `/quit` | Stop the server (or Ctrl+C) |

Changes apply immediately and are saved, just like the web interface. When the output goes to a
file (`skillbuilder > mcchat.log`) there's no prompt and the log is plain text.

### Minecraft Skill Builder (web interface)

The server also serves **Minecraft Skill Builder** at http://localhost:8080
(`--web-port` to change it, `--no-web` to turn it off):

- **Activity:** live feed of connections, player questions, AI answers, builds,
  challenges (with each criterion's level and next steps), setup and errors, with filters.
- **Players:** online players and everyone who has talked to the AI. View each
  player's conversation and reset it.
- **Rubrics:** create, edit and delete the challenge rubrics in `rubrics/`.
- **Costs:** what the AI costs, for today / 7 days / 30 days / all time: total cost, requests,
  tokens and players; a cost-per-day chart; tables by task type (chat, build, map, village,
  challenge setup and feedback, with the average cost per use) and by player; and a **class cost
  planner** that estimates the cost of a course from students × lessons × activities, using your
  real averages. Every AI call's tokens are recorded in `usage.jsonl` (not committed). Set your
  prices per million tokens on the page (defaults are GPT-5 list prices: $1.25 in, $10 out).
- **Settings:** change the Azure endpoint, key and model (tested before saving),
  the trigger prefix, private replies, history length and system prompt. Changes
  apply immediately and are saved to `.env`.

The web server only listens on 127.0.0.1 and rejects requests from other sites,
because the Settings page can change credentials. The API key is never sent to the browser.

### Chat commands

| Command | What it does |
|---|---|
| (any message) | Talk to the AI (or start with the trigger prefix, if one is set). |
| `!build <anything>` | Build it. The AI decides: a real place (`!build the tower of london`) is built around you from a map; anything else (`!build a lighthouse`) is designed and built in front of you. |
| `!village [style] [number]` | Build a village of ~20 buildings around you, with villagers, e.g. `!village viking`, `!village japanese 12`. |
| `!challenge` | Take a building challenge from a rubric; type `finished` when done to get feedback. |
| `!reset` | Clear your conversation with the AI. |
| `!setup` | Connect the AI to Azure from chat (only when no credentials are set). |
| `!cancel` | Stop setup or a challenge. |
| `!help` | List all commands. |

Shortcuts: `!map <place> [size] [m per block]` goes straight to the map (it works without the AI and
takes exact numbers, e.g. `!map big ben 120 3`); `!assess` is the old name for `!challenge`.

During anything slow (designing, downloading maps, placing thousands of blocks, checking a
challenge), the bot posts a short grey progress update whenever it has been quiet for 10 seconds,
e.g. *Placing blocks: 45% (180/400)... (25s)*.

### Villages (`!village`)

`!village viking` (or with no style, and the AI picks one) builds a village around you:

1. A **planner agent** plans the village: a name, ground and path blocks, and ~20 varied
   buildings in one style, starting with a centrepiece for the square (a well, statue...).
2. **Builder agents** design each building in parallel (4 at a time), using the same
   sandboxed pipeline as `!build`. A building that fails is skipped.
3. The buildings are laid out on 11×11 plots in rings around a central square, separated by
   3-block streets, each with its entrance facing the square. The area (about 70×70 blocks
   for 20 buildings) is cleared and given flat ground first, so **it replaces whatever was
   there**. You're moved to the square, and a villager is summoned outside each building.

It takes about 2–3 minutes for 20 buildings. Add a number for more or fewer (4–24).

### Building (`!build`)

`!build` first asks the AI what kind of request it is. A named real place ("the tower of london",
"times square", "my school, Hillside Primary in Leeds") is built from a map; anything else,
including things "like" or "inspired by" a place ("a castle like the tower of london"), is designed.
The AI also turns "big", "detailed" and similar words into the map's size and scale.

### Real places

`!build the tower of london` (or `!map tower of london`) builds the real place around you, north-up, from
[OpenStreetMap](https://www.openstreetmap.org/) data. It's our own Python implementation of
the approach used by [Arnis](https://github.com/louis-e/arnis), done live with commands:

1. The place is found with OpenStreetMap's Nominatim geocoder (if it isn't found and the AI is
   connected, the AI suggests a better search).
2. Buildings, roads, paths, railways, rivers, lakes, parks, land use and trees are downloaded
   from the Overpass API for a square around it.
3. Everything is projected onto the block grid: areas are filled, lines are drawn at their real
   widths, river outlines made of many pieces are joined, and buildings are raised to their real
   height (`height` or `building:levels`, up to 160 blocks) with walls, window bands and roofs.
   **Materials** come from the map's tags: `building:material` (brick, stone, limestone, sandstone,
   marble, timber, glass, copper...), `roof:material` (slate, clay tiles, copper, thatch...), and
   `building:colour` / `roof:colour`, matched to the nearest real facade block (brick, stone bricks,
   sandstone, quartz, terracotta) by how colours look to people. Untagged buildings get a realistic mix
   of facades. Windows are glass panes: separate windows in masonry, shop windows at street level,
   mostly glass for offices and towers, and flat-roofed masonry gets a stone cornice. Steel and
   lattice structures (towers, masts) become iron bars, and 3D parts inherit their building's
   material unless they set their own.
4. **3D shapes.** Where OpenStreetMap has "Simple 3D Buildings" data, each `building:part` is built
   from its own `min_height` to `height` with its `roof:shape` (pyramidal, hipped, gabled, dome,
   onion, cone), replacing the flat outline. Many landmarks are mapped this way: the Eiffel Tower's
   legs, platforms, tiers, dome and antenna; the Leaning Tower of Pisa's tiers and bell chamber;
   the Colosseum's rings.
5. **AI landmark models.** Famous structures that have only a flat outline (a Wikidata entry plus
   a landmark tag such as `tourism`, `historic` or `man_made`) are modelled by the AI at their
   real footprint and height: always the place you asked for, and up to 2 big landmarks per map.
   (Arnis ships hand-built models for a few landmarks; ours are designed on demand.)
6. The area is cleared and built with merged `fill` commands; you're moved to open ground first.

Options: a size in blocks (32–128, default 96) and metres per block (0.5–10, default 2), e.g.
`!map big ben 120 3`. The terrain is flat. `!map` doesn't need the AI, so it works without Azure.
The public map servers are sometimes busy; downloads are retried automatically.

Map data © OpenStreetMap contributors, available under the
[Open Database Licence](https://www.openstreetmap.org/copyright). The bot shows this credit after each map.

### Challenges (`!challenge`)

`!challenge` lists the rubrics in `rubrics/` (Markdown files). The student types a number to choose, then:

1. The AI reads the rubric and designs a **partially completed starting scene**
   (for example two riverbanks with an unfinished bridge). It builds the scene on its
   own platform about 12 blocks ahead of the student, teleports them to a start point,
   and gives them the task.
2. While they work, their `BlockPlaced` / `BlockBroken` events and chat are recorded.
3. When they type **finished**, every block in the task area is inspected with
   `/testforblock` (up to 5,000 blocks) and compared with the starting scene.
4. The AI gives **formative feedback** against the rubric: a level for each criterion
   with evidence, strengths, and next steps to do better. It then asks whether
   they'd like to **try again**. "yes" rebuilds the same scene for a new attempt.
5. Each attempt is saved as a Markdown assessment report in `assessments/`, including the
   activity log and the inspection map.

`!cancel` stops a challenge. A rubric works best with these sections: `# Title`,
`## Learning aims`, `## Learning objectives`, `## Task`, `## Starter build` (what the
AI builds and what it leaves for the student) and `## Assessment criteria` (a table of
levels). See [`rubrics/build_a_bridge.md`](rubrics/build_a_bridge.md), or
[`rubrics/new_york_skyscraper.md`](rubrics/new_york_skyscraper.md): design an Art Deco
skyscraper on an empty corner lot in 1930s Manhattan (a tall 10×10×45 task area).

A rubric can use a village instead: give it a `## Starter village` section describing the
village's style. The challenge then builds a 12-building village around the student, leaving
the plot in front of them empty, and that plot is the task area. See
[`rubrics/build_a_village_home.md`](rubrics/build_a_village_home.md).

Or a real place: a `## Starter map` section such as
`Clifton Suspension Bridge, Bristol, without bridges, size 96, scale 5` builds that place from
OpenStreetMap (here leaving the bridges out), and the task area is the middle 22×22 blocks of the
map. See [`rubrics/bridge_the_avon.md`](rubrics/bridge_the_avon.md).

### Designed builds

`!build a small oak cabin with a red roof` designs the structure with the LLM and builds
it in front of you, with its entrance facing you. The world needs cheats on.

How it works (adapted from [BuilderGPT](https://github.com/CyniaAI/BuilderGPT)):

1. The LLM writes a JavaScript `buildCreation()` function using `safeFill` and
   `safeSetBlock`, restricted to a list of Bedrock block IDs.
2. The script runs in a QuickJS sandbox (3 s time limit, 64 MB memory, max 5,000
   operations, max 48 blocks per side) that records the block operations.
3. `/querytarget` finds the player's position and facing. The design is rotated to
   face them and sent as `fill` / `setblock` commands, with large fills split to
   Bedrock's 32,768-block limit.

Only one build runs at a time per world. Block states (stair direction etc.) aren't supported yet.

**Blocks.** Builds can use about 240 blocks (`src/mcchat/palette.py`): the ones proven in
Minecraft Education, plus newer ones such as stone brick variants, cut and smooth sandstone, smooth
quartz, terracotta, copper (including oxidized), slabs, stairs and glass panes. Bedrock has renamed
many blocks between versions, so each newer block has fallbacks: if a world rejects a block name,
the server retries with the fallback (e.g. `stone_bricks` → `stonebrick`) and remembers what works
for that world.

### Setting up from chat (`!setup`)

The Settings page is the easiest way to add credentials. Alternatively, if the server
starts without Azure credentials, type `!setup` in Minecraft chat. The bot asks, privately,
for the endpoint, API key and model deployment name one at a time. It tests them and saves
them to `.env`. Type `!cancel` to stop.

- Everything a player types in chat is visible to everyone in the world, including
  the API key. Only run `!setup` in a private world.
- `!setup` only works while no credentials are configured, so players can't repoint
  the bot. To change credentials later, edit `.env` and restart.
- Only one player can run setup at a time (it times out after 5 minutes idle).

## Hosting on Azure (for many worlds and players)

Run one server in Azure that many Minecraft worlds connect to, each with its own build queue.
`deploy/azure/deploy.ps1` sets it up on **Azure App Service** (Linux, Python 3.12, about US$13 a
month on the B1 tier):

```powershell
az login
./deploy/azure/deploy.ps1            # shows the plan and asks before creating anything
./deploy/azure/deploy.ps1 -CodeOnly  # later: upload new code to the same app
```

It creates a resource group, App Service plan and web app (WebSockets and Always On), an
Entra ID app registration for sign-in, and the app settings (your Azure AI endpoint, key and
model come from your local `.env`). How it works when hosted (`HOSTED=true`):

- **One address.** Minecraft connects to `ws://<app>.azurewebsites.net/mc/<join code>`; the
  exact `/connect` command is shown at the top of the Activity tab. The secret **join code**
  stops strangers using your AI. Minecraft Education uses plain `ws://` (it didn't accept
  `wss://` in testing), so this connection isn't encrypted: treat the join code as a classroom
  password, not a secret for sensitive data. To change it, remove `joinCode` from
  `deploy/azure/.deploy-state.json` and rerun the deploy script (without `-CodeOnly`).
- **Sign-in.** The teacher pages need Microsoft Entra ID sign-in, using App Service's built-in
  authentication. The app refuses to show them if that sign-in isn't switched on. Only accounts
  in your directory can sign in; invite other teachers as guests.
- **Data** (settings, rubrics, reports, usage, map cache) lives in `/home/data`, which persists
  across restarts and deployments. Changes saved on the Settings page win over the initial app settings.

Remove everything with `az group delete -n rg-minecraft-skill-builder`.

## Troubleshooting

- **`/connect` does nothing or says it's already connected:** Minecraft allows one
  connection at a time. Run `/closewebsocket` (or leave and rejoin the world), then `/connect` again.
- **Can't connect from WSL:** WSL2 normally forwards localhost to Windows.
  If it doesn't, use the WSL IP address that the server prints, or turn on
  mirrored networking (`networkingMode=mirrored` in `%UserProfile%\.wslconfig`, then `wsl --shutdown`).
- **"Encrypted connection required":** turn off **Require Encrypted Websockets** in Minecraft settings.
- **The first run can't download packages** (TLS or handshake errors): your network may
  block or inspect downloads from PyPI. The launchers already trust the system certificate
  store; if it still fails, ask IT to allow `pypi.org`, `files.pythonhosted.org` and `astral.sh`,
  or run from WSL.
- **Windows SmartScreen blocks `Start Skill Builder.cmd`:** choose **More info → Run anyway**,
  or right-click the file → Properties → **Unblock**.
- **No replies:** run `uv run skillbuilder check` to test Azure, and `uv run skillbuilder -v` for debug logs.

## Configuration

All settings live in `.env` (see `.env.example`). CLI flags override them.

| Variable | Default | Meaning |
|---|---|---|
| `AZURE_AI_ENDPOINT` | | Foundry or Azure OpenAI endpoint (any form) |
| `AZURE_AI_API_KEY` | | API key |
| `AZURE_AI_MODEL` | | Model deployment name |
| `MC_HOST` / `MC_PORT` | `0.0.0.0` / `3000` | Where the WebSocket server listens (the Windows launcher uses `127.0.0.1`) |
| `WEB_HOST` / `WEB_PORT` | `127.0.0.1` / `8080` | Where the web interface listens |
| `MC_TRIGGER` | (empty) | Only answer messages with this prefix |
| `MC_REPLY_PRIVATE` | `false` | Reply only to the asking player |
| `MAX_HISTORY` | `20` | Past messages remembered per player |
| `SYSTEM_PROMPT` | school-friendly assistant | Custom system prompt |

## Project layout

```
Start Skill Builder.cmd   Windows launcher (runs scripts/start-windows.ps1)
start.sh                  WSL / macOS / Linux launcher
rubrics/                  Assessment rubrics (Markdown), editable in the web interface
assessments/              Saved challenge reports (created on the first challenge, not committed)
src/mcchat/
  minecraft.py  WebSocket server + Minecraft protocol (subscribe, commands, tellraw)
  llm.py        Azure AI Foundry client (OpenAI v1 API) and an echo mock
  bridge.py     Per-player conversations, triggers, !help/!reset/!setup/!build; emits events
  builder.py    !build: LLM build script -> QuickJS sandbox -> fill/setblock commands
  assessment.py !challenge: rubrics, starting scenes, activity recording, inspection, feedback
  village.py    !village: planner + builder agents, plot layout, streets, villagers
  realworld.py  Real places: OpenStreetMap geocoding + Overpass download, projection, rasterising, buildings
  router.py     !build: the AI decides between a real place (map) and a design
  progress.py   Progress updates in chat during long operations
  setup_wizard.py  Step-by-step state for collecting credentials in chat
  config.py     .env / environment settings
  runtime.py    Wires server + bridge + LLM; event log; settings updates (shared by CLI and web)
  webapp.py     Minecraft Skill Builder web server (aiohttp): REST API + server-sent events
  static/       The Minecraft Skill Builder page
  cli.py        `skillbuilder [serve]` and `skillbuilder check`
```

The CLI log and the web feed both subscribe to the runtime's `EventLog`.

## Tests

```bash
uv run pytest
```
