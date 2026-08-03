# 📰 Notizie JR

Repository con **due bot Telegram distinti** per le notizie sulla Juventus:

1. `bot_giornali.py` legge i PDF dei quotidiani da Dropbox e usa Gemini per estrarre notizie verificate;
2. `juve_press_bot.py` monitora siti web, canali YouTube e profili X, quindi segnala soltanto i contenuti delle date richieste che non risultano già notificati.

I due flussi hanno workflow, dipendenze e stato separati.

## Bot PDF: quotidiani sportivi

### Flusso

```text
Dropbox /NotizieJR
        │
        ▼
download dei PDF
        │
        ▼
Gemini: estrazione documentale in JSON
        │
        ▼
controlli deterministici
        │
        ▼
Telegram (divisione locale dei testi lunghi)
        │
        ▼
cancellazione da Dropbox dopo una lettura riuscita
```

`bot_giornali.py`:

- legge tutti i PDF presenti nella cartella Dropbox `/NotizieJR`;
- ricava la testata dal nome del file quando contiene Tuttosport, Gazzetta o Corriere;
- carica ogni PDF su Gemini e richiede un output JSON strutturato;
- usa `gemini-3.5-flash-lite`, con fallback a `gemini-3.1-flash-lite`, `gemini-3.6-flash` e `gemini-3.5-flash` sui limiti `429` o sugli errori temporanei `503`;
- se tutti i modelli sono temporaneamente occupati, attende con backoff e ripete l'intero giro fino a tre volte; i modelli con quota giornaliera esaurita vengono esclusi dai cicli successivi;
- esegue una sola lettura per impostazione predefinita; la seconda verifica sullo stesso documento resta opzionale;
- richiede per ogni notizia fonte, pagina e un breve riscontro testuale;
- richiede testi entro 3.800 caratteri visibili e divide localmente quelli più lunghi in più messaggi Telegram, senza una nuova richiesta Gemini e senza riassumerli;
- normalizza gli importi in milioni di euro (`10M€`, `40-50M€`) senza inventare intervalli;
- elimina duplicati e markup non consentito;
- cancella il file temporaneo da Gemini e dal runner.

Per ogni notizia approvata invia il corpo come testo semplice, senza tag o formattazione. Soltanto il nome della fonte nell'intestazione è in grassetto. Le eventuali parti sono numerate (`1/2`, `2/2`): dalla seconda in poi, ogni messaggio risponde alla parte precedente e forma una catena Telegram.

Tra due giornali attende 20 secondi. Il PDF originale viene cancellato da Dropbox soltanto dopo che Gemini ha completato la lettura, anche quando non trova notizie Juventus. Se il download o l'elaborazione Gemini falliscono, il PDF resta su Dropbox per il run successivo. Dopo una lettura riuscita viene invece cancellato anche in caso di invio Telegram incompleto, evitando duplicati al run seguente.

### Workflow e configurazione

Il workflow [`.github/workflows/run_giornali.yml`](.github/workflows/run_giornali.yml) è solo manuale, usa Python 3.14 ed esegue `bot_giornali.py`.

Configura questi secret:

| Secret | Uso |
|---|---|
| `TELEGRAM_TOKEN` | Token del bot Telegram. |
| `CHAT_ID` | Chat o canale di destinazione. |
| `GEMINI_API_KEY` | Accesso ai modelli Gemini. |
| `DROPBOX_APP_KEY` | App key Dropbox. |
| `DROPBOX_APP_SECRET` | App secret Dropbox. |
| `DROPBOX_REFRESH_TOKEN` | Refresh token OAuth2 Dropbox. |

Impostazioni opzionali lette dal codice:

| Variabile | Default | Effetto |
|---|---:|---|
| `MAX_CARATTERI_NOTIZIA` | `3800` | Limite visibile di ogni parte inviata a Telegram. |
| `USA_DOPPIA_VERIFICA` | `false` | Abilita una seconda richiesta Gemini di verifica. |
| `MAX_CICLI_GEMINI` | `3` | Numero massimo di giri completi sui modelli dopo errori temporanei. |
| `ATTESA_503_GEMINI` | `20` | Attesa iniziale in secondi tra i cicli; raddoppia fino a 60 secondi. |

