# GT86 Tracker

Traccia giornalmente gli annunci Toyota GT86 in vendita su [AutoScout24.it](https://www.autoscout24.it/lst/toyota/gt86), con storico prezzi, nuovi/rimossi e una mappa.

Perché così: sia questo ambiente sia molte reti aziendali/domestiche bloccano l'accesso diretto ad autoscout24.it. Per questo lo scraping gira su **GitHub Actions** (rete libera) una volta al giorno, salva i dati nel repo, e la dashboard in `docs/index.html` li legge tramite **GitHub Pages** — nessuna richiesta ad AutoScout24 parte dal tuo PC o dal browser.

## Struttura

- `scraper/scrape_gt86.py` — scarica gli annunci, calcola nuovi/rimossi, aggiorna lo storico prezzi, geocodifica le città (Nominatim/OpenStreetMap).
- `.github/workflows/scrape.yml` — esegue lo scraper ogni giorno (~08:17 ora italiana) e committa i dati aggiornati.
- `docs/index.html` — dashboard (mappa Leaflet + tabella + statistiche), pubblicata via GitHub Pages.
- `docs/data/current.json` — annunci attivi oggi.
- `docs/data/history.json` — database completo con storico prezzi e annunci rimossi (probabilmente venduti).

## Setup (una tantum)

1. **Crea un repository su GitHub** (pubblico o privato — con privato GitHub Pages richiede un piano Pro):
   vai su https://github.com/new, dagli un nome (es. `gt86-tracker`), non aggiungere README/gitignore (li abbiamo già), crea il repo.

2. **Collega questo progetto locale al repo** (sostituisci l'URL con quello del tuo repo):
   ```
   git remote add origin https://github.com/<tuo-utente>/gt86-tracker.git
   git branch -M main
   git push -u origin main
   ```

3. **Attiva i permessi di scrittura per le Actions** (serve perché il workflow fa commit dei dati):
   Settings → Actions → General → Workflow permissions → seleziona **Read and write permissions** → Save.

4. **Attiva GitHub Pages**:
   Settings → Pages → Build and deployment → Source: **Deploy from a branch** → Branch: **main**, cartella **/docs** → Save.
   Dopo un paio di minuti la dashboard sarà su `https://<tuo-utente>.github.io/gt86-tracker/`.

5. **Avvia la prima raccolta dati manualmente** (non serve aspettare il cron):
   Tab **Actions** del repo → workflow **Scrape GT86 AutoScout24** → **Run workflow**.
   Controlla i log: se trova 0 annunci, apri l'artifact `debug-page` caricato dal job per capire cosa è cambiato nella pagina (il parser usa un'euristica sul JSON della pagina, che potrebbe richiedere un piccolo aggiustamento se AutoScout24 cambia struttura — in tal caso incolla l'HTML di debug in una nuova conversazione e sistemiamo il parser).

Da quel momento in poi il workflow gira da solo ogni giorno, aggiorna `docs/data/*.json` e la dashboard mostra sempre i dati più recenti.

## Note

- Le coordinate sulla mappa sono approssimative: derivano dal geocoding della città indicata nell'annuncio, non dall'indirizzo esatto del venditore.
- Un annuncio è marcato "rimosso" (probabile venduto) se non compare più tra i risultati di ricerca del giorno.
- Lo scraping fa solo richieste alla ricerca pubblica, una volta al giorno: pensato per uso personale.
