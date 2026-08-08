<div align="center">

# 📰 Notizie JR

**Due bot Telegram per le notizie Juventus: rassegna PDF con Gemini e monitoraggio delle fonti web.**

[![Python 3.14](https://img.shields.io/badge/Python-3.14-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Notizie web](https://github.com/Tommaso20BW/Notizie_JR/actions/workflows/juve-press-news.yml/badge.svg)](https://github.com/Tommaso20BW/Notizie_JR/actions/workflows/juve-press-news.yml)
[![Quotidiani PDF](https://github.com/Tommaso20BW/Notizie_JR/actions/workflows/run_giornali.yml/badge.svg)](https://github.com/Tommaso20BW/Notizie_JR/actions/workflows/run_giornali.yml)

</div>

## Panoramica

Il repository contiene due flussi separati, con dipendenze, workflow e stato indipendenti.

| Bot | Punto di ingresso | Sorgenti | Output |
| --- | --- | --- | --- |
| Quotidiani PDF | `bot_giornali.py` | PDF presenti in Dropbox | Notizie verificate ed estratte con Gemini |
| Notizie web | `juve_press_bot.py` | Siti, feed YouTube e profili X | Articoli e post della data richiesta |

## Quotidiani PDF

### Flusso

```text
Dropbox /NotizieJR
        ↓
download dei PDF
        ↓
Gemini: estrazione JSON
        ↓
validazione deterministica
        ↓
Telegram
        ↓
rimozione del PDF dopo una lettura riuscita
```

`bot_giornali.py`:

- elenca tutti i PDF nella cartella Dropbox `/NotizieJR`;
- riconosce Tuttosport, La Gazzetta dello Sport e Corriere dello Sport dal nome del file quando possibile;
- carica ogni documento su Gemini e richiede notizie Juventus in JSON strutturato;
- pretende per ogni notizia fonte, pagina e un breve riscontro testuale tratto dal PDF;
- normalizza importi in milioni di euro, elimina duplicati e rimuove markup non consentito;
- divide localmente i testi oltre 3.800 caratteri, senza una nuova richiesta al modello;
- invia le parti come una catena di risposte Telegram;
- attende 20 secondi prima di elaborare il giornale successivo.

La catena Gemini predefinita è:

1. `gemini-3.5-flash-lite`;
2. `gemini-3.1-flash-lite`;
3. `gemini-3.6-flash`;
4. `gemini-3.5-flash`.

In caso di limiti `429` o errori temporanei `5xx`, il bot passa al modello successivo e può ripetere l'intero ciclo. I modelli con quota giornaliera esaurita vengono esclusi dai tentativi successivi.

> [!NOTE]
> Il PDF viene rimosso da Dropbox dopo una lettura Gemini completata, anche se non contiene notizie o se l'invio Telegram resta parziale. Se download o lettura falliscono, il file resta disponibile per il run seguente.

### Configurazione PDF

| Variabile o secret | Obbligatoria | Uso |
| --- | ---: | --- |
| `TELEGRAM_TOKEN` | sì | Token del bot Telegram |
| `CHAT_ID` | sì | Chat o canale di destinazione |
| `GEMINI_API_KEY` | sì | Accesso ai modelli Gemini |
| `DROPBOX_APP_KEY` | sì | App key Dropbox |
| `DROPBOX_APP_SECRET` | sì | App secret Dropbox |
| `DROPBOX_REFRESH_TOKEN` | sì | Refresh token OAuth2 Dropbox |
| `MAX_CARATTERI_NOTIZIA` | no | Lunghezza visibile di ogni parte; default `3800` |
| `USA_DOPPIA_VERIFICA` | no | Seconda lettura Gemini; default `false` |
| `MAX_CICLI_GEMINI` | no | Cicli completi sui modelli; default `3` |
| `ATTESA_503_GEMINI` | no | Attesa iniziale tra i cicli; default `20` secondi |

Il workflow imposta esplicitamente `USA_DOPPIA_VERIFICA=false`.

## Notizie web

### Fonti monitorate

Il bot seleziona normalmente i contenuti pubblicati nella data italiana corrente.

| Gruppo | Fonti | Regole principali |
| --- | --- | --- |
| Quotidiani | Tuttosport, Corriere dello Sport, La Gazzetta dello Sport | Sezioni Juventus, deduplicazione e filtro per data |
| Sky Sport | Calciomercato del giorno e pagina Juventus | Esclude recap generici, titoli con `video` e riferimenti alla Juve Stabia |
| Siti | Juventus.com, Gianluca Di Marzio, Alfredo Pedullà, Borsa Italiana | Feed o pagine dedicate; Di Marzio richiede Juve/Juventus nel titolo |
| YouTube | Juventus, Fabrizio Romano in Italiano, Romeo Agresti | Tutti i video della data richiesta dai feed Atom ufficiali |
| X | 11 profili | Feed RSS pubblici, filtro Juventus dove configurato, nessun repost |

I profili X sono:

| Profilo | Contenuti accettati |
| --- | --- |
| `@juventusfc` | Tutti i post originali |
| `@Glongari` | Solo post che citano Juve o Juventus |
| `@romeoagresti` | Tutti i post originali |
| `@NicoSchira` | Solo post che citano Juve o Juventus |
| `@AlfredoPedulla` | Solo post che citano Juve o Juventus |
| `@MatteMoretto` | Solo post che citano Juve o Juventus |
| `@FabrizioRomano` | Solo post che citano Juve o Juventus |
| `@DiMarzio` | Solo post che citano Juve o Juventus |
| `@_Morik92_` | Tutti i post originali |
| `@ilbianconerocom` | Tutti i post originali |
| `@BaridonMarco` | Tutti i post originali |

I link dei mirror vengono convertiti in URL `x.com`. Nel testo vengono rimossi i simboli `#` e `@`, mentre gli hashtag CamelCase vengono separati in parole leggibili.

### Media e invio Telegram

Per ogni contenuto il bot prova a recuperare le anteprime da feed RSS, YouTube, Open Graph o Twitter Card.

- Le foto vengono inviate con `sendPhoto`; più immagini possono formare un album.
- Per i video nativi X, FxTwitter e VxTwitter forniscono l'MP4 quando disponibile.
- FFmpeg verifica le tracce e aggiunge audio silenzioso ai video muti, evitando che Telegram li mostri come GIF.
- Le GIF animate di X usano soltanto una copertina statica.
- Se un media non è disponibile o viene rifiutato, il bot ripiega su foto, copertina o messaggio testuale.
- Il client Telegram gestisce errori di rete, rate limit `429` e risposte temporanee `5xx`.

Gli articoli vengono deduplicati e ordinati dal più vecchio al più recente. Lo stato viene aggiornato soltanto dopo una risposta Telegram valida contenente il `message_id`.

### Stato anti-duplicati

| File | Ruolo |
| --- | --- |
| `.seen_juve_press_news.json` | Chiavi già inviate e data di riferimento |
| `.pending_juve_press_news.json` | Journal delle notizie scoperte ma non ancora confermate da Telegram |

Al cambio di giorno lo stato degli elementi inviati viene azzerato. Ogni notizia scoperta viene registrata subito nel journal; dopo l'invio confermato passa nello stato `seen` e viene rimossa dai pending.

Il workflow usa `BASELINE_IF_NO_STATE=true`: se lo stato non esiste, registra i contenuti correnti senza inviarli, evitando una raffica al primo avvio. Al termine committa entrambi i file di stato quando cambiano.

### Configurazione web

| Secret | Uso |
| --- | --- |
| `TELEGRAM_TOKEN` | Token del bot Telegram |
| `CHAT_ID` | Chat o canale di destinazione |

Non servono chiavi API per le fonti web, YouTube o X.

## Struttura

```text
Notizie_JR/
├── bot_giornali.py
├── juve_press_bot.py
├── telegram_notifier.py
├── article_journal.py
├── preview_image.py
├── video_media.py
├── requirements.txt
├── requirements-juve-press.txt
├── tests/
└── .github/workflows/
    ├── run_giornali.yml
    └── juve-press-news.yml
```

## Requisiti

Entrambi i workflow usano Python 3.14.

Per il bot PDF:

```bash
python -m pip install -r requirements.txt
```

Per il bot web:

```bash
python -m pip install -r requirements-juve-press.txt
```

## Avvio locale

Bot PDF:

```bash
python bot_giornali.py
```

Bot web con invio reale:

```bash
python juve_press_bot.py
```

Raccolta senza stato e senza Telegram:

```bash
python juve_press_bot.py --dry-run
python juve_press_bot.py --dry-run --preview-messages
```

Per includere anche il giorno precedente durante i test:

```bash
python juve_press_bot.py --dry-run --include-yesterday
```

`--preview-messages` richiede `--dry-run`. `--include-yesterday` non è usato dal workflow e, senza `--dry-run`, può inviare contenuti di ieri non ancora presenti nello stato.

## Test

Dopo aver installato entrambi i file di dipendenze:

```bash
python -m unittest discover -s tests -v
```

Il workflow web esegue tutti i test dedicati al monitoraggio online e salta `test_bot_giornali.py`; il workflow PDF non esegue attualmente una fase di test.

## GitHub Actions

| Workflow | Comportamento |
| --- | --- |
| `run_giornali.yml` | Avvio manuale del bot PDF, con permessi di sola lettura sul contenuto del repository |
| `juve-press-news.yml` | Blocca run concorrenti, testa il bot web, controlla le notizie e salva lo stato |

Entrambi usano Python 3.14, sono avviabili soltanto con `workflow_dispatch` ed eliminano i propri run completati dalla cronologia. Nel repository non è configurato uno `schedule`.

## Limiti noti

- L'estrazione PDF dipende dalla qualità del documento e dall'output di Gemini; i controlli riducono, ma non eliminano, possibili errori.
- Selettori HTML, feed e endpoint non documentati possono cambiare senza preavviso.
- Il monitoraggio X dipende dalla disponibilità dei mirror RSS pubblici e, per i video, da FxTwitter o VxTwitter.
- I feed YouTube non applicano un ulteriore filtro Juventus al titolo dei tre canali configurati.
- Il bot PDF elimina un documento dopo una lettura riuscita anche se Telegram non consegna tutte le notizie.

---

Progetto amatoriale, non affiliato con Juventus Football Club, Telegram, Google, Dropbox o le fonti citate.
