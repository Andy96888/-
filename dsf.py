# dsf.py
import aiosqlite
import os
import logging
import asyncio
from datetime import datetime
from telegram.ext import Application, MessageHandler, filters, ContextTypes, CallbackQueryHandler
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Message, CallbackQuery
from telegram.request import HTTPXRequest
from telegram.error import TimedOut
import re
import math
from logging.handlers import TimedRotatingFileHandler
from collections import namedtuple
from types import SimpleNamespace

# --- 日志配置 ---
def setup_logging():
    try:
        log_dir = "logs"
        if not os.path.exists(log_dir):
            os.makedirs(log_dir)
        log_file = os.path.join(log_dir, "bot.log")
        logger = logging.getLogger()
        logger.setLevel(logging.INFO)
        if logger.hasHandlers():
            logger.handlers.clear()
        file_handler = TimedRotatingFileHandler(
            log_file, when='midnight', interval=1, backupCount=30, encoding='utf-8'
        )
        file_handler.suffix = "%Y-%m-%d"
        file_formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
        file_handler.setFormatter(file_formatter)
        logger.addHandler(file_handler)
        console_handler = logging.StreamHandler()
        console_formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
        console_handler.setFormatter(console_formatter)
        logger.addHandler(console_handler)
    except Exception as e:
        print(f"CRITICAL: 日志系统初始化失败! 错误: {e}")
        exit(1)

setup_logging()

# --- [优化] 核心网络优化：带重试机制的通用API调用函数 ---
async def _robust_telegram_call(api_call, max_retries=3, initial_delay=1.0, *args, **kwargs):
    """
    一个通用的Telegram API可靠调用函数，遇到超时会自动重试。
    """
    delay = initial_delay
    for attempt in range(max_retries):
        try:
            return await api_call(*args, **kwargs)
        except TimedOut:
            logging.warning(
                f"API call {api_call.__name__} timed out. Attempt {attempt + 1}/{max_retries}. "
                f"Retrying in {delay} seconds..."
            )
            if attempt + 1 == max_retries:
                logging.error(f"API call failed after {max_retries} attempts. Giving up.")
                raise  # 在多次重试失败后，重新抛出异常
            await asyncio.sleep(delay)
            delay *= 2
        except Exception as e:
            logging.error(f"An unexpected error occurred during API call {api_call.__name__}: {e}", exc_info=True)
            raise # 其他错误直接抛出

async def send_robust_reply(target: Message, text: str, **kwargs):
    """可靠地回复消息"""
    try:
        await _robust_telegram_call(target.reply_text, text=text, **kwargs)
    except (TimedOut, Exception):
        # 即使重试后仍然失败，我们也不让程序崩溃
        logging.error(f"send_robust_reply finally failed for target {target.message_id}")

async def robust_answer(query: CallbackQuery, **kwargs):
    """可靠地应答回调查询"""
    try:
        await _robust_telegram_call(query.answer, **kwargs)
    except (TimedOut, Exception):
        logging.error(f"robust_answer finally failed for query {query.id}")

async def robust_edit_message_text(query: CallbackQuery, **kwargs):
    """可靠地编辑消息文本"""
    try:
        await _robust_telegram_call(query.edit_message_text, **kwargs)
    except (TimedOut, Exception):
        logging.error(f"robust_edit_message_text finally failed for query {query.id}")
        # 如果编辑失败，可以尝试发送一条新消息作为备用方案
        try:
            await send_robust_reply(query.message, "更新账单详情失败，请重试。")
        except:
            pass

# --- 全局工具与配置 ---

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # 这个处理器现在只处理那些我们没有预料到或无法通过重试解决的严重错误
    logging.error(f"An uncaught error occurred for Update {update}: {context.error}", exc_info=context.error)
    if update and hasattr(update, 'effective_message'):
        try:
            await update.effective_message.reply_text("发生未知内部错误，请联系管理员检查日志。")
        except Exception as e:
            logging.error(f"Failed to send final generic error notification: {e}")


