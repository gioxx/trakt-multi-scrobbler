# Trakt Multi-Scrobbler (Jellyfin → Trakt)

[Italian version](README.IT.md)

![Logo](static/scrobbler_icon.webp)

Web dashboard to map Jellyfin watches to one or more Trakt accounts. Multi-user on both sides, per-title rules, light/dark themes, and Trakt account management via device flow.

## Features
- Reads Jellyfin library (movies/episodes) with TMDB/IMDB/TVDB IDs and posters.
- Choose which Jellyfin users are tracked as the “source” (persisted).
- Per-title rules: decide which Trakt accounts receive each movie/series.
- Manual/automatic sync to Trakt, filters for new titles and “Unassigned”.
- Add/remove Trakt accounts from the UI (device flow) and toggle them on/off.
- Light/dark themes and localization (en/it).

## Requirements
- Jellyfin with API key.
- Trakt app with `client_id` and `client_secret` (https://trakt.tv/oauth/applications).
- Python 3.11+ or Docker.

## Quick setup
1) **Clone**
   ```bash
   git clone https://github.com/gioxx/trakt-multi-scrobbler.git
   cd trakt-multi-scrobbler
   ```

2) **Minimum environment vars**
   ```bash
   export JELLYFIN_URL="https://your-jellyfin"
   export JELLYFIN_APIKEY="YOUR_JELLYFIN_API_KEY"
   export TRAKT_CLIENT_ID="YOUR_TRAKT_CLIENT_ID"
   export TRAKT_CLIENT_SECRET="YOUR_TRAKT_CLIENT_SECRET"
   ```
   Optional:
   ```bash
   export TRAKT_STATE_PATH="trakt_accounts.json"     # Trakt state path
   export JELLYFIN_STATE_PATH="jellyfin_state.json"  # Jellyfin user selection path (default: same dir as TRAKT_STATE_PATH)
   export WATCH_THRESHOLD="0.95"                     # completion threshold (0-1)
   export REFRESH_MINUTES="30"                       # Jellyfin polling interval
   ```

3) **Run locally (Python)**
   ```bash
   pip install -r requirements.txt
   uvicorn app.main:app --reload --host 0.0.0.0 --port 8089
   ```
   Then open http://localhost:8089.

4) **Run with Docker**
   ```bash
   docker compose up --build
   ```
   Uses the named volume in `docker-compose.yml` (`/data`). To initialize manually:
   ```bash
   docker compose run --rm trakt-multi-scrobbler sh -c 'cat > /data/trakt_accounts.json <<EOF\n{ \"accounts\": [], \"last_synced\": {} }\nEOF'
   ```

## Connect Trakt accounts (device flow)
- In the UI click “Add Trakt account”, copy the code, open the link, authorize: tokens are stored in `TRAKT_STATE_PATH`.
- Or via curl:
  1. `POST https://api.trakt.tv/oauth/device/code` with `client_id`.
  2. Authorize using `verification_url` and `user_code`.
  3. `POST https://api.trakt.tv/oauth/device/token` with `client_id`, `client_secret`, `code` to get `access_token`/`refresh_token`/`expires_in`.
  4. Compute `expires_at = now + expires_in` (seconds) and place it in the JSON.

## Using the UI
- **Jellyfin User(s)**: choose which Jellyfin users are tracked (modal checkboxes). Stored in `JELLYFIN_STATE_PATH`.
- **Trakt User(s)**: add/remove accounts via device flow; enable/disable with the toggle. Stored in `TRAKT_STATE_PATH`.
- **Content filters**: search, filter by type (movies/series), alphabet filter, and Trakt-account filter; set per-title rules (checkbox per account). “Unassigned” shows titles with no targets.
- **Sync to Trakt**: push completed events immediately; also runs automatically every `REFRESH_MINUTES`.
- **Refresh Jellyfin**: force library/user/cache refresh.
- **Recently watched**: latest 6 titles watched by selected Jellyfin users.

## Notes and limitations
- Only titles with TMDB/IMDB/TVDB IDs are scrobbled.
- Trakt receives the original Jellyfin timestamps.
- Trakt tokens refresh automatically.
- Localization: existing files in `static/locales/en.json` and `static/locales/it.json`. To add a new language, create `static/locales/<code>.json` and add the option to the language select in `static/index.html`.
