# Trakt Multi-Scrobbler (Jellyfin → Trakt)

[English version](README.md)

<img src="static/scrobbler_icon.webp" alt="Trakt Multi-Scrobbler Logo" width="256" />

Dashboard web per scegliere quali utenti Jellyfin scrobblano verso quali account Trakt. Supporta più utenti per entrambi i servizi, regole per serie/film, tema chiaro/scuro e gestione account Trakt via device flow.

## Novità 0.2
- Regole/stato sync in SQLite (i token restano in JSON) con migrazione automatica dalle installazioni precedenti.
- Pagine per singolo account: lista contenuti assegnati, filtri/ricerca, rimozione associazioni e sync mirata di un solo account Trakt.
- Backup/ripristino dalla UI (ZIP con token Trakt JSON + db SQLite + stato Jellyfin).
- Ritocchi UI: toggle stato account dalla pagina dedicata, filtri alfabetici/tipo anche lì, scorciatoia backup in header.

## Funzionalità principali
- Lettura libreria Jellyfin (film/episodi) con ID TMDB/IMDB/TVDB e locandine.
- Scelta degli utenti Jellyfin che fungono da “fonte” (persistita).
- Regole per contenuto: per ogni film/serie decidi a quali account Trakt inviare gli scrobble.
- Sync automatica/su richiesta verso Trakt, con filtri per nuovi titoli e “Unassigned”.
- Aggiunta/rimozione account Trakt dalla UI (device flow) e toggle per abilitarli.
- Pagina dedicata per ogni account Trakt per vedere cosa sincronizza.
- Backup/ripristino della configurazione (token JSON + db SQLite) dalla UI.
- Tema chiaro/scuro e localizzazione (en/it).

## Requisiti
- Jellyfin con API key.
- App Trakt con `client_id` e `client_secret` (https://trakt.tv/oauth/applications).
- Python 3.11+ oppure Docker.

## Configurazione rapida
1) **Clona il repo**
   ```bash
   git clone https://github.com/gioxx/trakt-multi-scrobbler.git
   cd trakt-multi-scrobbler
   ```

2) **Variabili d’ambiente minime**
   ```bash
   export JELLYFIN_URL="https://il-tuo-jellyfin"
   export JELLYFIN_APIKEY="API_KEY_JELLYFIN"
   export TRAKT_CLIENT_ID="CLIENT_ID_TRAKT"
   export TRAKT_CLIENT_SECRET="CLIENT_SECRET_TRAKT"
   ```
   Opzionali:
   ```bash
   export TRAKT_STATE_PATH="trakt_accounts.json"     # percorso stato account Trakt
   export TRAKT_DB_PATH="trakt_sync.db"              # facoltativo; SQLite con regole di sync (di default accanto a TRAKT_STATE_PATH)
   export JELLYFIN_STATE_PATH="jellyfin_state.json"  # facoltativo; di default usa la stessa cartella di TRAKT_STATE_PATH
   export THUMB_CACHE_DIR="/data/thumb_cache"        # facoltativo; cache locale poster (default accanto a TRAKT_STATE_PATH)
   export THUMB_CACHE_TTL_HOURS="72"                 # facoltativo; aggiorna la cache poster ogni N ore
   export WATCH_THRESHOLD="0.95"                     # soglia completamento (0-1)
   export REFRESH_MINUTES="30"                       # polling Jellyfin
   ```

3) **Avvio locale (Python)**
   ```bash
   pip install -r requirements.txt
   uvicorn app.main:app --reload --host 0.0.0.0 --port 8089
   ```
   Apri http://localhost:8089.

