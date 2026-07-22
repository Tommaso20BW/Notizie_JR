# 📰 Notizie JR

Repository con **due bot Telegram distinti** per le notizie sulla Juventus:

1. `bot_giornali.py` legge i PDF dei quotidiani da Dropbox e usa Gemini per estrarre notizie verificate;
2. `juve_press_bot.py` monitora otto fonti web e segnala soltanto gli articoli pubblicati oggi e non ancora notificati.

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
cancellazione da Dropbox solo se tutto è riuscito
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

Tra due giornali attende 20 secondi. Il PDF originale viene cancellato da Dropbox solo se l’elaborazione è conclusa e tutti gli invii Telegram sono riusciti; anche un PDF senza notizie Juventus viene considerato elaborato correttamente.

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

`juve_press_bot.py` raccoglie le notizie pubblicate nella data italiana corrente da:

- Tuttosport;
- Corriere dello Sport;
- La Gazzetta dello Sport;
- Sky Sport – Calciomercato;
- Juventus.com;
- Gianluca Di Marzio;
- Alfredo Pedullà;
- Borsa Italiana.

Per Sky, Di Marzio, Pedullà e Borsa Italiana vengono applicati filtri espliciti su `Juve`/`Juventus` (con esclusione di `Juve Stabia`). La pagina ufficiale Juventus è già specifica del club; Tuttosport, Corriere e Gazzetta usano sezioni o feed dedicati.

Gli articoli vengono normalizzati, deduplicati e ordinati dal più vecchio al più recente. Il messaggio Telegram contiene fonte, titolo, eventuale sommario e link all’articolo. In caso di rate limit `429`, l’invio rispetta `retry_after` e prova fino a tre volte.

### Stato anti-duplicati

Gli identificativi notificati sono salvati in `.seen_juve_press_news.json` (massimo 2.000 elementi).

In GitHub Actions lo stato viene conservato con `actions/cache`. Il workflow imposta `BASELINE_IF_NO_STATE=true`: se non esiste ancora una cache, registra le notizie correnti senza inviarle, evitando una raffica al primo avvio. Ogni articolo viene salvato nello stato subito dopo l’invio riuscito.

### Workflow e configurazione

Il workflow [`.github/workflows/juve-press-news.yml`](.github/workflows/juve-press-news.yml):

- è avviabile solo manualmente;
- usa Python 3.12;
- ripristina e salva lo stato con Actions Cache;
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
- Entrambi i workflow sono manuali: il repository non contiene uno `schedule`.
- Lo stato del bot web vive nella cache di GitHub Actions, non in un database o in un file versionato.

---

Progetto amatoriale, non affiliato con Juventus FC, Telegram, Google, Dropbox o le fonti citate.
