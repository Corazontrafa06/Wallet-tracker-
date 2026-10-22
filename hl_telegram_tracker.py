#!/usr/bin/env python3
"""
Hyperliquid Wallet Tracker Bot (Telegram)
=========================================
Multi-user • Real-time position alerts • Persistent SQLite storage

Fitur:
- /add <wallet> [alias]     → Tambah wallet untuk di-track
- /remove <wallet|alias>    → Hapus wallet
- /list                     → Lihat daftar wallet yang di-track
- /positions [wallet]       → Lihat posisi terbuka
- /summary                  → Ringkasan semua wallet
- /settings                 → Atur min notional & interval
- Alert otomatis saat open / close / add / reduce posisi

Install:
    pip install "python-telegram-bot[job-queue]" aiohttp aiosqlite

Jalankan:
    export BOT_TOKEN="your_telegram_bot_token"
    python hl_telegram_tracker.py
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import aiohttp
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ====================== CONFIG ======================
BOT_TOKEN = os.getenv("BOT_TOKEN", "")  # Wajib diisi / set via env
API_URL = "https://api.hyperliquid.xyz/info"
DB_PATH = os.getenv("DB_PATH", "hl_tracker.db")  # Railway Volume: set ke /data/hl_tracker.db

DEFAULT_POLL_INTERVAL = 20          # detik
DEFAULT_MIN_NOTIONAL = 100          # USD – abaikan posisi kecil
MAX_WALLETS_PER_USER = 15
REQUEST_TIMEOUT = 12

# ====================================================

logging.basicConfig(
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("HLTracker")

# -------------------- Database --------------------

def init_db() -> None:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                chat_id     INTEGER PRIMARY KEY,
                min_notional REAL DEFAULT 100,
                poll_interval INTEGER DEFAULT 20,
                created_at  TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS wallets (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id     INTEGER NOT NULL,
                address     TEXT NOT NULL,
                alias       TEXT,
                added_at    TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(chat_id, address),
                FOREIGN KEY (chat_id) REFERENCES users(chat_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS position_snapshots (
                chat_id     INTEGER NOT NULL,
                address     TEXT NOT NULL,
                snapshot    TEXT NOT NULL,   -- JSON string of positions
                updated_at  TEXT DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (chat_id, address)
            );

            CREATE INDEX IF NOT EXISTS idx_wallets_chat ON wallets(chat_id);
            """
        )
        conn.commit()


def ensure_user(chat_id: int) -> None:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO users (chat_id) VALUES (?)", (chat_id,)
        )
        conn.commit()


def get_user_settings(chat_id: int) -> Tuple[float, int]:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        row = conn.execute(
            "SELECT min_notional, poll_interval FROM users WHERE chat_id = ?",
            (chat_id,),
        ).fetchone()
        if row:
            return float(row[0]), int(row[1])
        return DEFAULT_MIN_NOTIONAL, DEFAULT_POLL_INTERVAL


def set_user_setting(chat_id: int, key: str, value: float | int) -> None:
    ensure_user(chat_id)
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(f"UPDATE users SET {key} = ? WHERE chat_id = ?", (value, chat_id))
        conn.commit()


def add_wallet(chat_id: int, address: str, alias: Optional[str] = None) -> Tuple[bool, str]:
    ensure_user(chat_id)
    address = address.lower()
    with closing(sqlite3.connect(DB_PATH)) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM wallets WHERE chat_id = ?", (chat_id,)
        ).fetchone()[0]
        if count >= MAX_WALLETS_PER_USER:
            return False, f"Maksimal {MAX_WALLETS_PER_USER} wallet per user."

        exists = conn.execute(
            "SELECT 1 FROM wallets WHERE chat_id = ? AND address = ?",
            (chat_id, address),
        ).fetchone()
        if exists:
            return False, "Wallet sudah ada di daftar tracking."

        conn.execute(
            "INSERT INTO wallets (chat_id, address, alias) VALUES (?, ?, ?)",
            (chat_id, address, alias),
        )
        conn.commit()
    return True, "OK"