4) **Avvio con Docker (immagine già pronta)**
   - GitHub Container Registry  
     ```bash
     docker run -d --name trakt-multi-scrobbler \
       -p 8089:8089 \
       -e JELLYFIN_URL="https://il-tuo-jellyfin" \
       -e JELLYFIN_APIKEY="API_KEY_JELLYFIN" \
       -e TRAKT_CLIENT_ID="CLIENT_ID_TRAKT" \
       -e TRAKT_CLIENT_SECRET="CLIENT_SECRET_TRAKT" \
       -e TRAKT_STATE_PATH="/data/trakt_accounts.json" \
       -e JELLYFIN_STATE_PATH="/data/jellyfin_state.json" \
       -v tms-data:/data \
       ghcr.io/gioxx/trakt-multi-scrobbler:latest
     ```
   - Docker Hub  
     ```bash
     docker run -d --name trakt-multi-scrobbler \
       -p 8089:8089 \
       -e JELLYFIN_URL="https://il-tuo-jellyfin" \
       -e JELLYFIN_APIKEY="API_KEY_JELLYFIN" \
       -e TRAKT_CLIENT_ID="CLIENT_ID_TRAKT" \
       -e TRAKT_CLIENT_SECRET="CLIENT_SECRET_TRAKT" \
       -e TRAKT_STATE_PATH="/data/trakt_accounts.json" \
       -e JELLYFIN_STATE_PATH="/data/jellyfin_state.json" \
       -v tms-data:/data \
       gfsolone/trakt-multi-scrobbler:latest
     ```

5) **Avvio con Docker Compose (file del repo)**
   ```bash
   docker compose up --build
   ```
   Usa il volume nominato previsto in `docker-compose.yml` (`/data`). Se vuoi inizializzarlo:
   ```bash
   docker compose run --rm trakt-multi-scrobbler sh -c 'cat > /data/trakt_accounts.json <<EOF\n{ \"accounts\": [], \"last_synced\": {} }\nEOF'
   ```

## Collegare account Trakt (device flow)
- Dalla UI clicca “Add Trakt account”, copia il codice, apri il link, autorizza: i token vengono salvati in `TRAKT_STATE_PATH` (JSON); regole di sync e timestamp sono nel file SQLite `TRAKT_DB_PATH`.
- In alternativa, via curl:
  1. `POST https://api.trakt.tv/oauth/device/code` con `client_id`.
  2. Autorizza via `verification_url` con `user_code`.
  3. `POST https://api.trakt.tv/oauth/device/token` con `client_id`, `client_secret`, `code` per ottenere `access_token`/`refresh_token`/`expires_in`.
  4. Calcola `expires_at = now + expires_in` (secondi) e inserisci nel JSON.

## Come usare la UI
- **Jellyfin User(s)**: scegli quali utenti Jellyfin sono monitorati (checkbox nel modale). Persistenza in `JELLYFIN_STATE_PATH`.
- **Trakt User(s)**: aggiungi/rimuovi account via device flow, attiva/disattiva con la checkbox. I token stanno in `TRAKT_STATE_PATH`; regole e stato sync sono nel database SQLite `TRAKT_DB_PATH`. Ogni scheda apre la pagina dedicata dell'account.
- **Backup & restore**: dalla UI principale puoi scaricare uno ZIP con token JSON + db SQLite (e stato Jellyfin); carica lo ZIP per ripristinare su un'altra installazione.
- **Content filters**: ricerca, filtro tipo (film/serie), filtro alfabetico e filtro per account Trakt; assegna le regole per film/serie (checkbox per account). “Unassigned” mostra i titoli senza destinazione.
- **Sync to Trakt**: invia subito gli eventi completati; la sync gira anche in automatico ogni `REFRESH_MINUTES`.
- **Refresh Jellyfin**: forza l’aggiornamento di libreria/utenti/cache.
- **Recently watched**: ultimi 6 titoli visti dagli utenti Jellyfin selezionati.

## Note e limiti
- Vengono scrobblati solo i titoli con ID TMDB/IMDB/TVDB.
- I timestamp inviati a Trakt sono quelli originali di Jellyfin.
- I token Trakt vengono refreshati automaticamente.
- Localizzazione: i file sono in `static/locales/en.json` e `static/locales/it.json`. Per aggiungere una lingua crea `static/locales/<codice>.json` e aggiungi l’opzione al select lingua in `static/index.html`.
- Se usi Plex come principale ma hai anche Jellyfin, puoi abbinarlo a `luigi311/jellyplex-watched` (https://github.com/luigi311/JellyPlex-Watched) per tenere allineati Jellyfin e Plex.