def get_db_path(group_id: int) -> str:
    data_dir = "data"
    if not os.path.exists(data_dir):
        os.makedirs(data_dir)
    return os.path.join(data_dir, f"group_{group_id}.db")

async def init_group_db(group_id: int):
    db_path = get_db_path(group_id)
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute("CREATE TABLE IF NOT EXISTS cycles (cycle_id INTEGER PRIMARY KEY AUTOINCREMENT, group_id INTEGER, start_time TEXT, end_time TEXT, is_active BOOLEAN DEFAULT TRUE)")
        await conn.execute("CREATE TABLE IF NOT EXISTS bills (bill_id INTEGER PRIMARY KEY AUTOINCREMENT, cycle_id INTEGER, group_id INTEGER, user_id INTEGER, amount DECIMAL(10,0), description TEXT, created_at TEXT, FOREIGN KEY (cycle_id) REFERENCES cycles(cycle_id))")
        await conn.execute("CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, username TEXT UNIQUE)")
        await conn.execute("CREATE TABLE IF NOT EXISTS operators (group_id INTEGER, user_id INTEGER, PRIMARY KEY (group_id, user_id), FOREIGN KEY (user_id) REFERENCES users(user_id))")
        await conn.execute("CREATE TABLE IF NOT EXISTS previous_balances (id INTEGER PRIMARY KEY AUTOINCREMENT, group_id INTEGER, amount DECIMAL(10,0), created_at TEXT)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_group_active ON cycles (group_id, is_active)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_cycle_group ON bills (cycle_id, group_id)")
        await conn.commit()

# --- 权限与数据辅助函数 ---

async def is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    admins_cache_key = f"admins_{chat_id}"
    
    cache_entry = context.chat_data.get(admins_cache_key)
    if not cache_entry or (datetime.now() - cache_entry.get('timestamp', datetime.min)).total_seconds() > 600:
        try:
            admins = await context.bot.get_chat_administrators(chat_id)
            context.chat_data[admins_cache_key] = {
                'admins': {admin.user.id for admin in admins},
                'timestamp': datetime.now()
            }
        except Exception as e:
            logging.error(f"Error fetching admin list for chat {chat_id}: {e}")
            if cache_entry:
                return user_id in cache_entry.get('admins', set())
            return False
    
    return user_id in context.chat_data[admins_cache_key].get('admins', set())

async def is_operator(group_id: int, user_id: int) -> bool:
    db_path = get_db_path(group_id)
    async with aiosqlite.connect(db_path) as conn:
        async with conn.execute("SELECT 1 FROM operators WHERE group_id = ? AND user_id = ?", (group_id, user_id)) as cursor:
            return bool(await cursor.fetchone())

async def is_authorized_user(update: Update, context: ContextTypes.DEFAULT_TYPE, group_id: int, user_id: int) -> bool:
    return await is_admin(update, context) or await is_operator(group_id, user_id)

async def get_active_cycle(group_id: int, context: ContextTypes.DEFAULT_TYPE):
    cache_key = f"active_cycle_{group_id}"
    if cache_key in context.bot_data:
        return context.bot_data[cache_key]
    
    db_path = get_db_path(group_id)
    async with aiosqlite.connect(db_path) as conn:
        async with conn.execute("SELECT cycle_id FROM cycles WHERE group_id = ? AND is_active = TRUE", (group_id,)) as cursor:
            cycle = await cursor.fetchone()
            cycle_id = cycle[0] if cycle else None
            if cycle_id: context.bot_data[cache_key] = cycle_id
            return cycle_id

async def get_previous_balance(group_id: int):
    db_path = get_db_path(group_id)
    async with aiosqlite.connect(db_path) as conn:
        async with conn.execute("SELECT amount FROM previous_balances WHERE group_id = ? ORDER BY id DESC LIMIT 1", (group_id,)) as cursor:
            result = await cursor.fetchone()
            return int(result[0] or 0) if result else 0