def remove_wallet(chat_id: int, query: str) -> Tuple[bool, str]:
    query = query.strip().lower()
    with closing(sqlite3.connect(DB_PATH)) as conn:
        # Coba by address dulu
        cur = conn.execute(
            "DELETE FROM wallets WHERE chat_id = ? AND address = ?",
            (chat_id, query),
        )
        if cur.rowcount:
            conn.execute(
                "DELETE FROM position_snapshots WHERE chat_id = ? AND address = ?",
                (chat_id, query),
            )
            conn.commit()
            return True, "Wallet dihapus."

        # Coba by alias
        cur = conn.execute(
            "DELETE FROM wallets WHERE chat_id = ? AND LOWER(alias) = ?",
            (chat_id, query),
        )
        if cur.rowcount:
            conn.commit()
            return True, "Wallet dihapus (via alias)."

        return False, "Wallet / alias tidak ditemukan."


def list_wallets(chat_id: int) -> List[Tuple[str, Optional[str]]]:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        rows = conn.execute(
            "SELECT address, alias FROM wallets WHERE chat_id = ? ORDER BY added_at",
            (chat_id,),
        ).fetchall()
    return [(r[0], r[1]) for r in rows]


def get_all_tracked() -> List[Tuple[int, str, Optional[str]]]:
    """Return list of (chat_id, address, alias) for all users."""
    with closing(sqlite3.connect(DB_PATH)) as conn:
        rows = conn.execute(
            "SELECT chat_id, address, alias FROM wallets"
        ).fetchall()
    return [(r[0], r[1], r[2]) for r in rows]


def save_snapshot(chat_id: int, address: str, positions: Dict[str, Any]) -> None:
    import json
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            """
            INSERT INTO position_snapshots (chat_id, address, snapshot, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(chat_id, address) DO UPDATE SET
                snapshot = excluded.snapshot,
                updated_at = excluded.updated_at
            """,
            (chat_id, address, json.dumps(positions), datetime.utcnow().isoformat()),
        )
        conn.commit()


def load_snapshot(chat_id: int, address: str) -> Dict[str, Any]:
    import json
    with closing(sqlite3.connect(DB_PATH)) as conn:
        row = conn.execute(
            "SELECT snapshot FROM position_snapshots WHERE chat_id = ? AND address = ?",
            (chat_id, address),
        ).fetchone()
    if row:
        return json.loads(row[0])
    return {}


# -------------------- Hyperliquid API --------------------

async def fetch_clearinghouse(session: aiohttp.ClientSession, address: str) -> Optional[dict]:
    try:
        async with session.post(
            API_URL,
            json={"type": "clearinghouseState", "user": address},
            timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
        ) as resp:
            if resp.status != 200:
                logger.warning("HL API %s → %s", address[:10], resp.status)
                return None
            return await resp.json()
    except Exception as e:
        logger.error("Fetch error %s: %s", address[:10], e)
        return None


def parse_positions(state: dict, min_notional: float = 0) -> Dict[str, dict]:
    positions: Dict[str, dict] = {}
    if not state or "assetPositions" not in state:
        return positions

    for item in state.get("assetPositions", []):
        pos = item.get("position") or {}
        coin = pos.get("coin")
        try:
            szi = float(pos.get("szi") or 0)
        except (TypeError, ValueError):
            continue
        if abs(szi) < 1e-10:
            continue

        try:
            notional = abs(float(pos.get("positionValue") or 0))
        except (TypeError, ValueError):
            notional = 0.0

        if notional < min_notional:
            continue

        lev = pos.get("leverage") or {}
        positions[coin] = {
            "coin": coin,
            "szi": szi,
            "side": "LONG" if szi > 0 else "SHORT",
            "entry_px": float(pos.get("entryPx") or 0),
            "position_value": notional,
            "unrealized_pnl": float(pos.get("unrealizedPnl") or 0),
            "leverage": lev.get("value") or 0,
            "leverage_type": lev.get("type") or "cross",
            "liquidation_px": float(pos["liquidationPx"]) if pos.get("liquidationPx") else None,
            "margin_used": float(pos.get("marginUsed") or 0),
            "roe": float(pos.get("returnOnEquity") or 0) * 100,
        }
    return positions


