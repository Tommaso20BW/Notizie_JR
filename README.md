# 📰 Notizie JR
> Bot Telegram per l'estrazione e pubblicazione automatica delle **notizie sulla Juventus** dai quotidiani sportivi — alimentato da AI e GitHub Actions.

---

## 📌 Panoramica
**Notizie JR** legge i PDF dei quotidiani sportivi italiani (Tuttosport, Gazzetta dello Sport, Corriere dello Sport) caricati su **Dropbox** e li invia direttamente a **Google Gemini**, che li analizza per isolare solo le notizie riguardanti la Juventus e le pubblica sul canale **@Juventus_Reborn** con formattazione e fonte corretta. Dopo l'elaborazione, i PDF vengono cancellati automaticamente da Dropbox.

---

## 🗂️ Struttura del repository
```
Notizie_JR/
├── bot_giornali.py           # Script principale
├── requirements.txt          # Dipendenze Python
└── .github/workflows/
    └── run_giornali.yml      # Workflow GitHub Actions
```

---

## ✨ Funzionalità
- **Ricezione PDF da Dropbox** — i PDF dei giornali vengono caricati in una cartella Dropbox dedicata (`NotizieJR`); il bot li scarica automaticamente senza limiti di dimensione
- **Cancellazione automatica** — dopo l'elaborazione, i PDF vengono cancellati da Dropbox; la cartella è sempre pulita per il giorno successivo
- **Lettura PDF con AI** — il PDF viene inviato direttamente a **Google Gemini**, che lo legge da solo. Funziona anche con i PDF scansionati (foto delle pagine), perché Gemini è multimodale e "vede" anche le immagini, non solo il testo
- **Analisi AI con Gemini** — `gemini-3.5-flash` identifica e sintetizza solo le notizie relative alla Juventus, assegnando la fonte corretta a ogni notizia
- **Formattazione HTML** — nomi di giocatori, allenatori, dirigenti e squadre vengono evidenziati in grassetto `<b>`; ogni notizia è limitata a 280 caratteri
- **Tag fonte automatico** — ogni notizia riporta un'emoji e il nome del quotidiano di provenienza (`TuttoSport`, `Gazzetta dello Sport`, `Corriere dello Sport`)
- **Pausa tra giornali** — attesa di 20 secondi tra l'elaborazione di un quotidiano e il successivo per non sovraccaricare l'API Gemini

---

## 📐 Formato del messaggio
```
[Emoji notizia] Testo della notizia con Giocatore in grassetto...
📰 TuttoSport
👉 @Juventus_Reborn
```

---

## ⚙️ Configurazione dei Secrets
Aggiungi i seguenti secret nelle impostazioni della repository (`Settings → Secrets and variables → Actions`):

| Secret | Descrizione |
|---|---|
| `TELEGRAM_TOKEN` | Token del bot Telegram |
| `CHAT_ID` | Chat ID del canale di destinazione per le notizie |
| `GEMINI_API_KEY` | Chiave API Google Gemini |
| `DROPBOX_APP_KEY` | App key dell'app Dropbox |
| `DROPBOX_APP_SECRET` | App secret dell'app Dropbox |
| `DROPBOX_REFRESH_TOKEN` | Refresh token OAuth2 Dropbox (non scade) |

---

## 🚀 Utilizzo
1. Fai il **fork** del repository
2. Configura i secret elencati sopra
3. Crea una cartella chiamata `NotizieJR` su Dropbox e condividila con il service account
4. Carica i PDF dei quotidiani nella cartella `NotizieJR` su Dropbox
5. Avvia il workflow da `Actions → Avvio Estrazione Notizie - Giornali → Run workflow`

> Carica i PDF su Dropbox **prima** di avviare il workflow. Dopo l'elaborazione verranno cancellati automaticamente.

---

## 🛠️ Stack tecnico
`Python 3.10` · `google-genai` · `dropbox` · `requests` · `GitHub Actions`

---

## 🤖 Modello AI
[Google Gemini](https://ai.google.dev/) — modello `gemini-3.5-flash` per la lettura dei PDF e la sintesi delle notizie.

---

*Progetto amatoriale. Non affiliato con la Juventus FC, Telegram, Google, Dropbox o i quotidiani citati.*