async def record_user(user: Update.effective_user, group_id: int):
    if not user or not user.username: return
    db_path = get_db_path(group_id)
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute(
            "INSERT INTO users (user_id, username) VALUES (?, ?) ON CONFLICT(user_id) DO UPDATE SET username=excluded.username",
            (user.id, f"@{user.username}")
        )
        await conn.commit()

# --- 核心逻辑：账单汇总与格式化 ---

async def get_cycle_summary(conn: aiosqlite.Connection, cycle_id: int) -> dict:
    summary = {}
    query = """
        SELECT
            COALESCE(SUM(CASE WHEN amount > 0 AND description NOT LIKE '[结余]%' THEN amount END), 0),
            COALESCE(COUNT(CASE WHEN amount > 0 AND description NOT LIKE '[结余]%' THEN 1 END), 0),
            COALESCE(SUM(CASE WHEN amount < 0 AND description NOT LIKE '[结余]%' THEN amount END), 0),
            COALESCE(COUNT(CASE WHEN amount < 0 AND description NOT LIKE '[结余]%' THEN 1 END), 0),
            COALESCE(SUM(CASE WHEN description LIKE '[结余]%' THEN amount END), 0)
        FROM bills
        WHERE cycle_id = ?
    """
    async with conn.execute(query, (cycle_id,)) as c:
        res = await c.fetchone()
        summary['total_deposits'] = int(res[0])
        summary['deposit_count'] = int(res[1])
        summary['total_withdrawals'] = int(abs(res[2]))
        summary['withdrawal_count'] = int(res[3])
        summary['previous_balance'] = int(res[4])

    summary['net_balance'] = summary['total_deposits'] - summary['total_withdrawals'] + summary['previous_balance']
    
    async with conn.execute("SELECT amount, created_at FROM bills WHERE cycle_id = ? AND amount > 0 AND description NOT LIKE '[结余]%' ORDER BY bill_id DESC LIMIT 5", (cycle_id,)) as c:
        summary['deposits'] = await c.fetchall()
    async with conn.execute("SELECT amount, created_at FROM bills WHERE cycle_id = ? AND amount < 0 AND description NOT LIKE '[结余]%' ORDER BY bill_id DESC LIMIT 5", (cycle_id,)) as c:
        summary['withdrawals'] = await c.fetchall()
    return summary

def format_summary_text(summary: dict) -> str:
    deposit_lines = []
    for i, t in enumerate(summary.get('deposits', [])):
        amount = int(t[0])
        line = f"{t[1][11:19]}   <b>{amount}</b>" if i == 0 else f"{t[1][11:19]}   {amount}"
        deposit_lines.append(line)

    withdrawal_lines = []
    for i, t in enumerate(summary.get('withdrawals', [])):
        amount = int(abs(t[0]))
        line = f"{t[1][11:19]}   <b>{amount}</b>" if i == 0 else f"{t[1][11:19]}   {amount}"
        withdrawal_lines.append(line)

    return (
        f"🟢入款 ({summary.get('deposit_count', 0)}笔)\n" + ("\n".join(deposit_lines) or "无记录") + "\n\n"
        f"🔴下发 ({summary.get('withdrawal_count', 0)}笔)\n" + ("\n".join(withdrawal_lines) or "无记录") + "\n\n"
        f"总入: <b>{summary.get('total_deposits', 0)}</b> RMB\n"
        f"总下: <b>{summary.get('total_withdrawals', 0)}</b> RMB\n"
        f"未下: <b>{summary.get('net_balance', 0)}</b> RMB"
    )

def get_group_lock(context: ContextTypes.DEFAULT_TYPE, group_id: int) -> asyncio.Lock:
    if 'group_locks' not in context.bot_data: context.bot_data['group_locks'] = {}
    if group_id not in context.bot_data['group_locks']: context.bot_data['group_locks'][group_id] = asyncio.Lock()
    return context.bot_data['group_locks'][group_id]

# --- 指令处理器 ---

