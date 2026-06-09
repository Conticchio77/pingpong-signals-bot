# 🏓 PingPong Bet Bot

Bot Telegram per scommesse su partite di ping pong.  
Stack: **Python** · **python-telegram-bot v21** · **SQLite** · **Railway**

---

## 📋 Flow dell'applicazione

```
Utente → /start
         │
         ├─ Nuovo utente → creato con 100 crediti di benvenuto
         │
         └─ Menu principale (bottoni inline)
              ├─ 📋 Partite disponibili → lista partite aperte
              │        └─ Clicca partita → scegli giocatore
              │                    └─ Scrivi importo → scommessa piazzata
              ├─ 💰 Saldo
              ├─ 📊 Le mie scommesse
              └─ 🏆 Classifica

Admin → /aggiungi_partita <p1> <p2> <quota1> <quota2>
      → /chiudi_partita <match_id> <1|2>
      → /recredita <user_id> <importo>
```

---

## 📁 Struttura del progetto

```
pingpong-bot/
├── bot.py            # Entry point, handlers Telegram
├── betting.py        # Logica scommesse e liquidazione
├── database.py       # Layer SQLite (users, matches, bets)
├── requirements.txt  # Dipendenze Python
├── Procfile          # Per Railway/Heroku
├── railway.toml      # Config deploy Railway
├── .env.example      # Variabili d'ambiente (template)
└── .gitignore
```

---

## ⚙️ Variabili d'ambiente

| Variabile | Descrizione |
|-----------|-------------|
| `TELEGRAM_BOT_TOKEN` | Token del bot (da @BotFather) |
| `ADMIN_IDS` | ID Telegram admin, separati da virgola |
| `DB_PATH` | Percorso file SQLite (default: `pingpong.db`) |

---

## 🚀 Deploy su Railway (passo per passo)

### 1. Crea il bot su Telegram
1. Apri Telegram e cerca **@BotFather**
2. Scrivi `/newbot` e segui le istruzioni
3. Copia il **token** (es. `123456789:AAxxxx...`)
4. Per trovare il tuo **ADMIN_ID** scrivi a **@userinfobot**

### 2. Carica su GitHub
```bash
git init
git add .
git commit -m "Initial commit"
git remote add origin https://github.com/TUO_USERNAME/pingpong-bot.git
git push -u origin main
```

### 3. Deploya su Railway
1. Vai su [railway.app](https://railway.app) e registrati
2. **New Project** → **Deploy from GitHub Repo**
3. Seleziona il repo `pingpong-bot`
4. Vai su **Variables** e aggiungi:
   - `TELEGRAM_BOT_TOKEN` = il tuo token
   - `ADMIN_IDS` = il tuo Telegram ID
5. Railway detecta il `Procfile` e avvia automaticamente il bot

> ⚠️ **Database persistente su Railway**: il filesystem è effimero.  
> Per dati persistenti aggiungi un **Volume** in Railway e imposta `DB_PATH=/data/pingpong.db`,  
> oppure migra su PostgreSQL (vedi sezione avanzata).

---

## 🎮 Comandi disponibili

### Utenti
| Comando | Descrizione |
|---------|-------------|
| `/start` | Menu principale + saldo |
| `/partite` | Lista partite aperte |
| `/saldo` | Mostra il tuo saldo |
| `/mybets` | Ultimi 10 scommesse personali |
| `/classifica` | Top 10 utenti per saldo |

### Admin
| Comando | Esempio |
|---------|---------|
| `/aggiungi_partita <p1> <p2> <q1> <q2>` | `/aggiungi_partita Rossi Bianchi 1.75 2.10` |
| `/chiudi_partita <id> <1\|2>` | `/chiudi_partita 3 1` (vince giocatore 1) |
| `/recredita <user_id> <importo>` | `/recredita 123456789 50` |

---

## 💻 Sviluppo locale

```bash
# Clona il repo
git clone https://github.com/TUO_USERNAME/pingpong-bot.git
cd pingpong-bot

# Crea virtual environment
python -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate

# Installa dipendenze
pip install -r requirements.txt

# Crea il file .env
cp .env.example .env
# → modifica .env con il tuo token e admin ID

# Avvia il bot
python bot.py
```

---

## 🗄️ Schema del database

```sql
users   (id, username, balance)
matches (id, player1, player2, odds1, odds2, status, winner)
bets    (id, user_id, match_id, player_num, amount, odds, status)
```

**Status partita:** `open` → `closed`  
**Status scommessa:** `pending` → `won` / `lost`

---

## 📈 Esempio di sessione

```
Admin:  /aggiungi_partita MaLong FanZhendong 1.65 2.30
Bot:    ✅ Partita aggiunta! ID: 1

Utente: /start → clicca "Partite disponibili"
        → clicca "MaLong vs FanZhendong"
        → clicca "🟢 MaLong (x1.65)"
        → scrive: 100
Bot:    ✅ Scommessa piazzata! Vincita potenziale: 165.00 crediti

Admin:  /chiudi_partita 1 1
Bot:    ✅ Partita chiusa. Vince giocatore 1.
        💸 Scommesse risolte: 1 | Pagati: 165.00 crediti
```