Il workflow imposta esplicitamente `USA_DOPPIA_VERIFICA=false` per limitare il consumo della quota Gemini.

## Bot web: Juventus Press News

### Fonti monitorate

Nell’esecuzione normale `juve_press_bot.py` raccoglie i contenuti pubblicati nella data italiana corrente.

| Gruppo | Fonti | Regola principale |
|---|---|---|
| Quotidiani | Tuttosport, Corriere dello Sport, La Gazzetta dello Sport | Sezioni o feed dedicati alla Juventus. |
| Altri siti | Sky Sport – Calciomercato, Juventus.com, Gianluca Di Marzio, Alfredo Pedullà, Borsa Italiana | Sky esclude i titoli contenenti `video`; Gianluca Di Marzio accetta solo titoli contenenti `Juventus`; le altre fonti applicano i rispettivi filtri. |
| YouTube | Fabrizio Romano in Italiano, Romeo Agresti | Tutti i video pubblicati nella data richiesta, letti dai feed Atom ufficiali dei canali. |
| X | 11 profili configurati | Lettura tramite mirror RSS pubblici, conversione dei collegamenti in URL `x.com`, rimozione dei simboli `#` e `@` dal testo e separazione degli hashtag CamelCase in parole (`#ForzaJuve` diventa `Forza Juve`). I post con un video nativo vengono inviati come video Telegram. |

I profili X configurati sono:

| Profilo | Contenuti accettati | Repost |
|---|---|---:|
| `@juventusfc` | Tutti i post | inclusi |
| `@Glongari` | Solo post che citano Juve/Juventus | esclusi |
| `@romeoagresti` | Tutti i post | inclusi |
| `@NicoSchira` | Solo post che citano Juve/Juventus | esclusi |
| `@AlfredoPedulla` | Solo post che citano Juve/Juventus | esclusi |
| `@MatteMoretto` | Solo post che citano Juve/Juventus | esclusi |
| `@FabrizioRomano` | Solo post che citano Juve/Juventus | esclusi |
| `@DiMarzio` | Solo post che citano Juve/Juventus | esclusi |
| `@_Morik92_` | Tutti i post | inclusi |
| `@ilbianconerocom` | Tutti i post | inclusi |

Per Sky il bot controlla esclusivamente la pagina della data richiesta. Se la pagina odierna non esiste ancora (`404`), la fonte viene ignorata senza mostrare errori nei log. Juventus.com viene letto attraverso il feed datato e la relativa paginazione.

Gli articoli vengono normalizzati, deduplicati e ordinati dal più vecchio al più recente. Il messaggio Telegram usa un solo formato: fonte, titolo, eventuale sommario e link al contenuto. Se un post X contiene un vero video nativo, il bot usa `sendVideo`; se contiene anche foto reali, invia video e foto insieme con `sendMediaGroup`. Prima dell'upload il bot verifica le tracce con FFmpeg e aggiunge audio silenzioso agli MP4 muti, impedendo a Telegram di mostrarli come GIF. Le GIF animate dichiarate da X non vengono inviate: il bot usa soltanto la loro copertina statica. Se il video non può essere preparato o viene rifiutato, il bot ripiega sulle foto, sulla copertina o sul testo. Se il contenuto espone una foto tramite feed RSS, YouTube, Open Graph o Twitter Card, il bot usa `sendPhoto` e inserisce il testo nella didascalia; se Telegram rifiuta la foto, ripiega sul messaggio testuale. Il client Telegram è separato dagli scraper, restituisce il `message_id` confermato dall'API e ritenta gli errori di rete, i rate limit `429` e gli errori temporanei `5xx`. Lo stato viene aggiornato soltanto dopo la conferma dell'invio.