async def handle_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text: return
    text = update.message.text.strip()
    group_id = update.effective_chat.id
    user_id = update.effective_user.id
    args = text.split()[1:]
    
    await init_group_db(group_id)
    await record_user(update.effective_user, group_id)
    
    group_lock = get_group_lock(context, group_id)
    db_path = get_db_path(group_id)
    cmd = text.split()[0]

    # ... '上课', '下课', etc. are unchanged ...
    if cmd == "上课":
        async with group_lock:
            if not await is_authorized_user(update, context, group_id, user_id): return await update.message.reply_text("无权限操作。")
            if await get_active_cycle(group_id, context): return await update.message.reply_text("当前已有活跃周期，请先‘下课’。")
            
            async with aiosqlite.connect(db_path) as conn:
                await conn.execute("UPDATE cycles SET is_active = FALSE WHERE group_id = ?", (group_id,))
                await conn.execute("INSERT INTO cycles (group_id, start_time, is_active) VALUES (?, ?, ?)", (group_id, datetime.now().isoformat(), True))
                await conn.commit()
            
            context.bot_data.pop(f"active_cycle_{group_id}", None)
            
            previous_balance = await get_previous_balance(group_id)
            reply_text = "☀️ 新的记账周期已顺利开启！"
            keyboard = []
            if previous_balance != 0:
                reply_text = f"☀️ 新的记账周期已开启！\n\n发现上个周期有结余 **{previous_balance}** RMB，需要现在导入吗？"
                keyboard = [[InlineKeyboardButton("📥 是的，立即导入", callback_data=f"importbalance_{group_id}_{previous_balance}")]]
            
            await update.message.reply_text(reply_text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))

    elif cmd == "下课":
        async with group_lock:
            if not await is_authorized_user(update, context, group_id, user_id): return await update.message.reply_text("无权限操作。")
            cycle_id = await get_active_cycle(group_id, context)
            if not cycle_id: return await update.message.reply_text("当前没有活跃周期。")
            
            async with aiosqlite.connect(db_path) as conn:
                summary = await get_cycle_summary(conn, cycle_id)
                net_balance = summary['net_balance']
            
                await conn.execute("BEGIN TRANSACTION")
                try:
                    await conn.execute("UPDATE cycles SET is_active = FALSE, end_time = ? WHERE cycle_id = ?", (datetime.now().isoformat(), cycle_id))
                    await conn.execute("DELETE FROM previous_balances WHERE group_id = ?", (group_id,))
                    if net_balance != 0:
                         await conn.execute("INSERT INTO previous_balances (group_id, amount, created_at) VALUES (?, ?, ?)", (group_id, net_balance, datetime.now().isoformat()))
                    cleanup_msg = "\n✅本周期账单已存档。"
                    await conn.commit()
                except Exception as e:
                    await conn.rollback()
                    logging.error(f"Transaction failed in '下课' for group {group_id}: {e}")
                    return await update.message.reply_text("处理失败，数据已回滚。")

            context.bot_data.pop(f"active_cycle_{group_id}", None)
            
            reply_text = (
                f" ✅当前记账周期已结束！\n\n"
                f"本次账目汇总如下：\n"
                f"总入: {summary['total_deposits']} RMB\n"
                f"总下: {summary['total_withdrawals']} RMB\n"
                f"**最终未下: {net_balance} RMB**"
                f"{cleanup_msg}"
            )
            await update.message.reply_text(reply_text, parse_mode="Markdown")

    elif re.match(r'^[+-]\d+', cmd):
        if not await is_authorized_user(update, context, group_id, user_id): return
        cycle_id = await get_active_cycle(group_id, context)
        if not cycle_id: return await update.message.reply_text("没有活跃周期，请先‘上课’。")
        
        try:
            amount = int(cmd)
            description = " ".join(args)[:255] or " "
            summary = None
            async with aiosqlite.connect(db_path) as conn:
                await conn.execute("INSERT INTO bills (cycle_id, group_id, user_id, amount, description, created_at) VALUES (?, ?, ?, ?, ?, ?)", (cycle_id, group_id, user_id, amount, description, datetime.now().isoformat()))
                summary = await get_cycle_summary(conn, cycle_id)
                await conn.commit()
            
            if summary:
                keyboard = [[InlineKeyboardButton("📊详细账单", callback_data=f"details_{group_id}_{cycle_id}_1")]]
                await send_robust_reply(
                    update.message,
                    text=format_summary_text(summary),
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )

        except ValueError: await update.message.reply_text("金额格式错误。")

    elif cmd == "结余":
        if not await is_authorized_user(update, context, group_id, user_id): return
        cycle_id = await get_active_cycle(group_id, context)
        if not cycle_id: return await update.message.reply_text("没有活跃周期。")
        
        try:
            amount = int(args[0])
            description = f"[结余] {' '.join(args[1:]) or '上期结余'}"
            summary = None
            async with aiosqlite.connect(db_path) as conn:
                async with conn.execute("SELECT 1 FROM bills WHERE cycle_id = ? AND description LIKE '[结余]%'", (cycle_id,)) as c:
                    if await c.fetchone(): return await update.message.reply_text("已记录结余，勿重复操作。")
                await conn.execute("INSERT INTO bills (cycle_id, group_id, user_id, amount, description, created_at) VALUES (?, ?, ?, ?, ?, ?)", (cycle_id, group_id, user_id, amount, description, datetime.now().isoformat()))
                summary = await get_cycle_summary(conn, cycle_id)
                await conn.commit()
            
            if summary:
                await send_robust_reply(
                    update.message,
                    text=f"✅结余记录成功！\n\n" + format_summary_text(summary),
                    parse_mode="HTML"
                )
        except (ValueError, IndexError): await update.message.reply_text("格式: `结余 +金额` 或 `结余 -金额`", parse_mode="Markdown")

    elif cmd == "撤销":
        if not await is_authorized_user(update, context, group_id, user_id): return
        cycle_id = await get_active_cycle(group_id, context)
        if not cycle_id: return await update.message.reply_text("没有活跃周期。")
        
        summary = None
        last_bill = None
        async with aiosqlite.connect(db_path) as conn:
            async with conn.execute("SELECT bill_id, amount, description FROM bills WHERE cycle_id = ? ORDER BY bill_id DESC LIMIT 1", (cycle_id,)) as c:
                last_bill = await c.fetchone()
            if not last_bill: return await update.message.reply_text("无记录可撤销。")
            await conn.execute("DELETE FROM bills WHERE bill_id = ?", (last_bill[0],))
            summary = await get_cycle_summary(conn, cycle_id)
            await conn.commit()
        
        if summary and last_bill:
            await send_robust_reply(
                update.message,
                text=f"✅已撤销: {last_bill[1]} × {last_bill[2]}\n\n" + format_summary_text(summary),
                parse_mode="HTML"
            )
            
    elif cmd in ["设置操作员", "删除操作员"]:
        if not await is_admin(update, context): return await update.message.reply_text("仅管理员可操作。")
        is_setting = cmd == "设置操作员"
        
        target_user = None
        if update.message.reply_to_message:
            target_user = update.message.reply_to_message.from_user
        elif args and args[0].startswith("@"):
            async with aiosqlite.connect(db_path) as conn:
                async with conn.execute("SELECT user_id FROM users WHERE username = ?", (args[0],)) as c:
                    user_record = await c.fetchone()
            if not user_record: return await update.message.reply_text(f"用户 {args[0]} 未在群内发言过。")
            target_user = SimpleNamespace(id=user_record[0], username=args[0].strip('@'))
        else: return await update.message.reply_text("格式: 回复某人消息或使用 `@username`。")
        
        if not target_user: return await update.message.reply_text("无法确定目标用户。")
        
        await record_user(target_user, group_id)
        
        username_to_display = f"@{target_user.username}" if getattr(target_user, 'username', None) else f"用户ID {target_user.id}"
        async with aiosqlite.connect(db_path) as conn:
            if is_setting:
                await conn.execute("INSERT OR IGNORE INTO operators (group_id, user_id) VALUES (?, ?)", (group_id, target_user.id))
                msg = f"✅ 已将 {username_to_display} 设为操作员。"
            else:
                await conn.execute("DELETE FROM operators WHERE group_id = ? AND user_id = ?", (group_id, target_user.id))
                msg = f"✅ 已移除 {username_to_display} 的操作员权限。"
            await conn.commit()
        await update.message.reply_text(msg)

    elif cmd == "当前操作员":
        if not await is_admin(update, context): return await update.message.reply_text("仅管理员可查看。")
        async with aiosqlite.connect(db_path) as conn:
            async with conn.execute("SELECT u.username FROM operators o JOIN users u ON o.user_id = u.user_id WHERE o.group_id = ?", (group_id,)) as c:
                operators = await c.fetchall()
        if not operators: await update.message.reply_text("当前没有操作员。")
        else: await update.message.reply_text("当前操作员：\n" + "\n".join([op[0] for op in operators]))
    
    elif cmd == "帮助":
        await update.message.reply_text(
            "📖 **记账机器人 - 快速入门**\n\n"
            "**三步搞定记账:**\n"
            "1️⃣ 发送 `上课` → 开启新账本\n"
            "2️⃣ 开始记账 → `+1000` (入款), `-500` (下发)\n"
            "3️⃣ 发送 `下课` → 结算本日账目\n\n"
            "--- **所有指令** ---\n\n"
            "**记账操作** (管理员/操作员)\n"
            "☀️ `上课` → 开始新一轮记账\n"
            "🌙 `下课` → 结束本轮, 生成总结\n"
            "🟢 `+100` → 记录一笔**入款**\n"
            "🔴 `-50`  → 记录一笔**下发**\n"
            "💰 `结余 +1000` → 录入上一轮的结余\n"
            "↩️ `撤销` → 删掉**最后一条**记录\n\n"
            "**管理操作** (仅管理员)\n"
            "➕ `设置操作员` → (回复/`@`) 设为记账员\n"
            "➖ `删除操作员` → (回复/`@`) 取消记账员\n"
            "👥 `当前操作员` → 查看记账员列表\n\n"
            "💡 **小提示:**\n"
            " ▸ 所有记账都可加备注, 如: `+5000 张三`\n"
            " ▸ 每个群组的账本和人员都完全独立。",
            parse_mode='Markdown'
        )

