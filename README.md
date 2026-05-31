# 📰 Notizie JR

> Bot Telegram per l'estrazione e pubblicazione automatica delle **notizie sulla Juventus** dai quotidiani sportivi — alimentato da AI e GitHub Actions.

---

## 📌 Panoramica

**Notizie JR** legge i PDF dei quotidiani sportivi italiani (Tuttosport, Gazzetta dello Sport, Corriere dello Sport) inviati direttamente al bot Telegram, estrae il testo, lo analizza con **Google Gemini** per isolare solo le notizie riguardanti la Juventus, e le pubblica sul canale **@Juventus_Reborn** con formattazione e fonte corretta.

---

## 🗂️ Struttura del repository

```
Notizie_JR/
├── bot.py                    # Script principale
├── requirements.txt          # Dipendenze Python
└── .github/workflows/
    └── run_bot.yml           # Workflow GitHub Actions
```

---

## ✨ Funzionalità

- **Ricezione PDF da Telegram** — i PDF dei giornali vengono inviati direttamente al bot; il bot li recupera tramite `getUpdates` e li scarica automaticamente (fino a 3 PDF per esecuzione)
- **Svuotamento coda automatico** — dopo ogni lettura, la coda di aggiornamenti Telegram viene resettata tramite offset per evitare di rielaborare i PDF già processati
- **Estrazione testo** — il testo viene estratto da ogni pagina del PDF con `pypdf`
- **Analisi AI con Gemini** — `gemini-3.5-flash` identifica e sintetizza solo le notizie relative alla Juventus, assegnando la fonte corretta a ogni notizia
- **Formattazione HTML** — nomi di giocatori, allenatori, dirigenti e squadre vengono evidenziati in grassetto `<b>`; ogni notizia è limitata a 280 caratteri
- **Tag fonte automatico** — ogni notizia riporta un'emoji e il nome del quotidiano di provenienza (`TuttoSport`, `Gazzetta dello Sport`, `Corriere dello Sport`)
- **Sessione HTTP robusta** — le richieste usano `urllib3.Retry` con backoff automatico su errori 5xx
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
| `TELEGRAM_TOKEN` | Token del bot Telegram (deve ricevere i PDF) |
| `CHAT_ID` | Chat ID del canale di destinazione per le notizie |
| `GEMINI_API_KEY` | Chiave API Google Gemini |

---

## 🚀 Utilizzo

1. Fai il **fork** del repository
2. Configura i secret elencati sopra
3. Invia i PDF dei quotidiani al bot Telegram (come documento)
4. Avvia il workflow da `Actions → Avvio Estrazione Notizie → Run workflow`

> Il bot legge i PDF presenti nella coda del bot Telegram al momento dell'esecuzione. Invia i giornali **prima** di avviare il workflow.

---

## 🛠️ Stack tecnico

`Python 3.10` · `pypdf` · `google-genai` · `requests` · `GitHub Actions`

---

## 🤖 Modello AI

[Google Gemini](https://ai.google.dev/) — modello `gemini-3.5-flash` per l'estrazione e sintesi delle notizie.

---

*Progetto amatoriale. Non affiliato con la Juventus FC, Telegram, Google o i quotidiani citati.*