### Stato anti-duplicati

Gli identificativi notificati sono salvati in `.seen_juve_press_news.json` insieme alla data di riferimento. Al primo run di un nuovo giorno il file viene azzerato automaticamente, perché le fonti ordinarie controllano soltanto i contenuti della data richiesta.

Ogni notizia appena scoperta viene scritta immediatamente in `.pending_juve_press_news.json`, senza attendere il completamento delle altre fonti. Dopo la conferma dell’invio Telegram viene aggiunta allo stato delle notizie inviate e rimossa dal journal. Se una fonte successiva o Telegram falliscono, la notizia rimane nel journal e viene ritentata al run seguente.

In GitHub Actions lo stato viene salvato nel repository come `.seen_juve_press_news.json`. Il workflow imposta `BASELINE_IF_NO_STATE=true`: se il file non esiste ancora, registra le notizie correnti senza inviarle, evitando una raffica al primo avvio. Dopo ogni esecuzione aggiorna il file con un commit, così lo stato resta disponibile anche nei run successivi.

### Workflow e configurazione

Il workflow [`.github/workflows/juve-press-news.yml`](.github/workflows/juve-press-news.yml):

- è avviabile solo manualmente;
- usa Python 3.14;
- legge e aggiorna lo stato versionato `.seen_juve_press_news.json`;
- installa `requirements-juve-press.txt`;
- esegue `python juve_press_bot.py`.

Richiede soltanto:

| Secret | Uso |
|---|---|
| `TELEGRAM_TOKEN` | Token del bot Telegram. |
| `CHAT_ID` | Chat o canale di destinazione. |

Il bot supporta anche una modalità di sola verifica:

```bash
python juve_press_bot.py --dry-run
```

La modalità `--dry-run` recupera e stampa le notizie senza leggere lo stato e senza usare Telegram.

Per visualizzare anche il messaggio HTML esatto che verrebbe inviato:

```bash
python juve_press_bot.py --dry-run --preview-messages
```

Per i test si può includere anche il giorno precedente:

```bash
python juve_press_bot.py --dry-run --include-yesterday
```

`--include-yesterday` amplia la raccolta a oggi e ieri. L’opzione non è usata dal workflow e va considerata uno strumento di test; senza `--dry-run` potrebbe inviare anche contenuti del giorno precedente non presenti nello stato.

## Avvio locale

### PDF

```bash
python -m pip install -r requirements.txt
python bot_giornali.py
```

### Web

```bash
python -m pip install -r requirements-juve-press.txt
python juve_press_bot.py --dry-run
```

Per gli invii reali imposta le variabili d’ambiente richieste dal relativo bot.

## Struttura

```text
Notizie_JR/
├── bot_giornali.py
├── juve_press_bot.py
├── requirements.txt
├── requirements-juve-press.txt
└── .github/workflows/
    ├── run_giornali.yml
    └── juve-press-news.yml
```

## Limiti noti

- L’estrazione PDF dipende dalla leggibilità del documento e dalla risposta di Gemini; i controlli riducono, ma non eliminano, il rischio di errori.
- I selettori HTML e gli endpoint non documentati delle fonti web possono cambiare.
- Il monitoraggio X dipende dai mirror RSS pubblici configurati e, per ricavare gli MP4 dei video nativi, dalle API pubbliche FxTwitter/VxTwitter: se i servizi disponibili sono indisponibili o cambiano formato, il post conserva comunque la copertina e il link a X.
- I feed YouTube includono tutti i video dei due canali configurati, senza un ulteriore filtro Juventus sul titolo.
- Entrambi i workflow sono manuali: il repository non contiene uno `schedule`.
- Lo stato del bot web vive nel file versionato `.seen_juve_press_news.json`; il workflow usa un gruppo di concorrenza per evitare esecuzioni sovrapposte.

---

Progetto amatoriale, non affiliato con Juventus FC, Telegram, Google, Dropbox o le fonti citate.
