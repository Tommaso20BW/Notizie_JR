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
Gemini: estrazione JSON + verifica documentale
        │
        ▼
controlli deterministici e limite caratteri
        │
        ▼
Telegram (due versioni per notizia)
        │
        ▼
cancellazione da Dropbox indipendentemente dall'esito
```

`bot_giornali.py`:

- legge tutti i PDF presenti nella cartella Dropbox `/NotizieJR`;
- ricava la testata dal nome del file quando contiene Tuttosport, Gazzetta o Corriere;
- carica ogni PDF su Gemini e richiede un output JSON strutturato;
- usa prima `gemini-3.5-flash` e passa a `gemini-2.5-flash` soltanto per errori `503`/servizio sovraccarico;
- esegue di default una seconda lettura di verifica sullo stesso documento;
- richiede per ogni notizia fonte, pagina e un breve riscontro testuale;
- mantiene ogni testo entro 280 caratteri visibili senza troncare frasi;
- normalizza gli importi in milioni di euro (`10M€`, `40-50M€`) senza inventare intervalli;
- elimina duplicati e markup non consentito;
- cancella il file temporaneo da Gemini e dal runner.

Per ogni notizia approvata invia:

1. una versione editoriale con persone/squadre evidenziate, fonte e firma `@Juventus_Reborn`;
2. una versione con hashtag e handle della testata.

Tra due giornali attende 20 secondi. Ogni PDF originale viene cancellato da Dropbox dopo il tentativo di elaborazione, anche se Gemini, la validazione o uno degli invii Telegram falliscono. Se fallisce il download, il bot tenta comunque la cancellazione del PDF remoto.

### Workflow e configurazione

Il workflow [`.github/workflows/run_giornali.yml`](.github/workflows/run_giornali.yml) è solo manuale, usa Python 3.10 ed esegue `bot_giornali.py`.

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
| `MAX_CARATTERI_NOTIZIA` | `280` | Limite visibile per ogni notizia. |
| `USA_DOPPIA_VERIFICA` | `true` | Abilita la seconda verifica Gemini. |

Il workflow corrente non passa queste due variabili opzionali: per cambiarle in Actions occorre aggiungerle al blocco `env`.

## Bot web: Juventus Press News

### Fonti monitorate

Nell’esecuzione normale `juve_press_bot.py` raccoglie i contenuti pubblicati nella data italiana corrente.

| Gruppo | Fonti | Regola principale |
|---|---|---|
| Quotidiani | Tuttosport, Corriere dello Sport, La Gazzetta dello Sport | Sezioni o feed dedicati alla Juventus. |
| Altri siti | Sky Sport – Calciomercato, Juventus.com, Gianluca Di Marzio, Alfredo Pedullà, Borsa Italiana | Gianluca Di Marzio accetta solo titoli contenenti `Juventus`; le altre fonti applicano i rispettivi filtri per data e parola chiave. |
| YouTube | Fabrizio Romano in Italiano, Romeo Agresti | Tutti i video pubblicati nella data richiesta, letti dai feed Atom ufficiali dei canali. |
| X | 10 profili configurati | Lettura tramite mirror RSS pubblici, conversione dei collegamenti in URL `x.com` e filtri diversi per account. |

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

Per Sky il bot prova la pagina della data richiesta e, se non esiste ancora (`404`), usa come fallback la pagina del giorno precedente. Juventus.com viene letto attraverso il feed datato e la relativa paginazione.

Gli articoli vengono normalizzati, deduplicati e ordinati dal più vecchio al più recente. Il messaggio Telegram contiene fonte, titolo, eventuale sommario e link all’articolo. In caso di rate limit `429`, l’invio rispetta `retry_after` e prova fino a tre volte.

### Stato anti-duplicati

Gli identificativi notificati sono salvati in `.seen_juve_press_news.json` (massimo 2.000 elementi).

In GitHub Actions lo stato viene salvato nel repository come `.seen_juve_press_news.json`. Il workflow imposta `BASELINE_IF_NO_STATE=true`: se il file non esiste ancora, registra le notizie correnti senza inviarle, evitando una raffica al primo avvio. Dopo ogni esecuzione aggiorna il file con un commit, così lo stato resta disponibile anche nei run successivi.

### Workflow e configurazione

Il workflow [`.github/workflows/juve-press-news.yml`](.github/workflows/juve-press-news.yml):

- è avviabile solo manualmente;
- usa Python 3.12;
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
- Il monitoraggio X dipende dai mirror RSS pubblici configurati (`nitter.net` e `xcancel.com`): se entrambi sono indisponibili o cambiano formato, la categoria X viene saltata per quell’esecuzione.
- I feed YouTube includono tutti i video dei due canali configurati, senza un ulteriore filtro Juventus sul titolo.
- Entrambi i workflow sono manuali: il repository non contiene uno `schedule`.
- Lo stato del bot web vive nel file versionato `.seen_juve_press_news.json`; il workflow usa un gruppo di concorrenza per evitare esecuzioni sovrapposte.

---

Progetto amatoriale, non affiliato con Juventus FC, Telegram, Google, Dropbox o le fonti citate.
