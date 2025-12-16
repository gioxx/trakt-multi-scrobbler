# Trakt Multi-Scrobbler (Jellyfin → Trakt)

Piccola dashboard che legge cosa hai visto su Jellyfin e lo marca come “watched” su uno o più account Trakt. Ogni account Trakt ha una checkbox per attivare o disattivare lo scrobbling e puoi collegare nuovi account direttamente dalla UI (device flow guidato).

## Cosa fa
- Scarica la cronologia “Played” di Jellyfin (film ed episodi) usando `JELLYFIN_URL` e `JELLYFIN_APIKEY`.
- Costruisce eventi completati con data/ora e ID TMDB/IMDB/TVDB.
- Per ogni account Trakt abilitato, invia gli eventi a `POST https://api.trakt.tv/sync/history` preservando il timestamp originale.
- Tiene `last_synced` per ogni utente Trakt così da non duplicare scrobble.
- L’interfaccia web mostra gli account Trakt (checkbox) e un pulsante “Sync to Trakt”.

## Requisiti
- Jellyfin con API key.
- Un’app Trakt (prendi `client_id` e `client_secret` da https://trakt.tv/oauth/applications).
- Token di ogni utente Trakt che vuoi scrobblare.
- Python 3.11+ (o Docker).

## Configurazione passo-passo (principiante)
1) **Clona il repo**  
   ```bash
   git clone https://github.com/gioxx/trakt-multi-scrobbler.git
   cd trakt-multi-scrobbler
   ```

2) **Crea il file di configurazione Trakt** (`trakt_accounts.json` nella root, può anche essere vuoto: verrà popolato dalla UI):  
   ```json
   {
     "accounts": [
       {
         "username": "nome-utente-trakt",
         "access_token": "...",
         "refresh_token": "...",
         "expires_at": 1730000000,
         "enabled": true
       }
     ],
     "last_synced": {}
   }
   ```
   - `expires_at` è un timestamp Unix (secondi) quando scade l’`access_token`.
   - Puoi inserire più account: ognuno comparirà con una propria checkbox.

3) **Ottieni i token Trakt (una volta per ogni utente)**  
   Hai due alternative:
   - **Via UI guidata (consigliato)**: avvia l’app, clicca “Add Trakt account”, copia il codice, apri il link e autorizza. L’app aggiungerà automaticamente `access_token`/`refresh_token`/`expires_at` al file `trakt_accounts.json`.
   - **Via curl manuale**:
     - Vai su https://trakt.tv/oauth/applications e prendi `client_id` e `client_secret` della tua app.
     - Avvia il device flow per ottenere il `user_code`:
       ```bash
       curl -X POST https://api.trakt.tv/oauth/device/code \
         -H "Content-Type: application/json" \
         -d '{"client_id":"<client_id>"}'
       ```
       Annota `user_code`, `device_code` e `verification_url`.
     - Apri `verification_url`, inserisci `user_code` e autorizza.
     - Scambia il `device_code` per i token:
       ```bash
       curl -X POST https://api.trakt.tv/oauth/device/token \
         -H "Content-Type: application/json" \
         -d '{"client_id":"<client_id>","client_secret":"<client_secret>","code":"<device_code>"}'
       ```
       L’output contiene `access_token`, `refresh_token` ed `expires_in` (in secondi). Calcola `expires_at` = `now + expires_in` (in secondi, non millisecondi) e incollalo nel JSON.

4) **Imposta le variabili d’ambiente** (esempio):
   ```bash
   export JELLYFIN_URL="https://il-tuo-jellyfin"
   export JELLYFIN_APIKEY="API_KEY_JELLYFIN"
   export TRAKT_CLIENT_ID="CLIENT_ID_TRAKT"
   export TRAKT_CLIENT_SECRET="CLIENT_SECRET_TRAKT"
   export TRAKT_STATE_PATH="trakt_accounts.json"   # opzionale
   export WATCH_THRESHOLD="0.95"                   # opzionale, % vista per dire “completato”
   export REFRESH_MINUTES="30"                     # opzionale, polling Jellyfin
   ```
   > Nota: `TRAKT_CLIENT_ID` e `TRAKT_CLIENT_SECRET` sono quelli della tua app Trakt e valgono per tutti gli utenti Trakt. Ogni utente ha poi i **propri** `access_token`/`refresh_token`/`expires_at` nel file `trakt_accounts.json`.

5) **Avvia in locale (Python)**  
   ```bash
   pip install -r requirements.txt
   uvicorn app.main:app --reload --host 0.0.0.0 --port 8089
   ```
   Poi apri http://localhost:8089.

6) **Avvia con Docker**  
   ```bash
   docker compose up --build
   ```
   Assicurati di montare `trakt_accounts.json` nel container (vedi `docker-compose.yml` già pronto).

### Posso ottenere i token Trakt via pagina web?
Sì. La UI integra il device flow Trakt: “Add Trakt account” mostra il codice e il link di verifica, poi salva i token nel JSON una volta approvato. In alternativa puoi usare i comandi `curl` indicati sopra.

## Come usarlo
- **Trakt accounts**: vedi l’elenco degli utenti Trakt trovati nel JSON; attiva/disattiva la checkbox per decidere chi riceve gli scrobble.
- **Sync to Trakt**: invia subito tutti gli eventi completati rilevati in Jellyfin (altrimenti la sync gira automaticamente ogni `REFRESH_MINUTES`).
- **Jellyfin history**: scegli un utente Jellyfin per vedere cosa ha guardato; i poster e le date vengono direttamente da Jellyfin.

## Note e limiti
- Sono inviati a Trakt solo gli elementi con ID TMDB/IMDB/TVDB (servono per il match).
- I timestamp inviati a Trakt sono quelli originali di Jellyfin.
- I token Trakt vengono aggiornati automaticamente tramite `refresh_token` quando servono.
- Localizzazione: file in `static/locales/en.json` e `static/locales/it.json`.