# --- 回调处理器 ---

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    # [修改] 使用新的可靠函数应答，这是第一步网络操作
    await robust_answer(query)
    
    data = query.data.split("_")
    action = data[0]
    
    if action == "details":
        try:
            group_id, cycle_id, page = map(int, data[1:])
            db_path = get_db_path(group_id)
            async with aiosqlite.connect(db_path) as conn:
                summary = await get_cycle_summary(conn, cycle_id)
                
                async with conn.execute("SELECT COUNT(*) FROM bills WHERE cycle_id = ?", (cycle_id,)) as c: total_items = (await c.fetchone())[0]
                async with conn.execute("SELECT COUNT(*) FROM bills WHERE cycle_id = ? AND description LIKE '[结余]%'", (cycle_id,)) as c: balance_count = (await c.fetchone())[0]

                items_per_page = 10
                offset = (page - 1) * items_per_page
                async with conn.execute("SELECT amount, description, created_at FROM bills WHERE cycle_id = ? ORDER BY bill_id DESC LIMIT ? OFFSET ?", (cycle_id, items_per_page, offset)) as c: bills = await c.fetchall()

            total_pages = math.ceil(total_items / items_per_page) if total_items > 0 else 1
            end_time_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            bill_lines = [f"{'⚖️' if d.startswith('[结余]') else ('🟢' if a > 0 else '🔴')} {t[11:16]} | {int(a):>8} | {d if d.strip() else ''}" for a, d, t in bills]
            bill_lines_str = "\n".join(bill_lines) if bill_lines else "无记录"
            reply_text = (
                f"⏰截止时间: {end_time_str}\n"
                f"💳昨日未下: {summary['previous_balance']} RMB\n"
                f"💰当前未下: <b>{summary['net_balance']}</b> RMB\n"
                f"📌(总 {total_items} 笔, 入款 {summary['deposit_count']} 笔, 下发 {summary['withdrawal_count']} 笔, 结余 {balance_count} 笔)\n"
                f"<b>📊 账单详情 - 第 {page} / 共 {total_pages} 页</b>\n"
                f"<pre>{bill_lines_str}</pre>"
            )
            nav_buttons = []
            if page > 1: nav_buttons.append(InlineKeyboardButton("⬅️ 上一页", callback_data=f"details_{group_id}_{cycle_id}_{page-1}"))
            if page < total_pages: nav_buttons.append(InlineKeyboardButton("下一页 ➡️", callback_data=f"details_{group_id}_{cycle_id}_{page+1}"))
            
            # [修改] 使用新的可靠函数编辑消息，这是第二步网络操作
            await robust_edit_message_text(
                query,
                text=reply_text,
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([nav_buttons])
            )
        except Exception as e: 
            # 这个异常现在只会在数据库查询等非网络部分出错时触发
            logging.error(f"Error processing 'details' callback logic for query {query.data}: {e}", exc_info=True)


    elif action == "importbalance":
        try:
            group_id, amount = map(int, data[1:])
            user_id = update.effective_user.id
            if not await is_authorized_user(update, context, group_id, user_id): return
            cycle_id = await get_active_cycle(group_id, context)
            if not cycle_id: 
                await robust_edit_message_text(query, text=f"{query.message.text}\n\n⚠️失败：没有活跃周期。")
                return

            summary = None
            async with aiosqlite.connect(get_db_path(group_id)) as conn:
                async with conn.execute("SELECT 1 FROM bills WHERE cycle_id = ? AND description LIKE '[结余]%'", (cycle_id,)) as c:
                    if await c.fetchone():
                        await robust_edit_message_text(query, text=f"{query.message.text}\n\n⚠️结余已存在，请勿重复操作。", reply_markup=None)
                        return
                await conn.execute("INSERT INTO bills (cycle_id, group_id, user_id, amount, description, created_at) VALUES (?, ?, ?, ?, ?, ?)", (cycle_id, group_id, user_id, amount, "[结余] 自动导入", datetime.now().isoformat()))
                summary = await get_cycle_summary(conn, cycle_id)
                await conn.commit()
            
            await robust_edit_message_text(query, text=f"{query.message.text.splitlines()[0]}\n\n✅ 结余 **{amount}** RMB 已成功导入！", parse_mode="Markdown")
            
            if summary:
                keyboard = [[InlineKeyboardButton("📊详细账单", callback_data=f"details_{group_id}_{cycle_id}_1")]]
                await send_robust_reply(
                    query.message,
                    text=format_summary_text(summary),
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )

        except Exception as e:
            logging.error(f"Error in importbalance callback logic for query {query.data}: {e}", exc_info=True)
            await send_robust_reply(query.message, text="导入结余失败！")

# --- 主函数 ---

def main():
    token = os.getenv("BOT_TOKEN", "8397896614:AAGRvgRO-RjaesarwQ6KJhi4U1x--262OdM")
    if not token:
        logging.critical("BOT_TOKEN 未设置！")
        exit(1)
    
    # 增加网络超时时间，作为第一道防线
    request = HTTPXRequest(
        connect_timeout=15.0, # 稍微增加连接超时
        read_timeout=30.0,    # 读取超时可以长一些
        pool_timeout=60.0
    )

    builder = Application.builder().token(token).request(request)
    app = builder.build()
    
    command_pattern = r'^([+-]\d+|上课|下课|设置操作员|删除操作员|当前操作员|帮助|结余|撤销)(\s.*)?$'
    app.add_handler(MessageHandler(
        filters.Regex(command_pattern) & filters.ChatType.GROUPS,
        handle_command
    ))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_error_handler(error_handler)
    
    logging.info("机器人已启动 (全面网络容错版)")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        logging.critical("程序启动时发生致命错误!", exc_info=True)