def get_account_value(state: dict) -> float:
    try:
        return float((state.get("marginSummary") or {}).get("accountValue") or 0)
    except (TypeError, ValueError):
        return 0.0


# -------------------- Formatting --------------------

def short_addr(addr: str) -> str:
    return f"{addr[:6]}…{addr[-4:]}"


def fmt_usd(v: float, signed: bool = False) -> str:
    if signed:
        return f"${v:+,.2f}"
    return f"${v:,.2f}"


def fmt_position(pos: dict) -> str:
    side_emoji = "🟢" if pos["side"] == "LONG" else "🔴"
    pnl_emoji = "📈" if pos["unrealized_pnl"] >= 0 else "📉"
    liq = ""
    if pos.get("liquidation_px"):
        liq = f"\n   ⚠️ Liq: {fmt_usd(pos['liquidation_px'])}"

    return (
        f"{side_emoji} <b>{pos['coin']}</b> {pos['side']}  ·  {pos['leverage']}x {pos['leverage_type']}\n"
        f"   Size: {abs(pos['szi']):.4f}  ·  Notional: {fmt_usd(pos['position_value'])}\n"
        f"   Entry: {fmt_usd(pos['entry_px'])}  ·  {pnl_emoji} uPnL: {fmt_usd(pos['unrealized_pnl'], True)} ({pos['roe']:+.1f}%)"
        f"{liq}"
    )


