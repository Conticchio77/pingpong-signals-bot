# 🏓 PingPong Signals Bot

Bot Telegram per segnali automatici su scommesse di ping pong.  
Genera segnali di tipo **Winner, Over/Under set, Handicap, Risultato esatto** con analisi AI.

---

## 📋 Flow completo

```
Boot
 └─ Scan automatico ogni ora
      │
      ├─ SofaScore API → partite live/programmate di ping pong
      │       └─ fallback: dataset top player mondiali
      │
      ├─ AI Analyzer (Claude API o euristica)
      │       └─ calcola value edge, Kelly stake, confidenza
      │
      └─ Segnale trovato (confidenza ≥ 60%, value ≥ 5%)
              │
              ├─ Notifica ADMIN con tasti:
              │       ├─ [📤 Invia al VIP ✅]  → pubblica nel canale VIP
              │       └─ [🗑 Scarta]            → archivia
              │
              └─ (opzionale) Auto-invio VIP attivabile dalle impostazioni
```

---

## 📁 Struttura

```
pingpong-signals-bot/
├── bot.py           # Entry point, handlers, scheduler
├── scraper.py       # Fetching partite da SofaScore + fallback
├── ai_analyzer.py   # Analisi AI (Claude API) + euristica
├── database.py      # SQLite: segnali, impostazioni, stats
├── requirements.txt
├── Procfile
├── railway.toml
├── .env.example
└── .gitignore
```

---

## ⚙️ Variabili d'ambiente

| Variabile | Obbligatoria | Descrizione |
|-----------|:---:|-------------|
| `TELEGRAM_BOT_TOKEN` | ✅ | Token da @BotFather |
| `ADMIN_ID` | ✅ | Tuo Telegram ID (858001417) |
| `VIP_GROUP_ID` | ✅ | ID canale VIP (-1002950341972) |
| `ANTHROPIC_API_KEY` | ❌ | Per analisi AI avanzata (fallback automatico senza) |
| `DB_PATH` | ❌ | Percorso SQLite (default: `signals.db`) |

---

## 🚀 Deploy su Railway

### 1. Crea il bot su Telegram
- @BotFather → `/newbot` → copia il token
- **⚠️ Revoca e rigenera il token se lo hai già condiviso in chat**

### 2. Aggiungi il bot al canale VIP come amministratore
- Apri il canale VIP su Telegram
- Impostazioni → Amministratori → Aggiungi amministratore
- Cerca il tuo bot e aggiungilo con permesso di **Pubblicare messaggi**

### 3. Carica su GitHub
```bash
git init
git add .
git commit -m "PingPong Signals Bot"
git remote add origin https://github.com/TUO_USER/pingpong-signals-bot.git
git push -u origin main
```

### 4. Configura Railway
1. [railway.app](https://railway.app) → **New Project** → **Deploy from GitHub Repo**
2. Seleziona il repo
3. **Variables** → aggiungi tutte le variabili da `.env.example`
4. (Consigliato) **New Volume** → mount path `/data` → imposta `DB_PATH=/data/signals.db`
5. Il bot si avvia automaticamente ✅

---

## 🎮 Pannello Admin

Scrivi `/start` al bot per aprire il pannello admin con:

| Tasto | Funzione |
|-------|----------|
| 🔍 Cerca segnali ora | Scan manuale immediato |
| 📋 Segnali pendenti | Lista segnali da approvare/scartare |
| 📊 Statistiche | Win rate, ROI, contatori |
| ⚙️ Impostazioni | Intervallo scan, auto-invio VIP, min confidenza |

---

## 🏓 Tipi di segnali generati

| Tipo | Esempio |
|------|---------|
| 🏆 Winner | Vince Fan Zhendong @ 1.85 |
| 📈 Over set | Over 3.5 set @ 1.90 |
| 📉 Under set | Under 3.5 set @ 1.75 |
| ⚖️ Handicap | Ma Long -1.5 set @ 2.10 |
| 🎯 Risultato esatto | Fan Zhendong 3-1 @ 3.20 |

---

## 📊 Esempio segnale ricevuto

```
🆕 Nuovo segnale trovato!

🏓 SEGNALE PING PONG
──────────────────────
🏆 Fan Zhendong vs Wang Chuqin
🎯 Giocata: Vince Fan Zhendong
💰 Quota: 1.85
📊 Confidenza: 76% ⭐⭐⭐⭐
💡 Value edge: +14.2%
📌 Stake: 3/5
⏰ Inizio: 14/06 18:30
🌍 Torneo: WTT Champions

📝 Fan Zhendong dominante nelle ultime 8 partite WTT
```

---

## 🔧 Note tecniche

- **SofaScore**: API pubblica non ufficiale, nessuna chiave necessaria
- **Fallback**: se SofaScore non risponde, usa dataset interno di top 12 player mondiali
- **AI**: con `ANTHROPIC_API_KEY` usa Claude per analisi reale; senza, usa l'analisi euristica (Kelly Criterion + probabilità implicite)
- **Database**: SQLite locale; su Railway usa un Volume per la persistenza
