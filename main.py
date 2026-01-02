#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Scam-base bot — full single-file implementation (TeleBot)

Features:
- Extended scam entries (reason, proof_text, comment, added_by, date)
- Photos stored in separate table scam_photos(file_id)
- Inline panel under /проверить with actions:
    🔥 Add to scam
    ❌ Remove from scam
    📝 Edit record (asks: reason, proof_text, comment)
    📄 Add proofs (photos) — send photos then /done
- /скам_стата — statistics for staff: username — messages / adds
- Staff system with roles
- CSV export for owner
- Per-user pending-actions state machine for multi-step flows
- Logging table and basic action logs
"""

import telebot
from telebot import types
import sqlite3
import csv
import io
import os
import threading
from datetime import datetime

# ========== CONFIG ==========
BOT_TOKEN = "8276253982:AAGSBdDaVBHCFOmi6-4PGZGvRGnrU8X4JmM"  # <- already set
OWNER_ID = 7504103313
MAIN_SCAM_CHAT_ID = -1002374406940
STAFF_CHAT_ID = -1003235703843
DB_FILE = "scam_full.db"
AUTO_DELETE = False  # set True if you want deletion of commands/replies
DELETE_DELAY = 6
# ===========================

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")

# ========== DB Setup ==========
conn = sqlite3.connect(DB_FILE, check_same_thread=False)
cur = conn.cursor()

# scam entries: user_id primary, reason, proof_text (text), comment, added_by, date
cur.execute("""
CREATE TABLE IF NOT EXISTS scam_list (
    user_id INTEGER PRIMARY KEY,
    reason TEXT,
    proof_text TEXT,
    comment TEXT,
    added_by INTEGER,
    added_by_name TEXT,
    added_at TEXT
)
""")

# photos: many per scam entry
cur.execute("""
CREATE TABLE IF NOT EXISTS scam_photos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scam_user_id INTEGER,
    file_id TEXT
)
""")

# staff table
cur.execute("""
CREATE TABLE IF NOT EXISTS staff (
    user_id INTEGER PRIMARY KEY,
    role TEXT
)
""")

# stats for staff
cur.execute("""
CREATE TABLE IF NOT EXISTS staff_stats (
    user_id INTEGER PRIMARY KEY,
    messages INTEGER DEFAULT 0,
    adds INTEGER DEFAULT 0
)
""")

# logs
cur.execute("""
CREATE TABLE IF NOT EXISTS actions_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT,
    actor_id INTEGER,
    actor_name TEXT,
    action TEXT
)
""")

conn.commit()

# ========== Helpers ==========
def now_ts():
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

def log_action(actor_id, actor_name, action):
    ts = now_ts()
    try:
        cur.execute("INSERT INTO actions_log (ts, actor_id, actor_name, action) VALUES (?, ?, ?, ?)",
                    (ts, actor_id, actor_name, action))
        conn.commit()
    except Exception:
        pass

def get_staff_role(user_id):
    cur.execute("SELECT role FROM staff WHERE user_id = ?", (user_id,))
    r = cur.fetchone()
    return r[0] if r else None

def is_owner(user_id):
    return user_id == OWNER_ID

def is_admin_in_staff_chat(user_id):
    try:
        memb = bot.get_chat_member(STAFF_CHAT_ID, user_id)
        return memb.status in ("administrator", "creator")
    except Exception:
        return False

def is_staff(user_id):
    if is_owner(user_id):
        return True
    return get_staff_role(user_id) is not None

def inc_staff_message(user_id):
    cur.execute("INSERT OR IGNORE INTO staff_stats (user_id, messages, adds) VALUES (?, 0, 0)", (user_id,))
    cur.execute("UPDATE staff_stats SET messages = messages + 1 WHERE user_id = ?", (user_id,))
    conn.commit()

def inc_staff_add(user_id):
    cur.execute("INSERT OR IGNORE INTO staff_stats (user_id, messages, adds) VALUES (?, 0, 0)", (user_id,))
    cur.execute("UPDATE staff_stats SET adds = adds + 1 WHERE user_id = ?", (user_id,))
    conn.commit()

def parse_token_to_id(token):
    token = token.strip()
    if token.startswith("@"):
        try:
            ch = bot.get_chat(token)
            return ch.id
        except Exception:
            return None
    if token.lstrip("-").isdigit():
        return int(token)
    return None

def pretty_user(uid, username=None):
    if username:
        username = username.lstrip("@")
        return f"<a href='tg://user?id={uid}'>@{username}</a>"
    return f"<a href='tg://user?id={uid}'>id:{uid}</a>"

# ========== Pending actions (in-memory, simple) ==========
# Structure: pending[user_id] = {"action": "...", "target": <user_id>, "buffer": {...}}
pending = {}
# Example actions:
# "add_proofs" -> expecting photos, buffer holds {"files":[file_id,...], "target":user}
# "edit_all" -> multi-step editing: steps: 1 reason, 2 proof_text, 3 comment
# "confirm_add" -> confirm before adding operator etc.

def set_pending(user_id, data):
    pending[user_id] = data

def get_pending(user_id):
    return pending.get(user_id)

def clear_pending(user_id):
    if user_id in pending:
        del pending[user_id]

# ========== DB operations ==========
def scam_exists(user_id):
    cur.execute("SELECT user_id FROM scam_list WHERE user_id = ?", (user_id,))
    return cur.fetchone() is not None

def add_scam_db(user_id, reason, proof_text, comment, added_by, added_by_name):
    if scam_exists(user_id):
        return False
    cur.execute("INSERT INTO scam_list (user_id, reason, proof_text, comment, added_by, added_by_name, added_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (user_id, reason, proof_text, comment, added_by, added_by_name, now_ts()))
    conn.commit()
    log_action(added_by, added_by_name or str(added_by), f"ADD_SCAM user={user_id}")
    inc_staff_add(added_by)
    return True

def update_scam_db(user_id, reason=None, proof_text=None, comment=None):
    if not scam_exists(user_id):
        return False
    # fetch current
    cur.execute("SELECT reason, proof_text, comment FROM scam_list WHERE user_id = ?", (user_id,))
    r = cur.fetchone()
    if not r:
        return False
    cur_reason, cur_proof, cur_comment = r
    new_reason = reason if reason is not None else cur_reason
    new_proof = proof_text if proof_text is not None else cur_proof
    new_comment = comment if comment is not None else cur_comment
    cur.execute("UPDATE scam_list SET reason = ?, proof_text = ?, comment = ? WHERE user_id = ?",
                (new_reason, new_proof, new_comment, user_id))
    conn.commit()
    return True

def remove_scam_db(user_id):
    if not scam_exists(user_id):
        return False
    cur.execute("DELETE FROM scam_list WHERE user_id = ?", (user_id,))
    cur.execute("DELETE FROM scam_photos WHERE scam_user_id = ?", (user_id,))
    conn.commit()
    log_action(0, "system", f"REMOVE_SCAM user={user_id}")
    return True

def add_scam_photo(user_id, file_id):
    cur.execute("INSERT INTO scam_photos (scam_user_id, file_id) VALUES (?, ?)", (user_id, file_id))
    conn.commit()

def get_scam_photos(user_id):
    cur.execute("SELECT file_id FROM scam_photos WHERE scam_user_id = ?", (user_id,))
    return [r[0] for r in cur.fetchall()]

def add_staff_db(user_id, role):
    cur.execute("INSERT OR REPLACE INTO staff (user_id, role) VALUES (?, ?)", (user_id, role))
    conn.commit()
    log_action(0, "system", f"ADD_STAFF user={user_id} role={role}")

def remove_staff_db(user_id):
    cur.execute("DELETE FROM staff WHERE user_id = ?", (user_id,))
    conn.commit()

# ========== Command handlers ==========

# START with STAFF button
@bot.message_handler(commands=["start"])
def cmd_start(m):
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("📋 STAFF", callback_data="show_staff"))
    kb.add(types.InlineKeyboardButton("📥 Export SCAM", callback_data="export_scam_cb"))
    kb.add(types.InlineKeyboardButton("📥 Export STAFF", callback_data="export_staff_cb"))
    bot.send_message(m.chat.id, "<b>GEROINES | SCAM-BASE</b>\nУправление базой.\nКоманды: +скам, -скам, +стафф, -стафф, /проверить, /скам_стата, /export_scam, /export_staff", reply_markup=kb, parse_mode="HTML")

# Inline callbacks for start buttons
@bot.callback_query_handler(func=lambda c: c.data in ("show_staff", "export_scam_cb", "export_staff_cb"))
def handle_start_buttons(call):
    if call.data == "show_staff":
        send_staff_list(call.message.chat.id)
    elif call.data == "export_scam_cb":
        if call.from_user.id != OWNER_ID:
            bot.answer_callback_query(call.id, "Только владелец может экспортировать.", show_alert=True)
            return
        export_scam(call.message)
    elif call.data == "export_staff_cb":
        if call.from_user.id != OWNER_ID:
            bot.answer_callback_query(call.id, "Только владелец может экспортировать.", show_alert=True)
            return
        export_staff(call.message)
    bot.answer_callback_query(call.id)

# +стафф: assign role (only in STAFF_CHAT_ID)
@bot.message_handler(regexp=r"^\+стафф\b", func=lambda m: m.chat.id == STAFF_CHAT_ID)
def cmd_plus_staff(m):
    sender = m.from_user
    allowed = is_owner(sender.id) or is_admin_in_staff_chat(sender.id) or (get_staff_role(sender.id) == "администратор")
    if not allowed:
        bot.reply_to(m, "⛔ У вас нет прав назначать стафф.")
        return
    parts = m.text.split(maxsplit=2)
    if len(parts) < 3 and not m.reply_to_message:
        bot.reply_to(m, "Использование: +стафф @username роль  (или реплай +стафф роль)")
        return
    # get target and role
    if m.reply_to_message:
        tuser = m.reply_to_message.from_user
        uid = tuser.id
        role = parts[1].strip().lower() if len(parts) > 1 else None
    else:
        token = parts[1]
        role = parts[2].strip().lower()
        uid = parse_token_to_id(token) or None
    if uid is None:
        bot.reply_to(m, "Не удалось определить пользователя.")
        return
    # normalize roles
    role_map = {"владелец":"владелец", "зам":"заместитель", "заместитель":"заместитель",
                "админ":"администратор", "администрато":"администратор", "администратор":"администратор",
                "ст.модератор":"ст.модератор", "ст.мод":"ст.модератор",
                "мод":"модератор", "модератор":"модератор"}
    if role not in role_map:
        bot.reply_to(m, "Неверная роль. Доступные: владелец, зам, админ, ст.модератор, модератор")
        return
    add_staff_db(uid, role_map[role])
    bot.reply_to(m, f"✅ Назначен: {pretty_user(uid)} — {role_map[role]}")
    log_action(sender.id, sender.username or str(sender.id), f"ASSIGN_STAFF {uid} role={role_map[role]}")

# -стафф: remove (only in STAFF_CHAT_ID)
@bot.message_handler(regexp=r"^\-стафф\b", func=lambda m: m.chat.id == STAFF_CHAT_ID)
def cmd_minus_staff(m):
    sender = m.from_user
    allowed = is_owner(sender.id) or is_admin_in_staff_chat(sender.id) or (get_staff_role(sender.id) == "администратор")
    if not allowed:
        bot.reply_to(m, "⛔ У вас нет прав снимать стафф.")
        return
    # expect reply to user
    if not m.reply_to_message:
        bot.reply_to(m, "Сделайте реплай на сообщение пользователя, чтобы снять роль.")
        return
    target = m.reply_to_message.from_user
    remove_staff_db(target.id)
    bot.reply_to(m, f"🗑 Снят со стаффа: {pretty_user(target.id, target.username or '')}")
    log_action(sender.id, sender.username or str(sender.id), f"REMOVE_STAFF {target.id}")

# +скам — add entry (only in MAIN_SCAM_CHAT_ID and staff)
@bot.message_handler(regexp=r"^\+скам\b", func=lambda m: m.chat.id == MAIN_SCAM_CHAT_ID)
def cmd_plus_scam(m):
    sender = m.from_user
    if not is_staff(sender.id):
        bot.reply_to(m, "⛔ Только сотрудники могут добавлять в скам.")
        return
    # increment staff message stat
    inc_staff_message(sender.id)
    # target: prefer reply
    reason = ""
    proof_text = ""
    comment = ""
    if m.reply_to_message:
        target = m.reply_to_message.from_user
        uid = target.id
        if len(m.text.split(maxsplit=1)) > 1:
            reason = m.text.split(maxsplit=1)[1].strip()
    else:
        parts = m.text.split(maxsplit=2)
        if len(parts) < 2:
            bot.reply_to(m, "Использование: +скам (reply) или +скам @username [причина]")
            return
        token = parts[1]
        uid = parse_token_to_id(token)
        if uid is None:
            bot.reply_to(m, "Не удалось получить ID пользователя.")
            return
        if len(parts) > 2:
            reason = parts[2].strip()
    if scam_exists(uid):
        bot.reply_to(m, f"⚠ {pretty_user(uid)} уже в базе.")
        return
    ok = add_scam_db(uid, reason, proof_text, comment, sender.id, sender.username or "")
    if ok:
        bot.reply_to(m, f"🛑 {pretty_user(uid)} добавлен(а) в скам.\nПричина: {reason or '-'}")
    else:
        bot.reply_to(m, "Ошибка добавления.")
    # inc adds stat done inside add_scam_db

# -скам — remove (only in MAIN_SCAM_CHAT_ID and staff)
@bot.message_handler(regexp=r"^\-скам\b", func=lambda m: m.chat.id == MAIN_SCAM_CHAT_ID)
def cmd_minus_scam(m):
    sender = m.from_user
    if not is_staff(sender.id):
        bot.reply_to(m, "⛔ Только сотрудники могут удалять записи.")
        return
    inc_staff_message(sender.id)
    if m.reply_to_message:
        target = m.reply_to_message.from_user
        uid = target.id
    else:
        parts = m.text.split(maxsplit=1)
        if len(parts) < 2:
            bot.reply_to(m, "Использование: -скам (reply) или -скам @username")
            return
        token = parts[1]
        uid = parse_token_to_id(token)
        if uid is None:
            bot.reply_to(m, "Не удалось распознать аргумент.")
            return
    ok = remove_scam_db(uid)
    if ok:
        bot.reply_to(m, f"✅ {pretty_user(uid)} удалён(а) из скам-базы.")
    else:
        bot.reply_to(m, f"⚠ {pretty_user(uid)} не найден(а).")

# +скам список / -скам список (bulk) — only in MAIN_SCAM_CHAT_ID and staff
@bot.message_handler(func=lambda m: m.text and m.text.startswith("+скам список"), content_types=["text"])
def cmd_plus_scam_list(m):
    sender = m.from_user
    if m.chat.id != MAIN_SCAM_CHAT_ID:
        return
    if not is_staff(sender.id):
        bot.reply_to(m, "⛔ Нет прав.")
        return
    inc_staff_message(sender.id)
    payload = m.text.replace("+скам список", "", 1).strip()
    if not payload:
        bot.reply_to(m, "Укажите список id через пробел.")
        return
    ids = payload.split()
    added = 0
    for tok in ids:
        try:
            uid = int(tok)
        except:
            continue
        if not scam_exists(uid):
            add_scam_db(uid, "", "", "", sender.id, sender.username or "")
            added += 1
    bot.reply_to(m, f"✅ Добавлено: {added}")

@bot.message_handler(func=lambda m: m.text and m.text.startswith("-скам список"), content_types=["text"])
def cmd_minus_scam_list(m):
    sender = m.from_user
    if m.chat.id != MAIN_SCAM_CHAT_ID:
        return
    if not is_staff(sender.id):
        bot.reply_to(m, "⛔ Нет прав.")
        return
    inc_staff_message(sender.id)
    payload = m.text.replace("-скам список", "", 1).strip()
    if not payload:
        bot.reply_to(m, "Укажите список id через пробел.")
        return
    ids = payload.split()
    removed = 0
    for tok in ids:
        try:
            uid = int(tok)
        except:
            continue
        if scam_exists(uid):
            remove_scam_db(uid)
            removed += 1
    bot.reply_to(m, f"✅ Удалено: {removed}")

# /проверить — check and show inline panel
@bot.message_handler(commands=["проверить", "check"])
def cmd_check(m):
    target_id = None
    if m.reply_to_message:
        target_id = m.reply_to_message.from_user.id
    else:
        parts = m.text.split(maxsplit=1)
        if len(parts) < 2:
            bot.reply_to(m, "Использование: /проверить @username or /проверить id (или reply).")
            return
        token = parts[1]
        target_id = parse_token_to_id(token)
        if target_id is None:
            bot.reply_to(m, "Не удалось разрешить аргумент.")
            return
    # fetch record
    cur.execute("SELECT reason, proof_text, comment, added_by_name, added_at FROM scam_list WHERE user_id = ?", (target_id,))
    r = cur.fetchone()
    reason = r[0] if r else None
    proof_text = r[1] if r else None
    comment = r[2] if r else None
    added_by_name = r[3] if r else None
    added_at = r[4] if r else None
    # prepare message
    if r:
        text = (f"🛑 <b>Запись найденa</b>\nПользователь: <code>{target_id}</code>\n"
                f"Причина: {reason or '-'}\nДоказательства (текст): {proof_text or '-'}\nКомментарий: {comment or '-'}\n"
                f"Добавил: {added_by_name or '-'} в {added_at or '-'}")
    else:
        text = f"✅ Пользователь <code>{target_id}</code> не найден в скам-базе."
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(types.InlineKeyboardButton("🔥 Добавить в скам", callback_data=f"add_scam:{target_id}"))
    kb.add(types.InlineKeyboardButton("❌ Удалить из скама", callback_data=f"remove_scam:{target_id}"))
    kb.add(types.InlineKeyboardButton("📝 Изменить запись", callback_data=f"edit_scam:{target_id}"))
    kb.add(types.InlineKeyboardButton("📄 Добавить доказательства", callback_data=f"proof_scam:{target_id}"))
    sent = bot.reply_to(m, text, reply_markup=kb, parse_mode="HTML")
    # schedule deletion if desired
    if AUTO_DELETE:
        try:
            threading.Timer(DELETE_DELAY, lambda: bot.delete_message(sent.chat.id, sent.message_id)).start()
        except Exception:
            pass

# Handle callbacks from panel
@bot.callback_query_handler(func=lambda call: True)
def callback_panel(call):
    data = call.data
    user = call.from_user
    # each callback: check permission (staff for actions)
    if data.startswith("add_scam:"):
        target = int(data.split(":",1)[1])
        if not is_staff(user.id):
            bot.answer_callback_query(call.id, "Только сотрудники могут добавлять.", show_alert=True)
            return
        if scam_exists(target):
            bot.answer_callback_query(call.id, "Уже в скам-базе.", show_alert=True)
            return
        # create pending confirm add (we will add with empty reason and prompt for reason)
        set_pending(user.id, {"action":"add_and_edit", "target":target})
        bot.send_message(user.id, f"Вы собираетесь добавить {target} в скам. Отправьте причину (текст).")
        bot.answer_callback_query(call.id, "Отправьте причину в личку боту.")
        return

    if data.startswith("remove_scam:"):
        target = int(data.split(":",1)[1])
        if not is_staff(user.id):
            bot.answer_callback_query(call.id, "Только сотрудники могут удалять.", show_alert=True)
            return
        ok = remove_scam_db(target)
        if ok:
            bot.answer_callback_query(call.id, "Запись удалена.", show_alert=True)
            bot.send_message(call.message.chat.id, f"🗑 Запись {target} удалена пользователем {user.username or user.id}")
        else:
            bot.answer_callback_query(call.id, "Запись не найдена.", show_alert=True)
        return

    if data.startswith("edit_scam:"):
        target = int(data.split(":",1)[1])
        if not is_staff(user.id):
            bot.answer_callback_query(call.id, "Только сотрудники могут редактировать.", show_alert=True)
            return
        # start edit_all flow: ask for reason, then proof_text, then comment
        set_pending(user.id, {"action":"edit_all", "target":target, "step":1, "buffer":{}})
        bot.send_message(user.id, f"Начинаем редактирование записи {target}.\n1/3 — Введите новую причину (или напишите '-' чтобы оставить прежнюю).")
        bot.answer_callback_query(call.id, "Я отправил инструкцию в личку.")
        return

    if data.startswith("proof_scam:"):
        target = int(data.split(":",1)[1])
        if not is_staff(user.id):
            bot.answer_callback_query(call.id, "Только сотрудники могут добавлять доказательства.", show_alert=True)
            return
        # start add_proofs flow: collect photos, user sends photos, then /done
        set_pending(user.id, {"action":"add_proofs", "target":target, "buffer":{"files":[]}})
        bot.send_message(user.
