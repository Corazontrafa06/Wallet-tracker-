# Hyperliquid Wallet Tracker Bot (Telegram)

Bot Telegram multi-user untuk tracking posisi perpetual wallet di Hyperliquid secara real-time.

## Fitur

- Tambah / hapus wallet via command atau langsung kirim alamat `0x...`
- List wallet yang di-track
- Lihat posisi terbuka & ringkasan portfolio
- Alert otomatis saat **OPEN / CLOSE / ADD / REDUCE** posisi
- Multi-user (setiap Telegram user punya daftar sendiri)
- Data tersimpan di SQLite
- Siap deploy ke Railway

## Deploy ke Railway (Recommended)

### 1. Siapkan Bot Token
1. Buka [@BotFather](https://t.me/BotFather) di Telegram
2. Kirim `/newbot` → ikuti instruksi
3. Salin token yang diberikan (format: `123456789:AAH...`)

### 2. Deploy
1. Buka [railway.app](https://railway.app) → login dengan GitHub
2. **New Project** → **Deploy from GitHub repo** (atau upload folder ini)
3. Pilih repository yang berisi file-file bot ini
4. Setelah service tercipta, buka tab **Variables**
5. Tambahkan variable:
   ```
   BOT_TOKEN = 123456789:AAHxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
   ```
6. Klik **Deploy** / tunggu otomatis deploy

### 3. Cek Logs
Buka tab **Deployments** → **View Logs**.  
Kalau sukses akan muncul:
```
Bot starting…
Background tracker started (interval 20s)
```

### 4. Test Bot
Buka bot di Telegram → kirim `/start`

---

## Struktur File

```
.
├── hl_telegram_tracker.py   # Main bot
├── requirements.txt
├── Procfile
├── railway.toml
├── .gitignore
└── README.md
```

## Perintah Bot

| Command | Keterangan |
|---------|------------|
| `/start` | Welcome |
| `/help` | Bantuan lengkap |
| `/add 0x... [alias]` | Tambah wallet |
| `/remove 0x...` / alias | Hapus wallet |
| `/list` | Daftar wallet |
| `/positions` | Lihat posisi |
| `/summary` | Ringkasan semua wallet |
| `/settings` | Lihat pengaturan |
| `/setmin 200` | Filter min notional ($) |
| `/setinterval 25` | Interval polling (min 15s) |

Langsung kirim alamat `0x...` juga otomatis dianggap `/add`.

## Catatan Penting (SQLite di Railway)

Railway filesystem bersifat **ephemeral**.  
Artinya database `hl_tracker.db` akan **hilang** setiap kali service di-redeploy / restart.

**Solusi:**
- Untuk penggunaan pribadi ringan → biarkan saja (cukup `/add` ulang setelah redeploy)
- Untuk production → attach **Volume** di Railway:
  1. Service → **Settings** → **Volumes** → Add Volume
  2. Mount path: `/data`
  3. Tambah environment variable:
     ```
     DB_PATH = /data/hl_tracker.db
     ```
  4. Redeploy

## Local Development

```bash
pip install -r requirements.txt
export BOT_TOKEN="your_token_here"
python hl_telegram_tracker.py
```

## Support

Kalau ada error di logs Railway, kirim screenshot log-nya.  
Bot ini menggunakan long-polling (tidak butuh domain / webhook).