def build_alert(
    action: str,
    alias: Optional[str],
    address: str,
    pos: Optional[dict],
    account_value: float,
    extra: str = "",
) -> str:
    name = alias or short_addr(address)
    header = {
        "OPEN": "🚀 <b>OPEN POSITION</b>",
        "CLOSE": "❌ <b>CLOSE POSITION</b>",
        "ADD": "➕ <b>ADD SIZE</b>",
        "REDUCE": "➖ <b>REDUCE SIZE</b>",
    }.get(action, f"<b>{action}</b>")

    lines = [
        f"{header}",
        f"👤 {name}",
        f"<code>{address}</code>",
        "",
    ]
    if pos:
        lines.append(fmt_position(pos))
    if extra:
        lines.append(extra)
    lines.append("")
    lines.append(f"💰 Account Value: <b>{fmt_usd(account_value)}</b>")
    lines.append(f"🕐 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    return "\n".join(lines)


# -------------------- Background Tracker --------------------

async def track_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Job yang dipanggil secara periodik oleh JobQueue."""
    app = context.application
    session: aiohttp.ClientSession = app.bot_data["session"]

    tracked = get_all_tracked()
    if not tracked:
        return

    # Group by address to avoid duplicate API calls
    addr_to_users: Dict[str, List[Tuple[int, Optional[str]]]] = {}
    for chat_id, address, alias in tracked:
        addr_to_users.setdefault(address, []).append((chat_id, alias))

    for address, users in addr_to_users.items():
        state = await fetch_clearinghouse(session, address)
        if state is None:
            continue

        account_value = get_account_value(state)

        for chat_id, alias in users:
            min_notional, _ = get_user_settings(chat_id)
            new_pos = parse_positions(state, min_notional)
            old_pos = load_snapshot(chat_id, address)

            # Deteksi perubahan
            all_coins = set(old_pos) | set(new_pos)
            for coin in all_coins:
                old = old_pos.get(coin)
                new = new_pos.get(coin)

                if old is None and new is not None:
                    msg = build_alert("OPEN", alias, address, new, account_value)
                    await _safe_send(context.bot, chat_id, msg)
                elif old is not None and new is None:
                    msg = build_alert(
                        "CLOSE",
                        alias,
                        address,
                        None,
                        account_value,
                        extra=f"Sebelumnya: {old['side']} {old['coin']} · Size {abs(old['szi']):.4f} · {fmt_usd(old['position_value'])}",
                    )
                    await _safe_send(context.bot, chat_id, msg)
                elif old and new:
                    delta = abs(new["szi"]) - abs(old["szi"])
                    if abs(delta) > 1e-8:
                        action = "ADD" if delta > 0 else "REDUCE"
                        msg = build_alert(
                            action,
                            alias,
                            address,
                            new,
                            account_value,
                            extra=f"Δ Size: {delta:+.4f}",
                        )
                        await _safe_send(context.bot, chat_id, msg)

            save_snapshot(chat_id, address, new_pos)

        await asyncio.sleep(0.35)  # rate-limit friendly


async def _safe_send(bot, chat_id: int, text: str) -> None:
    try:
        await bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    except Exception as e:
        logger.warning("Gagal kirim ke %s: %s", chat_id, e)


# -------------------- Command Handlers --------------------

WELCOME = """
🌊 <b>Hyperliquid Wallet Tracker</b>

Pantau posisi perp wallet mana pun secara real-time.
Setiap kali wallet yang kamu track <b>open / close / add / reduce</b> posisi, bot akan mengirim alert.

<b>Perintah utama:</b>
/add <code>0x...</code> [alias] – Tambah wallet
/remove <code>0x...</code> atau alias – Hapus
/list – Daftar wallet yang di-track
/positions [wallet] – Lihat posisi terbuka
/summary – Ringkasan semua wallet
/settings – Atur filter & interval
/help – Bantuan lengkap

Contoh:
<code>/add 0x31ca8395cf837de08b24da3f660e77761dfb974b Whale1</code>
"""


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ensure_user(update.effective_chat.id)
    await update.message.reply_text(WELCOME, parse_mode=ParseMode.HTML)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = """
📖 <b>Bantuan Lengkap</b>

<b>1. Menambah Wallet</b>
<code>/add 0xWalletAddress</code>
<code>/add 0xWalletAddress NamaAlias</code>

Alias memudahkan identifikasi di notifikasi.

<b>2. Menghapus Wallet</b>
<code>/remove 0xWalletAddress</code>
<code>/remove NamaAlias</code>

<b>3. Melihat Daftar</b>
/list

<b>4. Cek Posisi</b>
/positions → semua wallet
/positions 0x... atau alias → satu wallet

<b>5. Ringkasan</b>
/summary → account value + total uPnL semua wallet

<b>6. Settings</b>
/settings → lihat pengaturan
/setmin 500 → abaikan posisi di bawah $500
/setinterval 30 → cek setiap 30 detik (min 15)

⚠️ Batas: maksimal {max} wallet per user.
""".format(max=MAX_WALLETS_PER_USER)
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if not context.args:
        await update.message.reply_text(
            "Format: <code>/add 0xWallet [alias]</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    address = context.args[0].strip()
    if not re.fullmatch(r"0x[a-fA-F0-9]{40}", address):
        await update.message.reply_text("❌ Alamat wallet tidak valid (harus 0x + 40 hex).")
        return

    alias = " ".join(context.args[1:]).strip() or None
    ok, msg = add_wallet(chat_id, address, alias)
    if not ok:
        await update.message.reply_text(f"❌ {msg}")
        return

    name = alias or short_addr(address)
    await update.message.reply_text(
        f"✅ Wallet ditambahkan!\n\n"
        f"🏷 <b>{name}</b>\n"
        f"<code>{address}</code>\n\n"
        f"Bot akan mulai memantau posisi dan mengirim alert otomatis.",
        parse_mode=ParseMode.HTML,
    )

    # Ambil snapshot awal biar tidak spam alert palsu
    session: aiohttp.ClientSession = context.application.bot_data["session"]
    state = await fetch_clearinghouse(session, address.lower())
    if state:
        min_notional, _ = get_user_settings(chat_id)
        positions = parse_positions(state, min_notional)
        save_snapshot(chat_id, address.lower(), positions)


async def cmd_remove(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Format: <code>/remove 0x... atau alias</code>", parse_mode=ParseMode.HTML)
        return
    query = " ".join(context.args)
    ok, msg = remove_wallet(update.effective_chat.id, query)
    if ok:
        await update.message.reply_text(f"✅ {msg}")
    else:
        await update.message.reply_text(f"❌ {msg}")


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    wallets = list_wallets(update.effective_chat.id)
    if not wallets:
        await update.message.reply_text(
            "Belum ada wallet yang di-track.\nGunakan /add 0x... untuk menambah."
        )
        return

    lines = ["📋 <b>Daftar Wallet yang di-Track</b>\n"]
    for i, (addr, alias) in enumerate(wallets, 1):
        name = f"<b>{alias}</b> — " if alias else ""
        lines.append(f"{i}. {name}<code>{addr}</code>")

    lines.append(f"\nTotal: {len(wallets)} / {MAX_WALLETS_PER_USER}")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_positions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    wallets = list_wallets(chat_id)
    if not wallets:
        await update.message.reply_text("Belum ada wallet. Gunakan /add dulu.")
        return

    target = None
    if context.args:
        q = " ".join(context.args).lower()
        for addr, alias in wallets:
            if addr == q or (alias and alias.lower() == q):
                target = [(addr, alias)]
                break
        if not target:
            await update.message.reply_text("Wallet / alias tidak ditemukan di daftar kamu.")
            return
    else:
        target = wallets

    session: aiohttp.ClientSession = context.application.bot_data["session"]
    min_notional, _ = get_user_settings(chat_id)
    status = await update.message.reply_text("⏳ Mengambil data posisi…")

    parts = []
    for addr, alias in target:
        state = await fetch_clearinghouse(session, addr)
        if not state:
            parts.append(f"❌ Gagal ambil data untuk {alias or short_addr(addr)}")
            continue

        positions = parse_positions(state, min_notional)
        acc_val = get_account_value(state)
        name = alias or short_addr(addr)

        header = f"👤 <b>{name}</b>\n<code>{addr}</code>\n💰 Account: <b>{fmt_usd(acc_val)}</b>\n"
        if not positions:
            parts.append(header + "📭 Tidak ada posisi terbuka.")
        else:
            total_upnl = sum(p["unrealized_pnl"] for p in positions.values())
            body = "\n\n".join(fmt_position(p) for p in positions.values())
            parts.append(
                header
                + f"📊 {len(positions)} posisi · Total uPnL: <b>{fmt_usd(total_upnl, True)}</b>\n\n"
                + body
            )

    text = "\n\n────────────\n\n".join(parts)
    # Telegram limit ~4096 chars
    if len(text) > 4000:
        text = text[:3900] + "\n\n… (terpotong)"

    await status.edit_text(text, parse_mode=ParseMode.HTML)


async def cmd_summary(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    wallets = list_wallets(chat_id)
    if not wallets:
        await update.message.reply_text("Belum ada wallet. Gunakan /add dulu.")
        return

    session: aiohttp.ClientSession = context.application.bot_data["session"]
    min_notional, _ = get_user_settings(chat_id)
    status = await update.message.reply_text("⏳ Menghitung ringkasan…")

    total_value = 0.0
    total_upnl = 0.0
    total_positions = 0
    lines = ["📊 <b>Ringkasan Portfolio</b>\n"]

    for addr, alias in wallets:
        state = await fetch_clearinghouse(session, addr)
        if not state:
            lines.append(f"• {alias or short_addr(addr)} → error")
            continue
        positions = parse_positions(state, min_notional)
        acc = get_account_value(state)
        upnl = sum(p["unrealized_pnl"] for p in positions.values())
        total_value += acc
        total_upnl += upnl
        total_positions += len(positions)
        name = alias or short_addr(addr)
        lines.append(
            f"• <b>{name}</b> — {fmt_usd(acc)} · uPnL {fmt_usd(upnl, True)} · {len(positions)} pos"
        )

    lines.append("")
    lines.append(f"💰 <b>Total Account Value:</b> {fmt_usd(total_value)}")
    lines.append(f"📈 <b>Total Unrealized PnL:</b> {fmt_usd(total_upnl, True)}")
    lines.append(f"📦 <b>Total Open Positions:</b> {total_positions}")

    await status.edit_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    min_n, interval = get_user_settings(chat_id)
    text = (
        f"⚙️ <b>Pengaturan</b>\n\n"
        f"• Min Notional: <b>${min_n:,.0f}</b>\n"
        f"  (posisi di bawah ini diabaikan)\n"
        f"• Poll Interval: <b>{interval}s</b>\n\n"
        f"Ubah dengan:\n"
        f"<code>/setmin 200</code>\n"
        f"<code>/setinterval 25</code>"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def cmd_setmin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Contoh: /setmin 150")
        return
    try:
        val = float(context.args[0])
        if val < 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Nilai harus angka ≥ 0")
        return
    set_user_setting(update.effective_chat.id, "min_notional", val)
    await update.message.reply_text(f"✅ Min notional diubah menjadi ${val:,.0f}")


async def cmd_setinterval(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Contoh: /setinterval 20")
        return
    try:
        val = int(context.args[0])
        if val < 15:
            await update.message.reply_text("Minimum interval 15 detik (rate limit).")
            return
    except ValueError:
        await update.message.reply_text("Nilai harus angka bulat")
        return
    set_user_setting(update.effective_chat.id, "poll_interval", val)
    await update.message.reply_text(
        f"✅ Interval diubah menjadi {val}s.\n"
        f"Catatan: job global tetap berjalan ~setiap 20s; setting ini dipakai untuk filter di masa depan."
    )


async def unknown(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Kalau user langsung kirim alamat 0x...
    text = (update.message.text or "").strip()
    if re.fullmatch(r"0x[a-fA-F0-9]{40}", text):
        context.args = [text]
        await cmd_add(update, context)
        return
    await update.message.reply_text(
        "Perintah tidak dikenal. Ketik /help untuk daftar perintah."
    )


# -------------------- Main --------------------

async def post_init(app: Application) -> None:
    session = aiohttp.ClientSession()
    app.bot_data["session"] = session

    # Global tracking job – setiap 20 detik
    app.job_queue.run_repeating(
        track_job,
        interval=DEFAULT_POLL_INTERVAL,
        first=8,
        name="hl_tracker",
    )
    logger.info("Background tracker started (interval %ss)", DEFAULT_POLL_INTERVAL)


async def post_shutdown(app: Application) -> None:
    session: aiohttp.ClientSession = app.bot_data.get("session")
    if session:
        await session.close()


def main() -> None:
    if not BOT_TOKEN:
        print("❌ Set environment variable BOT_TOKEN dulu!")
        print("   export BOT_TOKEN='123456:ABC-DEF...'")
        print("   python hl_telegram_tracker.py")
        return

    init_db()

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("remove", cmd_remove))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("positions", cmd_positions))
    app.add_handler(CommandHandler("pos", cmd_positions))
    app.add_handler(CommandHandler("summary", cmd_summary))
    app.add_handler(CommandHandler("settings", cmd_settings))
    app.add_handler(CommandHandler("setmin", cmd_setmin))
    app.add_handler(CommandHandler("setinterval", cmd_setinterval))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, unknown))

    logger.info("Bot starting…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
