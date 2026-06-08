<div align="center">

# 📰 Notizie JR

**Bot Telegram che estrae e pubblica in automatico le notizie sulla Juventus dai quotidiani sportivi.**

Legge i PDF dei giornali, isola le notizie bianconere con l’AI e le pubblica su Telegram — senza server, senza database.

`Python 3.10` · `Google Gemini` · `Dropbox API` · `Telegram Bot API` · `GitHub Actions`

</div>

-----

## Indice

- [Cos’è](#cosè)
- [Come funziona](#come-funziona)
- [Funzionalità](#funzionalità)
- [Formato del messaggio](#formato-del-messaggio)
- [Struttura del repository](#struttura-del-repository)
- [Configurazione](#configurazione)
- [Avvio](#avvio)
- [Stack tecnico](#stack-tecnico)
- [Modello AI](#modello-ai)

-----

## Cos’è

Notizie JR legge i PDF dei quotidiani sportivi italiani (Tuttosport, Gazzetta dello Sport, Corriere dello Sport) caricati su **Dropbox** e li invia direttamente a **Google Gemini**, che li analizza per isolare solo le notizie riguardanti la Juventus e le pubblica sul canale **@Juventus_Reborn** con formattazione e fonte corretta. Dopo l’elaborazione, i PDF vengono cancellati automaticamente da Dropbox.

-----

## Come funziona

```
                ┌──────────────────────┐
                │   GitHub Actions      │  ← avvio manuale (workflow_dispatch)
                │   run_giornali.yml    │
                └──────────┬───────────┘
                           │
                           ▼
        ┌──────────────────────────────────────┐
        │            bot_giornali.py            │
        │  1. scarica i PDF da Dropbox          │
        │  2. invia ogni PDF a Gemini           │
        │  3. pubblica solo le notizie Juve     │
        │  4. cancella i PDF da Dropbox         │
        └───┬────────────┬───────────┬──────────┘
            │            │           │
            ▼            ▼           ▼
        ┌─────────┐  ┌────────┐  ┌──────────┐
        │ Dropbox │  │ Gemini │  │ Telegram │
        │  (PDF)  │  │  (AI)  │  │ (output) │
        └─────────┘  └────────┘  └──────────┘
```

1. **Ricezione** — i PDF dei giornali vengono caricati nella cartella Dropbox `NotizieJR`; il bot li scarica automaticamente, senza limiti di dimensione.
1. **Lettura AI** — ogni PDF viene inviato direttamente a Gemini, che lo legge da solo. Funziona anche con i PDF scansionati (foto delle pagine), perché Gemini è multimodale e “vede” anche le immagini, non solo il testo.
1. **Analisi** — `gemini-3.5-flash` identifica e sintetizza solo le notizie relative alla Juventus, assegnando a ciascuna la fonte corretta.
1. **Pulizia** — a elaborazione conclusa i PDF vengono cancellati da Dropbox, così la cartella è sempre pronta per il giorno dopo.

-----

## Funzionalità

- **Ricezione PDF da Dropbox** — i PDF dei giornali vengono caricati in una cartella Dropbox dedicata (`NotizieJR`); il bot li scarica automaticamente senza limiti di dimensione.
- **Cancellazione automatica** — dopo l’elaborazione, i PDF vengono cancellati da Dropbox; la cartella è sempre pulita per il giorno successivo.
- **Lettura PDF con AI** — il PDF viene inviato direttamente a Google Gemini, che lo legge da solo. Funziona anche con i PDF scansionati, perché Gemini è multimodale e “vede” anche le immagini.
- **Analisi AI con Gemini** — `gemini-3.5-flash` identifica e sintetizza solo le notizie relative alla Juventus, assegnando la fonte corretta a ogni notizia.
- **Formattazione HTML** — nomi di giocatori, allenatori, dirigenti e squadre vengono evidenziati in grassetto `<b>`; ogni notizia è limitata a 280 caratteri.
- **Tag fonte automatico** — ogni notizia riporta un’emoji e il nome del quotidiano di provenienza (`TuttoSport`, `Gazzetta dello Sport`, `Corriere dello Sport`).
- **Pausa tra giornali** — attesa di 20 secondi tra l’elaborazione di un quotidiano e il successivo per non sovraccaricare l’API Gemini.

-----

## Formato del messaggio

```
[Emoji notizia] Testo della notizia con Giocatore in grassetto...
📰 TuttoSport
👉 @Juventus_Reborn
```

-----

## Struttura del repository

```
Notizie_JR/
├── bot_giornali.py           # Script principale
├── requirements.txt          # Dipendenze Python
└── .github/workflows/
    └── run_giornali.yml      # Workflow GitHub Actions
```

-----

## Configurazione

In **Settings → Secrets and variables → Actions** aggiungi:

|Secret                 |Descrizione                                       |
|-----------------------|--------------------------------------------------|
|`TELEGRAM_TOKEN`       |Token del bot Telegram.                           |
|`CHAT_ID`              |Chat ID del canale di destinazione per le notizie.|
|`GEMINI_API_KEY`       |Chiave API Google Gemini.                         |
|`DROPBOX_APP_KEY`      |App key dell’app Dropbox.                         |
|`DROPBOX_APP_SECRET`   |App secret dell’app Dropbox.                      |
|`DROPBOX_REFRESH_TOKEN`|Refresh token OAuth2 Dropbox (non scade).         |

-----

## Avvio

1. Fai il **fork** del repository.
1. Configura i secret elencati sopra.
1. Crea una cartella chiamata `NotizieJR` su Dropbox e condividila con il service account.
1. Carica i PDF dei quotidiani nella cartella `NotizieJR` su Dropbox.
1. Avvia il workflow da `Actions → Avvio Estrazione Notizie - Giornali → Run workflow`.

> Carica i PDF su Dropbox **prima** di avviare il workflow. Dopo l’elaborazione verranno cancellati automaticamente.

-----

## Stack tecnico

`Python 3.10` · `google-genai` · `dropbox` · `requests` · `GitHub Actions`

-----

## Modello AI

[Google Gemini](https://ai.google.dev/) — modello `gemini-3.5-flash` per la lettura dei PDF e la sintesi delle notizie.

-----

<div align="center">

*Progetto amatoriale. Non affiliato con la Juventus FC, Telegram, Google, Dropbox o i quotidiani citati.*

</div>