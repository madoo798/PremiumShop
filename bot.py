# pyright: reportOptionalMemberAccess=false
# pyright: reportArgumentType=false
# pyright: reportAttributeAccessIssue=false
# pyright: reportOptionalSubscript=false
# ruff: noqa: BLE001, S110, S112
# ruff: noqa: BLE001, S110, S112, I001
import asyncio
import logging
import os
from threading import Thread
from typing import Any

from aiocryptopay import AioCryptoPay, Networks
from aiogram import Bot, Dispatcher, F, Router, types
from aiogram.filters import Command, CommandObject, Filter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup
from dotenv import load_dotenv
from flask import Flask

# Import database manager from database.py
from database import db

# Initialize proper file logger
logger = logging.getLogger(__name__)

# ==========================================
# RENDER 24/7 HOSTING WORKAROUND
# ==========================================
app = Flask(__name__)

@app.route('/')
def home():
    return "Altalis Storefront Bot is actively running!"

def run_flask():
    # Fix for PLW1508: ensure default fallback is a string before converting
    port = int(os.environ.get('PORT', '10000'))
    app.run(host='0.0.0.0', port=port)

def keep_alive():
    server_thread = Thread(target=run_flask)
    server_thread.daemon = True 
    server_thread.start()

# ==========================================
# 1. SETUP & INITIALIZATION
# ==========================================
load_dotenv()

router = Router()

# Initialize Crypto Pay (@CryptoBot) Client variables
crypto_token = os.getenv("CRYPTOBOT_TOKEN", "YOUR_CRYPTOBOT_TOKEN_HERE")
network_mode = Networks.MAIN_NET if os.getenv("CRYPTO_NETWORK", "MAIN_NET") == "MAIN_NET" else Networks.TEST_NET
# Create global placeholder to prevent 'MainThread' event loop RuntimeError
crypto = None 

# Load IDs and Configurations securely from .env
BINANCE_PAY_ID = os.getenv("BINANCE_PAY_ID", "YOUR_BINANCE_PAY_ID")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
REQUIRED_CHANNEL = os.getenv("REQUIRED_CHANNEL", "")

# Store Configuration Settings (Defaults to manual delivery as requested)
STORE_CONFIG = {
    "delivery_mode": os.getenv("DELIVERY_MODE", "manual").lower()  # Options: "manual" or "auto"
}

# Catalog Pagination Setting
ITEMS_PER_PAGE = 5

# Anti-Spam Rate-Limiting Memory Cache
USER_LAST_ACTION = {}


# ==========================================
# 2. CUSTOM MIDDLEWARE FILTERS & FSM STATES
# ==========================================
class IsAdmin(Filter):
    """Custom middleware filter to cleanly secure admin-only commands and callbacks."""
    async def __call__(self, event: types.Message | types.CallbackQuery) -> bool:
        return event.from_user.id == ADMIN_ID and ADMIN_ID != 0


class ThrottlingFilter(Filter):
    """Anti-spam flood protection filter (limits actions to 1 per second per user)."""
    async def __call__(self, event: types.Message | types.CallbackQuery) -> bool:
        user_id = event.from_user.id
        # Exempt admin from throttling restrictions
        if user_id == ADMIN_ID:
            return True
            
        current_time = asyncio.get_event_loop().time()
        last_time = USER_LAST_ACTION.get(user_id, 0.0)
        
        if current_time - last_time < 1.0:
            if isinstance(event, types.CallbackQuery):
                await event.answer("⚠️ Please slow down! You are clicking too fast.", show_alert=True)
            return False
            
        USER_LAST_ACTION[user_id] = current_time
        return True


class TopUpStates(StatesGroup):
    """Tracks user input states for custom amounts and manual payment receipts."""
    waiting_for_custom_amount = State()
    waiting_for_binance_txid = State()


# ==========================================
# 3. DATABASE, SUBSCRIPTION, & BROADCAST HELPERS
# ==========================================
def ensure_db_upgrades():
    """Silently upgrades the SQLite database schema to support warranties, categories, orders, restocks, and referrals."""
    try:
        with db._get_connection() as conn:
            try:
                conn.execute("ALTER TABLE products ADD COLUMN warranty TEXT DEFAULT 'None'")
            except Exception:
                pass
            try:
                conn.execute("ALTER TABLE products ADD COLUMN category TEXT DEFAULT 'General'")
            except Exception:
                pass
            try:
                conn.execute("ALTER TABLE users ADD COLUMN referred_by INTEGER DEFAULT NULL")
            except Exception:
                pass
            
            conn.execute("""
                CREATE TABLE IF NOT EXISTS restock_subscribers (
                    product_id INTEGER,
                    user_id INTEGER,
                    PRIMARY KEY (product_id, user_id)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS customer_orders (
                    order_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    product_name TEXT,
                    price_usd REAL,
                    deliverable TEXT,
                    purchased_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
    except Exception as e:
        logger.error(f"Database upgrade check failed: {e}")

def set_product_warranty(product_id: int, warranty_text: str) -> bool:
    """Updates the custom warranty duration for a specific product."""
    with db._get_connection() as conn:
        cursor = conn.execute("UPDATE products SET warranty = ? WHERE product_id = ?", (warranty_text, product_id))
        return cursor.rowcount > 0

def set_product_category(product_id: int, category_name: str) -> bool:
    """Updates the catalog category folder for a specific product."""
    with db._get_connection() as conn:
        cursor = conn.execute("UPDATE products SET category = ? WHERE product_id = ?", (category_name, product_id))
        return cursor.rowcount > 0

def get_active_products() -> list[dict[str, Any]]:
    """Query the SQLite database for all currently active store products."""
    with db._get_connection() as conn:
        cursor = conn.execute("SELECT * FROM products WHERE is_active = 1")
        cols = [col[0] for col in cursor.description]
        return [dict(zip(cols, row)) for row in cursor.fetchall()]

def get_all_categories() -> list[str]:
    """Retrieve all unique product category folders currently active in the store."""
    with db._get_connection() as conn:
        cursor = conn.execute("SELECT DISTINCT category FROM products WHERE is_active = 1")
        cats = [row[0] for row in cursor.fetchall() if row[0]]
        return cats if cats else ["General"]

def get_products_by_category(category_name: str) -> list[dict[str, Any]]:
    """Query active products belonging to a specific category."""
    with db._get_connection() as conn:
        cursor = conn.execute("SELECT * FROM products WHERE is_active = 1 AND category = ?", (category_name,))
        cols = [col[0] for col in cursor.description]
        return [dict(zip(cols, row)) for row in cursor.fetchall()]

def add_restock_subscriber(product_id: int, user_id: int) -> bool:
    """Subscribe a user to receive an instant DM alert when an out-of-stock item is restocked."""
    try:
        with db._get_connection() as conn:
            conn.execute("INSERT OR IGNORE INTO restock_subscribers (product_id, user_id) VALUES (?, ?)", (product_id, user_id))
            return True
    except Exception as e:
        logger.error(f"Failed to add restock subscriber: {e}")
        return False

def get_and_clear_restock_subscribers(product_id: int) -> list[int]:
    """Retrieve all users waiting for a restock alert and clear them from the queue."""
    with db._get_connection() as conn:
        cursor = conn.execute("SELECT user_id FROM restock_subscribers WHERE product_id = ?", (product_id,))
        users = [row[0] for row in cursor.fetchall()]
        if users:
            conn.execute("DELETE FROM restock_subscribers WHERE product_id = ?", (product_id,))
        return users

def log_customer_order(user_id: int, product_name: str, price_usd: float, deliverable: str):
    """Record a completed order into the customer's permanent purchase history."""
    try:
        with db._get_connection() as conn:
            conn.execute(
                "INSERT INTO customer_orders (user_id, product_name, price_usd, deliverable) VALUES (?, ?, ?, ?)",
                (user_id, product_name, price_usd, deliverable)
            )
    except Exception as e:
        logger.error(f"Failed to log customer order: {e}")

def get_customer_orders(user_id: int) -> list[dict[str, Any]]:
    """Retrieve all past purchases for a specific customer."""
    with db._get_connection() as conn:
        cursor = conn.execute("SELECT * FROM customer_orders WHERE user_id = ? ORDER BY purchased_at DESC", (user_id,))
        cols = [col[0] for col in cursor.description]
        return [dict(zip(cols, row)) for row in cursor.fetchall()]

def get_store_analytics() -> dict[str, Any]:
    """Calculate core store analytics for the admin dashboard."""
    with db._get_connection() as conn:
        rev_cursor = conn.execute("SELECT SUM(price_usd), COUNT(*) FROM customer_orders")
        rev_row = rev_cursor.fetchone()
        total_revenue = rev_row[0] if rev_row and rev_row[0] else 0.0
        total_orders = rev_row[1] if rev_row and rev_row[1] else 0
        
        user_cursor = conn.execute("SELECT COUNT(*) FROM users")
        user_row = user_cursor.fetchone()
        total_users = user_row[0] if user_row and user_row[0] else 0
        
        prod_cursor = conn.execute("SELECT COUNT(*) FROM products WHERE is_active = 1")
        prod_row = prod_cursor.fetchone()
        total_products = prod_row[0] if prod_row and prod_row[0] else 0
        
        return {
            "revenue": total_revenue,
            "orders": total_orders,
            "users": total_users,
            "products": total_products
        }

def add_user_balance(user_id: int, amount_usd: float) -> float:
    """Directly add funds to a user's wallet balance and return the new total."""
    with db._get_connection() as conn:
        conn.execute(
            "UPDATE users SET balance_usd = balance_usd + ? WHERE user_id = ?",
            (amount_usd, user_id)
        )
        conn.commit()
        cursor = conn.execute("SELECT balance_usd FROM users WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        return row[0] if row else 0.0

def append_product_description(product_id: int, added_text: str) -> bool:
    """Safely append additional information to an already existing product description."""
    with db._get_connection() as conn:
        cursor = conn.execute("SELECT description FROM products WHERE product_id = ?", (product_id,))
        row = cursor.fetchone()
        if not row:
            return False
        current_desc = row[0]
        new_desc = f"{current_desc}\n\n➕ **Update:** {added_text}"
        conn.execute("UPDATE products SET description = ? WHERE product_id = ?", (new_desc, product_id))
        return True

async def check_user_subscription(bot: Bot, user_id: int) -> bool:
    """Check if a user is subscribed to the public channel."""
    if not REQUIRED_CHANNEL:
        return True
    try:
        member = await bot.get_chat_member(chat_id=REQUIRED_CHANNEL, user_id=user_id)
        return member.status in ["member", "administrator", "creator"]
    except Exception as e:
        logger.error(f"Failed to check channel subscription for user {user_id}: {e}")
        return False

async def broadcast_to_users(bot: Bot, text: str, reply_markup: InlineKeyboardMarkup | None = None):
    """Safely broadcast a notification directly to all registered users in their private chat (Non-Blocking)."""
    with db._get_connection() as conn:
        cursor = conn.execute("SELECT user_id FROM users")
        user_ids = [row[0] for row in cursor.fetchall()]
        
    success_count = 0
    for user_id in user_ids:
        try:
            await bot.send_message(
                chat_id=user_id,
                text=text,
                reply_markup=reply_markup,
                parse_mode="Markdown"
            )
            success_count += 1
            await asyncio.sleep(0.05) 
        except Exception:
            continue
            
    logger.info(f"📣 Private DM broadcast successfully delivered to {success_count}/{len(user_ids)} users.")

async def safe_register_user(user_id: int, username: str, referrer_id: int | None = None) -> str:
    """Registers user safely, handles the Turso race condition, applies promos, and securely tracks referrals."""
    existing_user = await asyncio.to_thread(db.get_user, user_id)
    
    if existing_user:
        await asyncio.to_thread(db.add_user, user_id=user_id, username=username)
        return "existing"

    # Get total users BEFORE adding this one
    stats = await asyncio.to_thread(get_store_analytics)
    total_users = stats.get("users", 0)

    # 1. Insert the new user
    await asyncio.to_thread(db.add_user, user_id=user_id, username=username)
    
    # 2. Pause for 0.5s to ensure the Turso cloud DB fully commits the INSERT
    await asyncio.sleep(0.5)

    # 3. Handle Referral Milestone Logic
    if referrer_id and referrer_id != user_id:
        # Verify the referrer is an actual registered user in the DB before crediting any funds
        referrer_data = await asyncio.to_thread(db.get_user, referrer_id)
        
        if referrer_data:
            with db._get_connection() as conn:
                conn.execute("UPDATE users SET referred_by = ? WHERE user_id = ?", (referrer_id, user_id))
                conn.commit()
                
                # Count how many users this person has referred in total
                cursor = conn.execute("SELECT COUNT(*) FROM users WHERE referred_by = ?", (referrer_id,))
                total_referrals = cursor.fetchone()[0]
                
            # If the total referrals is a multiple of 5, give the reward
            if total_referrals > 0 and total_referrals % 5 == 0:
                reward_amount = 0.50 # Reward per 5 users
                await asyncio.to_thread(add_user_balance, user_id=referrer_id, amount_usd=reward_amount)
                return f"rewarded_referral_{referrer_id}_{reward_amount}_{total_referrals}"
            else:
                # Flag to notify them of their progress
                return f"progress_referral_{referrer_id}_{total_referrals}"

    # 4. Apply the promo if eligible
    if total_users < 100:
        await asyncio.to_thread(add_user_balance, user_id=user_id, amount_usd=1.0)
        return "promo"
    
    return "new"

# ==========================================
# 4. KEYBOARD BUILDERS
# ==========================================
def get_persistent_restart_keyboard() -> ReplyKeyboardMarkup:
    """Returns a permanent reply keyboard anchored at the bottom of the screen to quickly restart the bot."""
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="🔄 Restart / Menu")]],
        resize_keyboard=True,
        is_persistent=True
    )

def get_main_menu_keyboard(user_id: int = 0) -> InlineKeyboardMarkup:
    """Returns the primary storefront navigation menu."""
    buttons = [
        [InlineKeyboardButton(text="🛍️ View Catalog", callback_data="menu_catalog")],
        [InlineKeyboardButton(text="💳 Top-Up Wallet Balance", callback_data="menu_topup")],
        [InlineKeyboardButton(text="👤 My Profile & Balance", callback_data="menu_profile")],
        [InlineKeyboardButton(text="🎁 Invite Friends", callback_data="menu_referral")],
        [InlineKeyboardButton(text="❓ Help & Support", callback_data="menu_help")]
    ]
    
    if user_id == ADMIN_ID and ADMIN_ID != 0:
        buttons.append([InlineKeyboardButton(text="👑 Admin", callback_data="menu_admin")])
        
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_admin_panel_keyboard() -> InlineKeyboardMarkup:
    """Returns an interactive dashboard allowing the admin to click and execute commands directly."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📊 Store Analytics", callback_data="admbtn_stats"),
                InlineKeyboardButton(text="💾 DB Backup", callback_data="admbtn_backup")
            ],
            [
                InlineKeyboardButton(text="📋 List Products", callback_data="admbtn_listproducts"),
                InlineKeyboardButton(text="🔄 Delivery Mode", callback_data="admbtn_deliverymode")
            ],
            [
                InlineKeyboardButton(text="➕ Add Product", callback_data="admbtn_addproduct"),
                InlineKeyboardButton(text="📝 Modify Desc", callback_data="admbtn_modifydesc")
            ],
            [
                InlineKeyboardButton(text="🛡️ Set Warranty", callback_data="admbtn_warranty"),
                InlineKeyboardButton(text="📂 Set Category", callback_data="admbtn_category")
            ],
            [
                InlineKeyboardButton(text="📦 Add Stock", callback_data="admbtn_addstock"),
                InlineKeyboardButton(text="📊 List Stock", callback_data="admbtn_liststock")
            ],
            [
                InlineKeyboardButton(text="🧹 Clear Stock", callback_data="admbtn_clearstock"),
                InlineKeyboardButton(text="🗑️ Delete Product", callback_data="admbtn_delproduct")
            ],
            [
                InlineKeyboardButton(text="💳 Credit Wallet", callback_data="admbtn_credit"),
                InlineKeyboardButton(text="🗑️ Delete Stock Key", callback_data="admbtn_delstock")
            ],
            [
                InlineKeyboardButton(text="🧹 Wipe Screen Menu", callback_data="admbtn_clearmenu"),
                InlineKeyboardButton(text="« Return to Main Menu", callback_data="menu_main")
            ]
        ]
    )


def get_topup_keyboard() -> InlineKeyboardMarkup:
    """Returns quick-select deposit amounts, custom amount, and Binance Pay options."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="+$5 USD", callback_data="deposit_5"),
                InlineKeyboardButton(text="+$10 USD", callback_data="deposit_10")
            ],
            [
                InlineKeyboardButton(text="+$25 USD", callback_data="deposit_25"),
                InlineKeyboardButton(text="+$50 USD", callback_data="deposit_50")
            ],
            [InlineKeyboardButton(text="✏️ Enter Custom Amount ($1+)", callback_data="deposit_custom")],
            [InlineKeyboardButton(text="🟡 Pay via Binance Pay (Zero Fees)", callback_data="deposit_binance")],
            [InlineKeyboardButton(text="« Back to Main Menu", callback_data="menu_main")]
        ]
    )


# ==========================================
# 5. AUTOMATED BACKGROUND POLLING & WATCHERS
# ==========================================
async def auto_verify_invoices(bot: Bot):
    """Background loop: Scans pending invoices and automatically credits user balances (Non-Blocking)."""
    logger.info("⏳ Starting Automated Crypto Pay Verification Loop...")
    while True:
        try:
            pending_invoices = await asyncio.to_thread(
                lambda: [{"invoice_id": row[0], "user_id": row[1], "amount_usd": row[2]} for row in db._get_connection().execute("SELECT invoice_id, user_id, amount_usd FROM invoices WHERE status = 'pending'").fetchall()]
            )
            
            for inv in pending_invoices:
                try:
                    invoices = await crypto.get_invoices(invoice_ids=inv["invoice_id"])
                    
                    if not invoices:
                        continue
                    if hasattr(invoices, 'items'):
                        invoices = invoices.items
                    elif not isinstance(invoices, list):
                        invoices = [invoices]

                    for invoice in invoices:
                        if invoice.status == "paid":
                            credited = await asyncio.to_thread(db.mark_invoice_paid, inv["invoice_id"])
                            if credited:
                                user = await asyncio.to_thread(db.get_user, inv["user_id"])
                                new_bal = user["balance_usd"] if user else 0.0
                                await bot.send_message(
                                    chat_id=inv["user_id"],
                                    text=(
                                        f"⚡ **Automatic Payment Verified!**\n\n"
                                        f"Your deposit of `${inv['amount_usd']:.2f} USD` via `@CryptoBot` was detected automatically.\n"
                                        f"**New Wallet Balance:** `${new_bal:.2f} USD`"
                                    ),
                                    reply_markup=InlineKeyboardMarkup(
                                        inline_keyboard=[[InlineKeyboardButton(text="🛍️ Open Catalog", callback_data="menu_catalog")]]
                                    ),
                                    parse_mode="Markdown"
                                )
                                logger.info(f"✅ [AUTO-CREDITED] Invoice {inv['invoice_id']} -> User {inv['user_id']} (+${inv['amount_usd']})")
                
                except Exception:
                    logger.exception(f"CryptoPay Error Details for invoice {inv['invoice_id']}:")
                
                await asyncio.sleep(1)
                
        except Exception:
            logger.exception("Crash Details in background verification loop:")
        
        await asyncio.sleep(15)


async def check_low_stock_watcher(bot: Bot):
    """Background loop: Automatically alerts admin when product stock drops to 3 units or fewer."""
    logger.info("⏳ Starting Low Stock Inventory Watcher...")
    while True:
        await asyncio.sleep(300) # Check every 5 minutes
        if ADMIN_ID == 0:
            continue
        try:
            products = await asyncio.to_thread(get_active_products)
            for p in products:
                pid = p["product_id"]
                stock = await asyncio.to_thread(db.get_stock_count, pid)
                if 0 < stock <= 3:
                    try:
                        await bot.send_message(
                            chat_id=ADMIN_ID,
                            text=f"⚠️ **LOW STOCK WARNING!**\n\nProduct **{p['name']}** (ID: `{pid}`) only has **{stock} unit(s)** remaining in inventory!\n\n*Run* `/addstock {pid} <key>` *to restock.*",
                            parse_mode="Markdown"
                        )
                        # Brief pause to prevent flooding if multiple items are low
                        await asyncio.sleep(2)
                    except Exception:
                        pass
        except Exception as e:
            logger.error(f"Error in low stock watcher: {e}")


# ==========================================
# 6. NAVIGATION & MENU HANDLERS
# ==========================================
@router.message(Command("start"), ThrottlingFilter())
async def cmd_start(message: types.Message, state: FSMContext, command: CommandObject = None):
    """Register user on startup, handle referral deep linking, and display main menu."""
    await state.clear()
    
    await message.answer(
        "📌 *Store navigation anchored. Tap the button at the bottom of your screen anytime to bring the menu back!*",
        reply_markup=get_persistent_restart_keyboard(),
        parse_mode="Markdown"
    )
    
    if REQUIRED_CHANNEL:
        is_subscribed = await check_user_subscription(message.bot, message.from_user.id)
        if not is_subscribed:
            join_keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="📢 Join Channel", url=f"https://t.me/{REQUIRED_CHANNEL.lstrip('@')}")],
                    [InlineKeyboardButton(text="🔄 I Have Subscribed", callback_data="menu_main")]
                ]
            )
            await message.answer(
                "🔒 **Channel Subscription Required**\n\n"
                f"To access the storefront, you must first subscribe to our official channel: {REQUIRED_CHANNEL}\n\n"
                "Please join the channel below and then click **I Have Subscribed** to unlock the bot:",
                reply_markup=join_keyboard,
                parse_mode="Markdown"
            )
            return

    # Extract optional referral ID from deep link (e.g., t.me/bot?start=123456)
    referrer_id = None
    if command and command.args:
        try:
            referrer_id = int(command.args)
        except ValueError:
            pass

    status = await safe_register_user(message.from_user.id, message.from_user.username or "NoUsername", referrer_id)

    # Handle Referral Milestone Reward Notification
    if status.startswith("rewarded_referral_"):
        parts = status.split("_")
        actual_referrer_id = int(parts[2])
        reward_amount = float(parts[3])
        total_refs = int(parts[4])
        
        try:
            await message.bot.send_message(
                chat_id=actual_referrer_id,
                text=(
                    "🎉 **Referral Milestone Reached!**\n\n"
                    f"You have referred a total of **{total_refs} users** to Altalis & Celesta! "
                    f"**${reward_amount:.2f} USD** has been automatically deposited into your wallet balance.\n\n"
                    "Keep sharing your link from the **🎁 Invite Friends** menu to earn more!"
                ),
                parse_mode="Markdown"
            )
        except Exception as e:
            logger.warning(f"Could not send reward notification to referrer {actual_referrer_id}: {e}")
        status = "new"

    # Handle Referral Progress Notification
    elif status.startswith("progress_referral_"):
        parts = status.split("_")
        actual_referrer_id = int(parts[2])
        total_refs = int(parts[3])
        refs_needed = 5 - (total_refs % 5)
        
        try:
            await message.bot.send_message(
                chat_id=actual_referrer_id,
                text=(
                    "🤝 **New Referral Joined!**\n\n"
                    f"Someone just joined using your invite link! You currently have **{total_refs}** referral(s).\n"
                    f"Invite **{refs_needed} more** user(s) to receive your next **$0.50 USD** wallet credit!"
                ),
                parse_mode="Markdown"
            )
        except Exception as e:
            logger.warning(f"Could not send progress notification to referrer {actual_referrer_id}: {e}")
        status = "new"

    if status == "promo":
        welcome_msg = (
            "🎉 **Welcome to Altalis & Celesta!**\n\n"
            "Congratulations! You are one of our first 100 users. "
            "We have credited **$1.00 USD** to your wallet for free! 🎁\n\n"
            "Top up your wallet balance instantly using cryptocurrency via `@CryptoBot` or zero-fee transfers via **Binance Pay**, browse available subscriptions and software keys, and receive instant delivery upon checkout.\n\n"
            "Select an option below to begin:"
        )
    elif status == "new":
        welcome_msg = (
            "👋 **Welcome to the Digital Storefront!**\n\n"
            "Top up your wallet balance instantly using cryptocurrency via `@CryptoBot` or zero-fee transfers via **Binance Pay**, browse available subscriptions and software keys, and receive instant delivery upon checkout.\n\n"
            "Select an option below to begin:"
        )
    else:
        welcome_msg = (
            "👋 **Welcome back to the Digital Storefront!**\n\n"
            "Select an option below to manage your wallet or browse available digital inventory:"
        )
    
    await message.answer(
        welcome_msg,
        reply_markup=get_main_menu_keyboard(message.from_user.id),
        parse_mode="Markdown"
    )

@router.callback_query(F.data == "menu_referral", ThrottlingFilter())
async def cb_menu_referral(callback: types.CallbackQuery, state: FSMContext):
    """Generate and display the user's unique referral link."""
    await state.clear()
    
    bot_info = await callback.bot.get_me()
    ref_link = f"https://t.me/{bot_info.username}?start={callback.from_user.id}"
    
    text = (
        "🤝 **Invite Friends & Earn!**\n\n"
        "Share your unique link below. For every **5 new users** who start the bot using your link, "
        "you will automatically receive **$0.50 USD** directly into your wallet balance!\n\n"
        f"🔗 `{ref_link}`"
    )
    
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="« Back to Main Menu", callback_data="menu_main")]]
    )
    
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="Markdown")

@router.message(Command("restart", "menu", "home"), ThrottlingFilter())
@router.message(F.text == "🔄 Restart / Menu", ThrottlingFilter())
async def cmd_restart(message: types.Message, state: FSMContext):
    """Instantly clears state and re-sends a fresh main menu at the bottom of the chat."""
    await state.clear()
    
    if REQUIRED_CHANNEL:
        is_subscribed = await check_user_subscription(message.bot, message.from_user.id)
        if not is_subscribed:
            join_keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="📢 Join Channel", url=f"https://t.me/{REQUIRED_CHANNEL.lstrip('@')}")],
                    [InlineKeyboardButton(text="🔄 I Have Subscribed", callback_data="menu_main")]
                ]
            )
            await message.answer(
                "🔒 **Channel Subscription Required**\n\n"
                f"To access the storefront, you must first subscribe to our official channel: {REQUIRED_CHANNEL}\n\n"
                "Please join the channel below and then click **I Have Subscribed** to unlock the bot:",
                reply_markup=join_keyboard,
                parse_mode="Markdown"
            )
            return

    await safe_register_user(message.from_user.id, message.from_user.username or "NoUsername")
    
    await message.answer(
        "👋 **Digital Storefront**\n\nSelect an option below to manage your wallet or browse available digital inventory:",
        reply_markup=get_main_menu_keyboard(message.from_user.id),
        parse_mode="Markdown"
    )


@router.message(Command("clearmenu"), ThrottlingFilter())
async def cmd_clear_menu(message: types.Message, state: FSMContext):
    """Permanently removes the bottom gray reply keyboard from the user's screen."""
    await state.clear()
    await message.answer(
        "🧹 **Bottom menu removed!**\n\n"
        "Your old keyboard layout has been wiped. Please type `/start` to open your clean inline storefront.",
        reply_markup=types.ReplyKeyboardRemove(),
        parse_mode="Markdown"
    )


@router.callback_query(F.data == "menu_main", ThrottlingFilter())
async def cb_menu_main(callback: types.CallbackQuery, state: FSMContext):
    """Return to the root main menu after validating subscription."""
    await state.clear()
    
    if REQUIRED_CHANNEL:
        is_subscribed = await check_user_subscription(callback.bot, callback.from_user.id)
        if not is_subscribed:
            await callback.answer("⚠️ You must subscribe to the channel first!", show_alert=True)
            return

    await safe_register_user(callback.from_user.id, callback.from_user.username or "NoUsername")
    
    await callback.message.edit_text(
        "👋 **Digital Storefront**\n\nSelect an option below to manage your wallet or browse available digital inventory:",
        reply_markup=get_main_menu_keyboard(callback.from_user.id),
        parse_mode="Markdown"
    )


@router.callback_query(F.data == "menu_profile", ThrottlingFilter())
async def cb_menu_profile(callback: types.CallbackQuery, state: FSMContext):
    """Display user profile details, current wallet balance, and order history button."""
    await state.clear()
    user = await asyncio.to_thread(db.get_user, callback.from_user.id)
    if not user:
        await safe_register_user(callback.from_user.id, callback.from_user.username or "NoUsername")
        user = await asyncio.to_thread(db.get_user, callback.from_user.id)
        
    balance = user["balance_usd"] if user else 0.0
    
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📦 My Purchase History / Keys", callback_data="menu_orders")],
            [InlineKeyboardButton(text="💳 Add Funds Now", callback_data="menu_topup")],
            [InlineKeyboardButton(text="« Back to Main Menu", callback_data="menu_main")]
        ]
    )
    
    await callback.message.edit_text(
        f"👤 **Customer Account Profile**\n\n"
        f"**Telegram ID:** `{callback.from_user.id}`\n"
        f"**Username:** @{callback.from_user.username or 'N/A'}\n"
        f"**Available Wallet Balance:** `${balance:.2f} USD`\n\n"
        "*All store purchases are deducted directly from your wallet balance.*",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )


@router.callback_query(F.data == "menu_orders", ThrottlingFilter())
async def cb_menu_orders(callback: types.CallbackQuery, state: FSMContext):
    """Display the user's permanent purchase history and digital key deliverables."""
    await state.clear()
    orders = await asyncio.to_thread(get_customer_orders, callback.from_user.id)
    
    if not orders:
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="« Back to Profile", callback_data="menu_profile")]]
        )
        await callback.message.edit_text(
            "📦 **My Purchase History**\n\nYou haven't purchased any items yet! Visit the catalog to explore available digital goods.",
            reply_markup=keyboard,
            parse_mode="Markdown"
        )
        return
        
    text = "📦 **Your Permanent Order History & Keys:**\n\n"
    for o in orders[:10]: # Show last 10 orders
        text += f"• **{o['product_name']}** (${o['price_usd']:.2f})\n  🔑 Key: `{o['deliverable']}`\n  📅 *{o['purchased_at']}*\n---\n"
        
    if len(orders) > 10:
        text += "\n*Showing your 10 most recent purchases.*"
        
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="« Back to Profile", callback_data="menu_profile")]]
    )
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="Markdown")


@router.callback_query(F.data == "menu_topup", ThrottlingFilter())
async def cb_menu_topup(callback: types.CallbackQuery, state: FSMContext):
    """Display balance top-up options and clear any active input states."""
    await state.clear()
    await callback.message.edit_text(
        "💳 **Top-Up Wallet Balance**\n\n"
        "Select a deposit method or quick-select an amount below. Payments via `@CryptoBot` are automatically verified in the background, while **Binance Pay** allows direct fee-free P2P transfers:",
        reply_markup=get_topup_keyboard(),
        parse_mode="Markdown"
    )


# ==========================================
# 7. CUSTOM AMOUNT & CRYPTO PAY INTEGRATION
# ==========================================
@router.callback_query(F.data == "deposit_custom", ThrottlingFilter())
async def cb_deposit_custom(callback: types.CallbackQuery, state: FSMContext):
    """Prompt the user to input a custom deposit amount."""
    await state.set_state(TopUpStates.waiting_for_custom_amount)
    
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="« Cancel & Return", callback_data="menu_topup")]]
    )
    
    await callback.message.edit_text(
        "✏️ **Enter Custom Deposit Amount**\n\n"
        "Please type and send the amount in USD you wish to deposit into your store wallet.\n\n"
        "• **Minimum Amount:** `$1.00 USD`\n"
        "• **Example Input:** `15` or `25.50`",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )


@router.message(TopUpStates.waiting_for_custom_amount, ThrottlingFilter())
async def process_custom_amount(message: types.Message, state: FSMContext):
    """Validate user input for custom amount and initiate Crypto Pay invoice."""
    try:
        amount = float(message.text.strip())
        if amount < 1.0:
            await message.answer(
                "⚠️ **Amount Too Low**\n\nThe minimum deposit amount is `$1.00 USD`. Please enter a valid amount (e.g., `5` or `12.50`):", 
                parse_mode="Markdown"
            )
            return
            
        await state.clear()
        
        status_msg = await message.answer(f"⏳ Generating secure payment link for `${amount:.2f} USD`...", parse_mode="Markdown")
        
        invoice = await crypto.create_invoice(
            amount=amount,
            fiat="USD",
            currency_type="fiat"
        )
        
        await asyncio.to_thread(
            db.create_invoice,
            invoice_id=invoice.invoice_id,
            user_id=message.from_user.id,
            amount_usd=amount
        )
        
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="💳 Pay via @CryptoBot", url=invoice.bot_invoice_url)],
                [InlineKeyboardButton(text="🔄 Manual Verify Payment", callback_data=f"check_{invoice.invoice_id}")],
                [InlineKeyboardButton(text="« Cancel & Return", callback_data="menu_topup")]
            ]
        )
        
        await status_msg.edit_text(
            "⚡ **Deposit Invoice Generated**\n\n"
            f"**Deposit Amount:** `${amount:.2f} USD`\n"
            f"**Invoice ID:** `{invoice.invoice_id}`\n\n"
            "1️⃣ Click **Pay via @CryptoBot** to open the payment gateway.\n"
            "2️⃣ Complete the transaction using your preferred cryptocurrency.\n"
            "3️⃣ Our background system will automatically detect the transfer and credit your balance within 15 seconds!",
            reply_markup=keyboard,
            parse_mode="Markdown"
        )
    except ValueError:
        await message.answer("❌ **Invalid Format**\n\nPlease enter only a numerical value (for example: `10` or `15.50`):", parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Failed to generate custom invoice: {e}")
        await message.answer("❌ Gateway error. Please return to the menu and try again.")


@router.callback_query(F.data.startswith("deposit_") & (F.data != "deposit_custom") & (F.data != "deposit_binance"), ThrottlingFilter())
async def cb_deposit_quick(callback: types.CallbackQuery, state: FSMContext):
    """Generate a live fiat invoice via Crypto Pay API for quick-select amounts."""
    await state.clear()
    amount = float(callback.data.split("_")[1])
    
    await callback.answer("⏳ Generating secure payment link...")
    
    try:
        invoice = await crypto.create_invoice(
            amount=amount,
            fiat="USD",
            currency_type="fiat"
        )
        
        await asyncio.to_thread(
            db.create_invoice,
            invoice_id=invoice.invoice_id,
            user_id=callback.from_user.id,
            amount_usd=amount
        )
        
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="💳 Pay via @CryptoBot", url=invoice.bot_invoice_url)],
                [InlineKeyboardButton(text="🔄 Manual Verify Payment", callback_data=f"check_{invoice.invoice_id}")],
                [InlineKeyboardButton(text="« Cancel & Return", callback_data="menu_topup")]
            ]
        )
        
        await callback.message.edit_text(
            "⚡ **Deposit Invoice Generated**\n\n"
            f"**Deposit Amount:** `${amount:.2f} USD`\n"
            f"**Invoice ID:** `{invoice.invoice_id}`\n\n"
            "1️⃣ Click **Pay via @CryptoBot** to open the payment gateway.\n"
            "2️⃣ Complete the transaction using your preferred cryptocurrency.\n"
            "3️⃣ Our background system will automatically detect the transfer and credit your balance within 15 seconds!",
            reply_markup=keyboard,
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Failed to create Crypto Pay invoice: {e}")
        await callback.answer("❌ Gateway error. Please try again in a moment.", show_alert=True)


@router.callback_query(F.data.startswith("check_"), ThrottlingFilter())
async def cb_check_payment(callback: types.CallbackQuery):
    """Manual fallback to query API status and credit user wallet if paid."""
    invoice_id = int(callback.data.split("_")[1])
    
    try:
        invoices = await crypto.get_invoices(invoice_ids=invoice_id)
        if not invoices:
            await callback.answer("❌ Invoice record not found on gateway.", show_alert=True)
            return
            
        status = invoices[0].status if isinstance(invoices, list) else invoices.status
        
        if status == "paid":
            credited = await asyncio.to_thread(db.mark_invoice_paid, invoice_id)
            user = await asyncio.to_thread(db.get_user, callback.from_user.id)
            new_balance = user["balance_usd"] if user else 0.0
            
            if credited:
                await callback.answer("✅ Payment Confirmed! Funds added to your balance.", show_alert=True)
                await callback.message.edit_text(
                    "🎉 **Deposit Successful!**\n\n"
                    "Your transaction was verified by `@CryptoBot` and funds have been credited.\n"
                    f"**New Wallet Balance:** `${new_balance:.2f} USD`",
                    reply_markup=InlineKeyboardMarkup(
                        inline_keyboard=[
                            [InlineKeyboardButton(text="🛍️ View Catalog", callback_data="menu_catalog")],
                            [InlineKeyboardButton(text="« Return to Main Menu", callback_data="menu_main")]
                        ]
                    ),
                    parse_mode="Markdown"
                )
            else:
                await callback.answer("⚠️ This invoice has already been credited to your account.", show_alert=True)
        else:
            await callback.answer("⏳ Payment not detected yet. Please complete the transfer and try again.", show_alert=True)
    except Exception as e:
        logger.error(f"Error verifying invoice ID {invoice_id}: {e}")
        await callback.answer("❌ Unable to check status right now. Try again shortly.", show_alert=True)


# ==========================================
# 8. BINANCE PAY MANUAL & AUTOMATED APPROVAL
# ==========================================
@router.callback_query(F.data == "deposit_binance", ThrottlingFilter())
async def cb_deposit_binance(callback: types.CallbackQuery, state: FSMContext):
    """Display instructions for manual fee-free transfers via Binance Pay."""
    await state.clear()
    
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📝 Submit TXID & Amount", callback_data="binance_submit_txid")],
            [InlineKeyboardButton(text="« Return to Top-Up Menu", callback_data="menu_topup")]
        ]
    )
    
    await callback.message.edit_text(
        "🟡 **Binance Pay Deposit Gateway**\n\n"
        "Top up your wallet balance fee-free using Binance Pay P2P transfers!\n\n"
        "1️⃣ Open your Binance App and tap **Binance Pay** (or Scan/Send).\n"
        "2️⃣ Transfer your desired amount (Minimum: `$1.00 USD` equivalent in USDT, USDC, or BNB) to our official **Binance Pay ID**:\n"
        f"`{BINANCE_PAY_ID}`\n"
        "*(Tap the ID above to copy it instantly)*\n\n"
        "3️⃣ Once the transfer is complete, tap **Submit TXID & Amount** below so our automated approval queue can verify and credit your balance!",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )


@router.callback_query(F.data == "binance_submit_txid", ThrottlingFilter())
async def cb_binance_submit_txid(callback: types.CallbackQuery, state: FSMContext):
    """Prompt user to send their Binance Pay transaction ID and sent amount."""
    await state.set_state(TopUpStates.waiting_for_binance_txid)
    
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="« Cancel & Return", callback_data="deposit_binance")]]
    )
    
    await callback.message.edit_text(
        "📝 **Submit Binance Pay Receipt**\n\n"
        "Please paste and send your **Binance Pay Order ID (or TXID)** AND the **Exact Amount Sent** in USD.\n\n"
        "*Example:* `Order ID: 192837465019 - Sent $15 USDT`",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )


@router.message(TopUpStates.waiting_for_binance_txid, ThrottlingFilter())
async def process_binance_txid(message: types.Message, state: FSMContext):
    """Receive Binance TXID from user and send interactive 1-tap credit buttons to Admin."""
    txid_info = message.text.strip()
    user_id = message.from_user.id
    username = message.from_user.username or "NoUsername"
    
    await state.clear()
    
    # Notify User
    await message.answer(
        "✅ **Verification Submitted!**\n\n"
        "Your receipt details have been sent to our automated processing queue:\n"
        f"`{txid_info}`\n\n"
        "Your wallet balance will be credited automatically as soon as the transfer is confirmed.\n\n"
        "💡 *Need urgent assistance? Reach out directly to **@MadVichi**.*",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="💬 Contact Support (@MadVichi)", url="https://t.me/MadVichi")],
                [InlineKeyboardButton(text="🛍️ View Catalog", callback_data="menu_catalog")],
                [InlineKeyboardButton(text="« Return to Main Menu", callback_data="menu_main")]
            ]
        ),
        parse_mode="Markdown"
    )
    
    # Send interactive 1-tap approval buttons to Store Admin
    if ADMIN_ID > 0:
        admin_kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="✅ +$5", callback_data=f"credit_{user_id}_5"),
                    InlineKeyboardButton(text="✅ +$10", callback_data=f"credit_{user_id}_10"),
                    InlineKeyboardButton(text="✅ +$15", callback_data=f"credit_{user_id}_15")
                ],
                [
                    InlineKeyboardButton(text="✅ +$25", callback_data=f"credit_{user_id}_25"),
                    InlineKeyboardButton(text="✅ +$50", callback_data=f"credit_{user_id}_50"),
                    InlineKeyboardButton(text="✅ +$100", callback_data=f"credit_{user_id}_100")
                ],
                [InlineKeyboardButton(text="❌ Dismiss / Ignore", callback_data="dismiss_binance")]
            ]
        )
        
        await message.bot.send_message(
            chat_id=ADMIN_ID,
            text=(
                "🟡 **New Binance Pay Deposit Request**\n\n"
                f"**User:** @{username} (`{user_id}`)\n"
                "**Submitted Details:**\n"
                f"`{txid_info}`\n\n"
                "👉 *Click a button below to automatically credit the user's balance and notify them instantly:*\n"
                "*(For custom amounts, use `/credit {user_id} <amount>`)*"
            ),
            reply_markup=admin_kb,
            parse_mode="Markdown"
        )


@router.callback_query(F.data.startswith("credit_"), IsAdmin(), ThrottlingFilter())
async def cb_admin_quick_credit(callback: types.CallbackQuery):
    """Admin handler: Instantly adds balance to user from the interactive Telegram button."""
    parts = callback.data.split("_")
    target_user_id = int(parts[1])
    amount = float(parts[2])
    
    new_bal = await asyncio.to_thread(add_user_balance, user_id=target_user_id, amount_usd=amount)
    
    # Notify Admin
    await callback.message.edit_text(
        "✅ **Deposit Approved & Credited!**\n\n"
        f"Added `${amount:.2f} USD` to User ID `{target_user_id}`.\n"
        f"**User's New Balance:** `${new_bal:.2f} USD`",
        parse_mode="Markdown"
    )
    
    # Send instant confirmation to Customer
    try:
        await callback.bot.send_message(
            chat_id=target_user_id,
            text=(
                "🎉 **Binance Pay Deposit Approved!**\n\n"
                f"We have verified your transfer and credited `${amount:.2f} USD` to your account.\n"
                f"**New Wallet Balance:** `${new_bal:.2f} USD`\n\n"
                "You can now purchase items from the catalog!"
            ),
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="🛍️ Open Store Catalog", callback_data="menu_catalog")]]
            ),
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.warning(f"Could not notify user {target_user_id} about credit: {e}")


@router.callback_query(F.data == "dismiss_binance", IsAdmin(), ThrottlingFilter())
async def cb_dismiss_binance(callback: types.CallbackQuery):
    """Admin handler: Dismisses an invalid Binance Pay receipt."""
    await callback.message.edit_text("❌ **Receipt Dismissed.** No funds were added.")


# ==========================================
# 9. MULTI-TIER CATEGORIES & PAGINATED CATALOG
# ==========================================
@router.callback_query(F.data == "menu_catalog", ThrottlingFilter())
async def cb_menu_catalog(callback: types.CallbackQuery, state: FSMContext):
    """Display root catalog category folders to cleanly organize inventory."""
    await state.clear()
    
    if REQUIRED_CHANNEL:
        is_subscribed = await check_user_subscription(callback.bot, callback.from_user.id)
        if not is_subscribed:
            await callback.answer("⚠️ You must subscribe to the channel first!", show_alert=True)
            return

    categories = await asyncio.to_thread(get_all_categories)
    
    buttons = []
    for cat in categories:
        buttons.append([InlineKeyboardButton(
            text=f"📂 {cat}",
            callback_data=f"catview:{cat}:0"
        )])
        
    buttons.append([InlineKeyboardButton(text="✖️ ↩️ To Main Menu", callback_data="menu_main")])
    
    await callback.message.edit_text(
        "🛍️ **Digital Inventory Catalog**\n\nSelect a product category folder below to browse available items:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="Markdown"
    )


@router.callback_query(F.data.startswith("catview:"), ThrottlingFilter())
async def cb_view_category_page(callback: types.CallbackQuery, state: FSMContext):
    """Display paginated list of products inside a specific category folder (5 items per page)."""
    await state.clear()
    parts = callback.data.split(":")
    category_name = parts[1]
    page = int(parts[2])
    
    products = await asyncio.to_thread(get_products_by_category, category_name)
    
    if not products:
        await callback.message.edit_text(
            f"📂 **{category_name}**\n\n*(No items currently listed in this category)*",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="📂 ↩️ Back to Categories", callback_data="menu_catalog")]]
            ),
            parse_mode="Markdown"
        )
        return
        
    # Calculate page slices
    total_pages = (len(products) + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE
    page_products = products[page * ITEMS_PER_PAGE : (page + 1) * ITEMS_PER_PAGE]
    
    buttons = []
    for p in page_products:
        stock = await asyncio.to_thread(db.get_stock_count, p["product_id"])
        if stock > 0:
            btn_text = f"🌐 {p['name']} | ${p['price_usd']:.2f} | 📦 {stock}"
        else:
            btn_text = f"🔴 [SOLD OUT] {p['name']} | ${p['price_usd']:.2f} 🔴"
            
        buttons.append([InlineKeyboardButton(text=btn_text, callback_data=f"view_{p['product_id']}")])
        
    # Add smart pagination buttons if item count exceeds page size
    if total_pages > 1:
        nav_row = []
        if page > 0:
            nav_row.append(InlineKeyboardButton(text="« Prev", callback_data=f"catview:{category_name}:{page - 1}"))
        
        nav_row.append(InlineKeyboardButton(text=f"📄 {page + 1} / {total_pages}", callback_data="ignore_nav"))
        
        if (page + 1) < total_pages:
            nav_row.append(InlineKeyboardButton(text="Next »", callback_data=f"catview:{category_name}:{page + 1}"))
            
        buttons.append(nav_row)
        
    buttons.append([InlineKeyboardButton(text="📂 ↩️ Back to Categories", callback_data="menu_catalog")])
    buttons.append([InlineKeyboardButton(text="✖️ ↩️ To Main Menu", callback_data="menu_main")])
    
    await callback.message.edit_text(
        f"📂 **Category:** {category_name}\n\nChoose a product below to view details or purchase:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="Markdown"
    )


@router.callback_query(F.data == "ignore_nav", ThrottlingFilter())
async def cb_ignore_nav(callback: types.CallbackQuery):
    """Silently ignore clicks on the page indicator button."""
    await callback.answer()


@router.callback_query(F.data.startswith("view_"), ThrottlingFilter())
async def cb_view_product(callback: types.CallbackQuery, state: FSMContext):
    """Display individual product details, warranty badge, and Buy Now or Notify When Restocked buttons."""
    await state.clear()
    product_id = int(callback.data.split("_")[1])
    
    products = await asyncio.to_thread(get_active_products)
    product = next((p for p in products if p["product_id"] == product_id), None)
    
    if not product:
        await callback.answer("❌ This product listing is no longer available.", show_alert=True)
        return
        
    stock = await asyncio.to_thread(db.get_stock_count, product_id)
    
    # Process custom warranty display
    warranty = product.get("warranty", "None")
    if warranty and warranty.lower() != "none":
        warranty_block = f"🛡️ 🔴 {warranty.upper()} WARRANTY INCLUDED 🔴\n\n"
    else:
        warranty_block = "\n"
    
    text = (
        f"🌐 **{product['name']}**\n"
        f"📦 Current stock: {stock}\n"
        f"💲 Price: **${product['price_usd']:.2f}**\n"
        f"{warranty_block}"
        f"📝 **Description:**\n{product['description']}"
    )
    
    buttons = []
    if stock > 0:
        buttons.append([InlineKeyboardButton(text="🛒 Buy now", callback_data=f"buy_{product_id}")])
    else:
        buttons.append([InlineKeyboardButton(text="🔔 Notify When Restocked", callback_data=f"notify_{product_id}")])
        
    buttons.append([InlineKeyboardButton(text="📂 ↩️ To Category", callback_data=f"catview:{product.get('category', 'General')}:0")])
    
    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="Markdown"
    )


@router.callback_query(F.data.startswith("notify_"), ThrottlingFilter())
async def cb_notify_restock(callback: types.CallbackQuery):
    """Subscribe user to receive an automated DM alert the moment an item is restocked."""
    product_id = int(callback.data.split("_")[1])
    user_id = callback.from_user.id
    
    success = await asyncio.to_thread(add_restock_subscriber, product_id, user_id)
    if success:
        await callback.answer("✅ Subscribed! We will DM you automatically the exact moment this item is restocked.", show_alert=True)
    else:
        await callback.answer("⚠️ You are already subscribed to restock notifications for this item.", show_alert=True)


@router.callback_query(F.data.startswith("buy_"), ThrottlingFilter())
async def cb_buy_product(callback: types.CallbackQuery, state: FSMContext):
    """Process wallet deduction, log order history, and execute either Auto or Manual delivery."""
    await state.clear()
    product_id = int(callback.data.split("_")[1])
    user_id = callback.from_user.id
    username = callback.from_user.username or "NoUsername"
    
    user = await asyncio.to_thread(db.get_user, user_id)
    if not user:
        await safe_register_user(user_id, username)
        user = await asyncio.to_thread(db.get_user, user_id)
        
    products = await asyncio.to_thread(get_active_products)
    product = next((p for p in products if p["product_id"] == product_id), None)
    
    if not product:
        await callback.answer("❌ This product listing is no longer available.", show_alert=True)
        return
        
    # Check wallet balance
    if user["balance_usd"] < product["price_usd"]:
        shortfall = product["price_usd"] - user["balance_usd"]
        await callback.answer(f"⚠️ Insufficient balance! You need ${shortfall:.2f} more.", show_alert=True)
        
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="💳 Top-Up Wallet Now", callback_data="menu_topup")],
                [InlineKeyboardButton(text="📂 Back to Catalog", callback_data="menu_catalog")]
            ]
        )
        await callback.message.edit_text(
            "⚠️ **Insufficient Wallet Balance**\n\n"
            f"**Selected Item:** {product['name']}\n"
            f"**Item Price:** `${product['price_usd']:.2f} USD`\n"
            f"**Your Current Balance:** `${user['balance_usd']:.2f} USD`\n\n"
            "Please deposit funds into your wallet to complete this purchase.",
            reply_markup=keyboard,
            parse_mode="Markdown"
        )
        return
        
    # Execute database order fulfillment to automatically decrement stock by 1
    delivered_content = await asyncio.to_thread(db.fulfill_order, user_id=user_id, product_id=product_id)
    
    if delivered_content:
        new_balance = user["balance_usd"] - product["price_usd"]
        
        # Log into permanent order history so user can view/recover key via /myorders
        await asyncio.to_thread(log_customer_order, user_id, product["name"], product["price_usd"], delivered_content)
        
        # ==========================================================
        # WORKFLOW A: AUTOMATIC INSTANT DELIVERY
        # ==========================================================
        if STORE_CONFIG["delivery_mode"] == "auto":
            await callback.answer("🎉 Purchase complete! Check your chat messages.", show_alert=True)
            
            await callback.message.answer(
                "🎉 **Order Fulfilled Instantly!**\n\n"
                f"**Product Purchased:** {product['name']}\n"
                f"**Remaining Wallet Balance:** `${new_balance:.2f} USD`\n\n"
                "📦 **Your Digital Access Data / License Key:**\n"
                f"`{delivered_content}`\n\n"
                "_You can also view your past purchases anytime in your **👤 My Profile** menu._",
                parse_mode="Markdown"
            )
            
        # ==========================================================
        # WORKFLOW B: MANUAL CONTACT-ADMIN DELIVERY (CURRENT MODE)
        # ==========================================================
        else:
            await callback.answer("🎉 Purchase complete! Please contact admin to claim.", show_alert=True)
            
            # 1. Send secret fulfillment details to Store Admin DM
            if ADMIN_ID > 0:
                admin_alert = (
                    "🚨 **NEW MANUAL ORDER TO FULFILL!**\n\n"
                    f"**Buyer:** @{username} (`{user_id}`)\n"
                    f"**Product:** {product['name']} (ID: `{product_id}`)\n"
                    f"**Price Paid:** `${product['price_usd']:.2f} USD`\n"
                    f"**Reserved Stock Key/Note:** `{delivered_content}`\n\n"
                    "👉 *The customer's balance was deducted and stock dropped by 1. They have been instructed to DM you to claim the key/service above.*"
                )
                admin_kb = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(text="💬 Message Buyer Directly", url=f"tg://user?id={user_id}")],
                        [InlineKeyboardButton(text="👤 Open Profile (@" + username + ")", url=f"https://t.me/{username}") if username != "NoUsername" else InlineKeyboardButton(text="⚠️ No Username Provided", callback_data="none")]
                    ]
                )
                try:
                    await callback.bot.send_message(
                        chat_id=ADMIN_ID,
                        text=admin_alert,
                        reply_markup=admin_kb,
                        parse_mode="Markdown"
                    )
                except Exception as e:
                    logger.warning(f"Failed to send purchase alert to admin: {e}")

            # 2. Send contact instructions to Customer
            customer_kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="💬 Contact Admin (@MadVichi) to Claim Order", url="https://t.me/MadVichi")],
                    [InlineKeyboardButton(text="🛍️ Return to Catalog", callback_data="menu_catalog")]
                ]
            )
            
            await callback.message.answer(
                "🎉 **Payment Successful!**\n\n"
                f"**Product Purchased:** {product['name']}\n"
                f"**Amount Paid:** `${product['price_usd']:.2f} USD`\n"
                f"**Remaining Wallet Balance:** `${new_balance:.2f} USD`\n\n"
                "📬 **Next Step — Receive Your Order:**\n"
                "Your order has been logged and your inventory slot is reserved! To receive your subscription access or setup instructions, please tap the button below to message the store administrator directly:",
                reply_markup=customer_kb,
                parse_mode="Markdown"
            )
            
        await cb_menu_catalog(callback, state)
    else:
        await callback.answer("❌ Checkout failed. This item may have just gone out of stock!", show_alert=True)
        await cb_menu_catalog(callback, state)


# ==========================================
# 10. HELP & ADMIN MANAGEMENT COMMANDS
# ==========================================
@router.callback_query(F.data == "menu_help", ThrottlingFilter())
async def cb_menu_help(callback: types.CallbackQuery, state: FSMContext):
    """Display store instructions, direct contact support, and admin reference commands."""
    await state.clear()
    await callback.message.edit_text(
        "❓ **Help & Support Guide**\n\n"
        "• **Purchase History / Keys (`/myorders`):** Need to find a license key or subscription link you bought previously? Open your **👤 My Profile** menu and click **📦 My Purchase History / Keys** to view all your past orders instantly 24/7!\n"
        "• **Menu Crawling / Lost Menu:** If your interactive menu ever gets pushed up by chat messages or receipts, simply tap the permanent **🔄 Restart / Menu** button at the bottom of your screen (or type `/restart`) to instantly drop a fresh menu to the bottom!\n"
        "• **Restock Alerts:** If an item is sold out, click **🔔 Notify When Restocked** to get an instant private DM from the bot the exact second new inventory is added!\n"
        "• **Direct Support:** Need personal assistance or have a question about a product? Reach out directly to the store operator at **@MadVichi** for fast 1-on-1 support.\n"
        "• **How to Purchase:** Deposit funds into your wallet using the **Top-Up Wallet Balance** menu. Once funded, select any item from the catalog for instant delivery.\n\n"
        "⚙️ **Admin Commands (For Store Operator):**\n"
        "`/stats` — View live store revenue, total orders, and user metrics\n"
        "`/backup` — Download a secure backup copy of your database (`database.db`)\n"
        "`/listproducts` — View all store items and their Product IDs\n"
        "`/credit <user_id> <amount>` — Instantly inject funds into any wallet\n"
        "`/addproduct <price> <name>` — Create a new product listing\n"
        "`/setcategory <id> <category_name>` — Assign item to a catalog folder\n"
        "`/modifydescription <id> <text>` — Append information to existing description\n"
        "`/applydisclaimer` — Bulk apply PC-only warning to all product descriptions\n"
        "`/setwarranty <id> <duration>` — Set custom warranty duration (e.g. '1 Month')\n"
        "`/addstock <product_id> <content>` — Load digital keys & auto-alert waiting buyers\n"
        "`/delproduct <product_id>` — Delete a product and wipe its leftover inventory\n"
        "`/clearstock <product_id>` — Wipe all unsold stock for an item (reset to 0)\n"
        "`/liststock <product_id>` — View current unsold keys loaded in stock\n"
        "`/delstock <exact_key_or_link>` — Delete a specific individual key or link\n"
        "`/deliverymode <auto/manual>` — Switch between instant automatic or contact-admin delivery\n"
        "`/clearmenu` — Wipe cached reply keyboards from the user's screen",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="💬 Message Support (@MadVichi)", url="https://t.me/MadVichi")],
                [InlineKeyboardButton(text="« Back to Main Menu", callback_data="menu_main")]
            ]
        ),
        parse_mode="Markdown"
    )


@router.callback_query(F.data == "menu_admin", IsAdmin(), ThrottlingFilter())
async def cb_menu_admin(callback: types.CallbackQuery, state: FSMContext):
    """Admin dashboard: Lets the operator click and trigger any management tool directly."""
    await state.clear()
    await callback.message.edit_text(
        "👑 **Storefront Operator Dashboard**\n\n"
        "Click any command button below to execute it immediately or get interactive 1-tap formatting instructions:",
        reply_markup=get_admin_panel_keyboard(),
        parse_mode="Markdown"
    )


@router.callback_query(F.data.startswith("admbtn_"), IsAdmin(), ThrottlingFilter())
async def cb_admin_panel_buttons(callback: types.CallbackQuery, state: FSMContext):
    """Handle direct clicks from the admin panel keyboard."""
    await callback.answer()
    action = callback.data.split("_")[1]
    
    back_kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="« Back to Admin Panel", callback_data="menu_admin")]]
    )
    
    if action == "stats":
        stats = await asyncio.to_thread(get_store_analytics)
        text = (
            "📊 **Live Store Analytics Dashboard**\n\n"
            f"💰 **Total Gross Revenue:** `${stats['revenue']:.2f} USD`\n"
            f"📦 **Total Orders Fulfilled:** `{stats['orders']}`\n"
            f"👥 **Registered Customers:** `{stats['users']}`\n"
            f"🛍️ **Active Products:** `{stats['products']}`\n\n"
            "*System is operating normally with async non-blocking queries.*"
        )
        await callback.message.edit_text(text, reply_markup=back_kb, parse_mode="Markdown")
        
    elif action == "backup":
        await callback.answer("⏳ Generating secure database backup...", show_alert=True)
        try:
            if os.path.exists("database.db"):
                file_obj = FSInputFile("database.db")
                await callback.message.bot.send_document(
                    chat_id=callback.from_user.id,
                    document=file_obj,
                    caption="💾 **Store Database Backup (`database.db`)**\n\nStore this file securely. It contains all user balances, inventory stock, and order histories.",
                    parse_mode="Markdown"
                )
                await callback.message.edit_text("✅ **Backup Sent!**\nA secure copy of your database has been sent directly to your DMs.", reply_markup=back_kb, parse_mode="Markdown")
            else:
                await callback.message.edit_text("❌ Database file `database.db` could not be found on the server filesystem.", reply_markup=back_kb, parse_mode="Markdown")
        except Exception as e:
            await callback.message.edit_text(f"❌ Backup failed: {e}", reply_markup=back_kb, parse_mode="Markdown")
            
    elif action == "listproducts":
        products = await asyncio.to_thread(get_active_products)
        if not products:
            text = "📦 **Catalog Empty:** No products found in the database."
        else:
            text = "📋 **Store Inventory & Product IDs:**\n\n"
            for p in products:
                stock = await asyncio.to_thread(db.get_stock_count, p["product_id"])
                cat = p.get("category", "General")
                text += f"• **ID:** `{p['product_id']}` — **{p['name']}** (${p['price_usd']:.2f}) | *Category: {cat}* | *Stock: {stock}*\n"
        await callback.message.edit_text(text, reply_markup=back_kb, parse_mode="Markdown")
        
    elif action == "deliverymode":
        current = STORE_CONFIG["delivery_mode"]
        new_mode = "manual" if current == "auto" else "auto"
        STORE_CONFIG["delivery_mode"] = new_mode
        await callback.answer(f"🔄 Mode switched to: {new_mode.upper()}", show_alert=True)
        await cb_menu_admin(callback, state)
        
    elif action == "clearmenu":
        await callback.message.answer("🧹 Type `/clearmenu` in chat to wipe lingering bottom reply keyboards.")
        
    elif action == "addproduct":
        await callback.message.edit_text(
            "➕ **Create New Product**\n\nTo add an item, copy and send the command below:\n`/addproduct <price_usd> <product_name>`\n\n*Example:* `/addproduct 14.99 1-Month Premium Subscription`",
            reply_markup=back_kb, parse_mode="Markdown"
        )
    elif action == "category":
        await callback.message.edit_text(
            "📂 **Set Product Category Folder**\n\nTo organize an item into a specific folder, send:\n`/setcategory <product_id> <category_name>`\n\n*Example:* `/setcategory 1 AI & Cloud Software`\n*Example:* `/setcategory 2 Gaming & Game Pass`",
            reply_markup=back_kb, parse_mode="Markdown"
        )
    elif action == "modifydesc":
        await callback.message.edit_text(
            "📝 **Modify Product Description**\n\nTo append information to an existing description, send:\n`/modifydescription <product_id> <text_to_add>`\n\n*Example:* `/modifydescription 1 Includes 24/7 priority customer support.`",
            reply_markup=back_kb, parse_mode="Markdown"
        )
    elif action == "warranty":
        await callback.message.edit_text(
            "🛡️ **Set Custom Warranty Duration**\n\nTo manually add or change the warranty banner on an item, send:\n`/setwarranty <product_id> <duration>`\n\n*Example:* `/setwarranty 1 3 Months`\n*(To remove the warranty completely, type `/setwarranty 1 None`)*",
            reply_markup=back_kb, parse_mode="Markdown"
        )
    elif action == "addstock":
        await callback.message.edit_text(
            "📦 **Load Inventory Stock**\n\nTo add digital deliverables, copy and send:\n`/addstock <product_id> <deliverable_content>`\n\n*Example:* `/addstock 1 https://invite.link/private-access-token`",
            reply_markup=back_kb, parse_mode="Markdown"
        )
    elif action == "liststock":
        await callback.message.edit_text(
            "📊 **Check Unsold Stock**\n\nTo see available keys for an item, send:\n`/liststock <product_id>`\n\n*Example:* `/liststock 1`",
            reply_markup=back_kb, parse_mode="Markdown"
        )
    elif action == "clearstock":
        await callback.message.edit_text(
            "🧹 **Wipe Unsold Stock**\n\nTo delete all unsold keys for an item (reset to 0), send:\n`/clearstock <product_id>`\n\n*Example:* `/clearstock 1`",
            reply_markup=back_kb, parse_mode="Markdown"
        )
    elif action == "delproduct":
        await callback.message.edit_text(
            "🗑️ **Delete Product Listing**\n\nTo permanently remove an item and its stock, send:\n`/delproduct <product_id>`\n\n*Example:* `/delproduct 1`",
            reply_markup=back_kb, parse_mode="Markdown"
        )
    elif action == "credit":
        await callback.message.edit_text(
            "💳 **Credit Wallet Balance**\n\nTo manually add funds to a customer's wallet, send:\n`/credit <user_id> <amount_usd>`\n\n*Example:* `/credit 123456789 15.50`",
            reply_markup=back_kb, parse_mode="Markdown"
        )
    elif action == "delstock":
        await callback.message.edit_text(
            "🗑️ **Delete Specific Stock Key**\n\nTo remove a single exact key or link from inventory, send:\n`/delstock <exact_key_or_link>`\n\n*Example:* `/delstock https://invite.link/bad-token-123`",
            reply_markup=back_kb, parse_mode="Markdown"
        )


@router.message(Command("applydisclaimer"), IsAdmin(), ThrottlingFilter())
async def cmd_applydisclaimer(message: types.Message):
    """Admin tool: Appends the PC compatibility notice to all active product descriptions at once."""
    notice = "\n\n⚠️ **Notice:** Only for PC (1 key = 1 PC). Do NOT use on console. We are not responsible if used on console."
    
    def _update_all():
        with db._get_connection() as conn:
            cursor = conn.execute(
                "UPDATE products SET description = description || ? WHERE is_active = 1",
                (notice,)
            )
            return cursor.rowcount

    updated_count = await asyncio.to_thread(_update_all)
    await message.answer(f"✅ **Disclaimer Applied!**\n\nSuccessfully updated descriptions for **{updated_count}** active product(s).", parse_mode="Markdown")


@router.message(Command("stats"), IsAdmin(), ThrottlingFilter())
async def cmd_stats(message: types.Message):
    """Admin tool: Display live store revenue, total orders, and user metrics."""
    stats = await asyncio.to_thread(get_store_analytics)
    await message.answer(
        "📊 **Live Store Analytics Dashboard**\n\n"
        f"💰 **Total Gross Revenue:** `${stats['revenue']:.2f} USD`\n"
        f"📦 **Total Orders Fulfilled:** `{stats['orders']}`\n"
        f"👥 **Registered Customers:** `{stats['users']}`\n"
        f"🛍️ **Active Products:** `{stats['products']}`\n\n"
        "*System is operating normally with async non-blocking queries.*",
        parse_mode="Markdown"
    )


@router.message(Command("backup"), IsAdmin(), ThrottlingFilter())
async def cmd_backup(message: types.Message):
    """Admin tool: Send a downloadable copy of database.db directly to admin DM."""
    try:
        if os.path.exists("database.db"):
            file_obj = FSInputFile("database.db")
            await message.bot.send_document(
                chat_id=message.from_user.id,
                document=file_obj,
                caption="💾 **Store Database Backup (`database.db`)**\n\nStore this file securely. It contains all user balances, inventory stock, and order histories.",
                parse_mode="Markdown"
            )
            await message.answer("✅ Database backup sent successfully to your DMs.")
        else:
            await message.answer("❌ Database file `database.db` not found.")
    except Exception as e:
        await message.answer(f"❌ Failed to generate backup: {e}")


@router.message(Command("setcategory"), IsAdmin(), ThrottlingFilter())
async def cmd_setcategory(message: types.Message, command: CommandObject, state: FSMContext):
    """Admin tool: Assign a product to a specific catalog folder."""
    await state.clear()
    if not command.args:
        await message.answer(
            "⚠️ **Usage:** `/setcategory <product_id> <category_name>`\n"
            "**Example:** `/setcategory 1 AI & Cloud Software`\n"
            "**Example:** `/setcategory 2 Gaming & Game Pass`", 
            parse_mode="Markdown"
        )
        return
        
    parts = command.args.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("⚠️ You must provide both the Product ID and the desired Category Name.")
        return
        
    try:
        product_id = int(parts[0])
        category_name = parts[1].strip()
        
        success = await asyncio.to_thread(set_product_category, product_id, category_name)
        if success:
            await message.answer(
                "✅ **Category Assigned!**\n\n"
                f"Product ID `{product_id}` is now organized under the **📂 {category_name}** folder in your store catalog.",
                parse_mode="Markdown"
            )
        else:
            await message.answer(f"❌ Could not find an active product with ID `{product_id}`.")
            
    except ValueError:
        await message.answer("❌ Product ID must be a valid integer number.")
    except Exception as e:
        logger.error(f"Error setting category: {e}")
        await message.answer("❌ Database error occurred while trying to update the category.")


@router.message(Command("setwarranty"), IsAdmin(), ThrottlingFilter())
async def cmd_setwarranty(message: types.Message, command: CommandObject, state: FSMContext):
    """Admin tool: Set a custom warranty duration badge for an existing product."""
    await state.clear()
    if not command.args:
        await message.answer(
            "⚠️ **Usage:** `/setwarranty <product_id> <duration>`\n"
            "**Example:** `/setwarranty 1 3 Months`\n"
            "*(To remove a warranty, type `/setwarranty 1 None`)*", 
            parse_mode="Markdown"
        )
        return
        
    parts = command.args.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("⚠️ You must provide both the Product ID and the warranty duration.")
        return
        
    try:
        product_id = int(parts[0])
        duration = parts[1].strip()
        
        success = await asyncio.to_thread(set_product_warranty, product_id, duration)
        
        if success:
            if duration.lower() == "none":
                await message.answer(f"✅ **Warranty Removed!**\n\nThe warranty badge will no longer show for Product ID `{product_id}`.", parse_mode="Markdown")
            else:
                await message.answer(
                    "✅ **Warranty Updated!**\n\n"
                    f"Product ID `{product_id}` now has a custom warranty badge: **{duration.upper()}**.\n"
                    "Use `/listproducts` or open the catalog to review your updated product listing.",
                    parse_mode="Markdown"
                )
        else:
            await message.answer(f"❌ Could not find an active product with ID `{product_id}`.")
            
    except ValueError:
        await message.answer("❌ Product ID must be a valid integer number.")
    except Exception as e:
        logger.error(f"Error updating warranty: {e}")
        await message.answer("❌ Database error occurred while trying to update the warranty.")


@router.message(Command("modifydescription"), IsAdmin(), ThrottlingFilter())
async def cmd_modifydescription(message: types.Message, command: CommandObject, state: FSMContext):
    """Admin tool: Append additional information to an already existing product description."""
    await state.clear()
    if not command.args:
        await message.answer(
            "⚠️ **Usage:** `/modifydescription <product_id> <additional_information>`\n"
            "**Example:** `/modifydescription 1 Includes 24/7 priority customer support.`", 
            parse_mode="Markdown"
        )
        return
        
    parts = command.args.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("⚠️ You must provide both the Product ID and the additional information you wish to add.")
        return
        
    try:
        product_id = int(parts[0])
        added_text = parts[1].strip()
        
        success = await asyncio.to_thread(append_product_description, product_id, added_text)
        
        if success:
            await message.answer(
                "✅ **Description Updated!**\n\n"
                f"Added your information to Product ID `{product_id}`.\n"
                "Use `/listproducts` or open the catalog to review your updated product listing.",
                parse_mode="Markdown"
            )
        else:
            await message.answer(f"❌ Could not find an active product with ID `{product_id}`.")
            
    except ValueError:
        await message.answer("❌ Product ID must be a valid integer number.")
    except Exception as e:
        logger.error(f"Error modifying description: {e}")
        await message.answer("❌ Database error occurred while trying to append information to the description.")


@router.message(Command("listproducts"), IsAdmin(), ThrottlingFilter())
async def cmd_listproducts(message: types.Message, state: FSMContext):
    """Admin tool: List all active products and their Product IDs."""
    await state.clear()
    products = await asyncio.to_thread(get_active_products)
    if not products:
        await message.answer("📦 **Catalog Empty:** No products found in the database.", parse_mode="Markdown")
        return
        
    text = "📋 **Store Inventory & Product IDs:**\n\n"
    for p in products:
        stock = await asyncio.to_thread(db.get_stock_count, p["product_id"])
        cat = p.get("category", "General")
        text += f"• **ID:** `{p['product_id']}` — **{p['name']}** (${p['price_usd']:.2f}) | *Category: {cat}* | *Stock: {stock}*\n"
        
    text += "\n💡 *Use* `/setcategory <id> <name>` *to organize items into folders.*"
    await message.answer(text, parse_mode="Markdown")


@router.message(Command("credit"), IsAdmin(), ThrottlingFilter())
async def cmd_credit(message: types.Message, command: CommandObject):
    """Admin tool: Manually add custom balance amounts to any user."""
    if not command.args:
        await message.answer("⚠️ **Usage:** `/credit <user_id> <amount_usd>`\n**Example:** `/credit 123456789 15.50`", parse_mode="Markdown")
        return
        
    parts = command.args.split()
    if len(parts) < 2:
        await message.answer("⚠️ You must provide both a User ID and an amount.")
        return
        
    try:
        target_user_id = int(parts[0])
        amount = float(parts[1])
        new_bal = await asyncio.to_thread(add_user_balance, user_id=target_user_id, amount_usd=amount)
        
        await message.answer(f"✅ Successfully credited `${amount:.2f} USD` to User ID `{target_user_id}`.\n**New Balance:** `${new_bal:.2f} USD`", parse_mode="Markdown")
        
        try:
            await message.bot.send_message(
                chat_id=target_user_id,
                text=f"🎉 **Wallet Credited!**\n\nAn admin has added `${amount:.2f} USD` to your account.\n**Available Balance:** `${new_bal:.2f} USD`",
                parse_mode="Markdown"
            )
        except Exception:
            pass
    except ValueError:
        await message.answer("❌ Invalid numerical format for User ID or amount.")


@router.message(Command("addproduct"), IsAdmin(), ThrottlingFilter())
async def cmd_addproduct(message: types.Message, command: CommandObject, state: FSMContext):
    """Admin tool: Create a new product listing and broadcast a private DM alert to all users."""
    await state.clear()
    if not command.args:
        await message.answer("⚠️ **Usage:** `/addproduct <price_usd> <product_name>`\n**Example:** `/addproduct 14.99 1-Month Premium Subscription`", parse_mode="Markdown")
        return
        
    parts = command.args.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("⚠️ You must provide both a numerical price and a product name.")
        return
        
    try:
        price = float(parts[0])
        name = parts[1]
        product_id = await asyncio.to_thread(db.add_product, name=name, description="Instant automated digital delivery", price_usd=price)
        
        await message.answer(
            "✅ **New Product Created!**\n"
            f"**Product ID:** `{product_id}`\n**Name:** {name}\n**Price:** `${price:.2f} USD`\n\n"
            "💡 **Next Steps:**\n"
            f"• Assign Category: `/setcategory {product_id} AI & Software`\n"
            f"• Add Warranty Badge: `/setwarranty {product_id} 1 Month`\n"
            f"• Load Inventory: `/addstock {product_id} <access_link_or_key>`", 
            parse_mode="Markdown"
        )
        
        # --- DIRECT DM BROADCAST TO ALL USERS ---
        alert_text = (
            f"🌐 **{name}**\n"
            "✔️ Added: **New Product**\n"
            "📦 Current stock: **0**\n"
            f"💲 Price: **${price:.2f}**"
        )
        alert_kb = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="🛒 View in Catalog", callback_data="menu_catalog")]]
        )
        
        asyncio.create_task(broadcast_to_users(message.bot, alert_text, alert_kb))
        
    except ValueError:
        await message.answer("❌ Invalid price format. Ensure the first argument is a number (e.g., `10.00`).")


@router.message(Command("addstock"), IsAdmin(), ThrottlingFilter())
async def cmd_addstock(message: types.Message, state: FSMContext):
    """Admin tool: Add bulk inventory, broadcast restock alerts to waiting buyers, and notify all users."""
    await state.clear()
    
    # Split message by newlines to automatically detect bulk text blocks
    lines = message.text.strip().split('\n')
    first_line = lines[0].split()
    
    if len(first_line) < 2:
        await message.answer(
            "⚠️ **Usage:**\n`/addstock <product_id>`\n`KEY-1`\n`KEY-2`\n\n"
            "**Example:**\n`/addstock 1`\n`https://link1.com`\n`https://link2.com`", 
            parse_mode="Markdown"
        )
        return
        
    try:
        product_id = int(first_line[1])
        
        # Extract items from subsequent lines, ignoring completely empty ones
        stock_items = [line.strip() for line in lines[1:] if line.strip()]
        
        # Fallback for the old single-line method: /addstock 1 SOME-KEY
        if len(first_line) > 2 and not stock_items:
            stock_items = [" ".join(first_line[2:])]
            
        if not stock_items:
            await message.answer("❌ You didn't provide any stock items to add.")
            return
            
        # Push the full list directly to database.py
        added_count = await asyncio.to_thread(db.add_stock, product_id=product_id, deliverables=stock_items)
        total_stock = await asyncio.to_thread(db.get_stock_count, product_id)
        
        await message.answer(f"📦 **Inventory Updated!**\nAdded `{added_count}` digital unit(s) to Product ID `{product_id}`.\n**Total Available Stock:** `{total_stock} units`", parse_mode="Markdown")
        
        products = await asyncio.to_thread(get_active_products)
        product = next((p for p in products if p["product_id"] == product_id), None)
        
        if product:
            # --- 1. PRIORITY RESTOCK ALERT TO WAITING SUBSCRIBERS ---
            waiting_users = await asyncio.to_thread(get_and_clear_restock_subscribers, product_id)
            if waiting_users:
                restock_text = (
                    "🚨 **RESTOCK ALERT!**\n\n"
                    f"Good news! **{product['name']}** is officially back in stock.\n"
                    f"**Price:** `${product['price_usd']:.2f} USD` | **Available Stock:** `{total_stock} units`\n\n"
                    "⚡ *You requested this notification. Tap below to secure your order before it sells out!*"
                )
                restock_kb = InlineKeyboardMarkup(
                    inline_keyboard=[[InlineKeyboardButton(text="🛒 Buy Now Before It Sells Out", callback_data=f"view_{product_id}")]]
                )
                for w_user in waiting_users:
                    try:
                        await message.bot.send_message(chat_id=w_user, text=restock_text, reply_markup=restock_kb, parse_mode="Markdown")
                        await asyncio.sleep(0.05)
                    except Exception:
                        continue
                logger.info(f"🔔 Restock alerts delivered to {len(waiting_users)} waiting subscribers for Product {product_id}.")

            # --- 2. GENERAL DM BROADCAST TO ALL REGISTERED USERS ---
            alert_text = (
                f"🌐 **{product['name']}**\n"
                f"✔️ Added: **{added_count}**\n"
                f"📦 Current stock: **{total_stock}**\n"
                f"💲 Price: **${product['price_usd']:.2f}**"
            )
            alert_kb = InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="🛒 View Item", callback_data=f"view_{product_id}")]]
            )
            
            asyncio.create_task(broadcast_to_users(message.bot, alert_text, alert_kb))
                
    except ValueError:
        await message.answer("❌ Product ID must be a valid integer.")


@router.message(Command("delproduct"), IsAdmin(), ThrottlingFilter())
async def cmd_delproduct(message: types.Message, command: CommandObject, state: FSMContext):
    """Admin tool: Delete a product and its associated inventory from the database."""
    await state.clear()
    if not command.args:
        await message.answer("⚠️ **Usage:** `/delproduct <product_id>`\n**Example:** `/delproduct 1`", parse_mode="Markdown")
        return
        
    try:
        product_id = int(command.args.strip())
        
        def _delete():
            with db._get_connection() as conn:
                cursor = conn.execute("SELECT name FROM products WHERE product_id = ?", (product_id,))
                row = cursor.fetchone()
                if not row:
                    return None
                p_name = row[0]
                conn.execute("DELETE FROM inventory WHERE product_id = ?", (product_id,))
                conn.execute("DELETE FROM products WHERE product_id = ?", (product_id,))
                conn.execute("DELETE FROM restock_subscribers WHERE product_id = ?", (product_id,))
                return p_name

        product_name = await asyncio.to_thread(_delete)
        if not product_name:
            await message.answer(f"❌ Product ID `{product_id}` not found in the database.", parse_mode="Markdown")
            return
            
        await message.answer(f"🗑️ **Product Removed!**\n\nSuccessfully deleted **{product_name}** (ID: `{product_id}`) and all of its associated stock from your storefront.", parse_mode="Markdown")
        
    except ValueError:
        await message.answer("❌ Product ID must be a valid integer.")
    except Exception as e:
        logger.error(f"Error deleting product: {e}")
        await message.answer("❌ Database error occurred while trying to delete the product.")


@router.message(Command("clearstock"), IsAdmin(), ThrottlingFilter())
async def cmd_clearstock(message: types.Message, command: CommandObject, state: FSMContext):
    """Admin tool: Wipe all unsold inventory for a specific Product ID."""
    await state.clear()
    if not command.args:
        await message.answer("⚠️ **Usage:** `/clearstock <product_id>`\n**Example:** `/clearstock 1`", parse_mode="Markdown")
        return
        
    try:
        product_id = int(command.args.strip())
        
        def _clear():
            with db._get_connection() as conn:
                cursor = conn.execute("SELECT name FROM products WHERE product_id = ?", (product_id,))
                row = cursor.fetchone()
                if not row:
                    return None, 0
                p_name = row[0]
                cursor = conn.execute("DELETE FROM inventory WHERE product_id = ?", (product_id,))
                return p_name, cursor.rowcount

        product_name, deleted_count = await asyncio.to_thread(_clear)
        if not product_name:
            await message.answer(f"❌ Product ID `{product_id}` not found.", parse_mode="Markdown")
            return
            
        await message.answer(
            "🧹 **Stock Cleared!**\n\n"
            f"Removed **{deleted_count}** digital unit(s) from **{product_name}** (ID: `{product_id}`).\n"
            "**Current Stock:** `0 units`", 
            parse_mode="Markdown"
        )
    except ValueError:
        await message.answer("❌ Product ID must be a valid integer.")
    except Exception as e:
        logger.error(f"Error clearing stock: {e}")
        await message.answer("❌ Database error occurred while trying to clear stock.")


@router.message(Command("delstock"), IsAdmin(), ThrottlingFilter())
async def cmd_delstock(message: types.Message, command: CommandObject, state: FSMContext):
    """Admin tool: Delete a specific individual key or link from inventory."""
    await state.clear()
    if not command.args:
        await message.answer("⚠️ **Usage:** `/delstock <exact_key_or_link>`\n**Example:** `/delstock https://invite.link/bad-token-123`", parse_mode="Markdown")
        return
        
    target_content = command.args.strip()
    
    try:
        def _del_key():
            with db._get_connection() as conn:
                cursor = conn.execute("PRAGMA table_info(inventory)")
                columns = [col["name"] for col in cursor.fetchall()]
                text_col = next((col for col in columns if col in ["deliverable", "content", "data", "key", "deliverable_content", "text"]), None)
                if not text_col:
                    return -1
                cursor = conn.execute(f"DELETE FROM inventory WHERE {text_col} = ?", (target_content,))
                return cursor.rowcount

        deleted_count = await asyncio.to_thread(_del_key)
        if deleted_count == -1:
            await message.answer("❌ Could not identify the content column in your inventory database table.")
            return
            
        if deleted_count > 0:
            await message.answer(f"🗑️ **Item Deleted!**\n\nSuccessfully removed `{deleted_count}` matching key(s)/link(s) from your inventory.", parse_mode="Markdown")
        else:
            await message.answer("⚠️ **Not Found:** Could not find that exact string in your unsold inventory. Use `/liststock <product_id>` to check your active keys.", parse_mode="Markdown")
            
    except Exception as e:
        logger.error(f"Error deleting specific stock: {e}")
        await message.answer("❌ Database error occurred while trying to delete the stock item.")


@router.message(Command("liststock"), IsAdmin(), ThrottlingFilter())
async def cmd_liststock(message: types.Message, command: CommandObject, state: FSMContext):
    """Admin tool: Print a list of unsold inventory keys/links for a specific product."""
    await state.clear()
    if not command.args:
        await message.answer("⚠️ **Usage:** `/liststock <product_id>`\n**Example:** `/liststock 1`", parse_mode="Markdown")
        return
        
    try:
        product_id = int(command.args.strip())
        
        def _get_stock_list():
            with db._get_connection() as conn:
                cursor = conn.execute("SELECT name FROM products WHERE product_id = ?", (product_id,))
                row = cursor.fetchone()
                if not row:
                    return None, []
                p_name = row[0]
                
                cursor = conn.execute("PRAGMA table_info(inventory)")
                columns = [col["name"] for col in cursor.fetchall()]
                text_col = next((col for col in columns if col in ["deliverable", "content", "data", "key", "deliverable_content", "text"]), None)
                sold_col = next((col for col in columns if col in ["is_sold", "sold", "status"]), None)
                
                if sold_col:
                    query = f"SELECT {text_col} FROM inventory WHERE product_id = ? AND ({sold_col} = 0 OR {sold_col} = 'unsold' OR {sold_col} IS NULL)"
                else:
                    query = f"SELECT {text_col} FROM inventory WHERE product_id = ?"
                    
                cursor = conn.execute(query, (product_id,))
                return p_name, [r[0] for r in cursor.fetchall()]

        product_name, items = await asyncio.to_thread(_get_stock_list)
        if not product_name:
            await message.answer(f"❌ Product ID `{product_id}` not found.", parse_mode="Markdown")
            return
            
        if not items:
            await message.answer(f"📦 **{product_name}** currently has **0** items in stock.", parse_mode="Markdown")
            return
            
        text = f"📦 **Current Unsold Stock for {product_name} (ID: `{product_id}`):**\n\n"
        for idx, item in enumerate(items[:15], 1):
            text += f"`{item}`\n"
            
        if len(items) > 15:
            text += f"\n*...and {len(items) - 15} more units.*"
            
        text += "\n💡 *To delete a specific key above, copy it and run:*\n`/delstock <pasted_key>`"
        await message.answer(text, parse_mode="Markdown")
        
    except ValueError:
        await message.answer("❌ Product ID must be a valid integer.")
    except Exception as e:
        logger.error(f"Error listing stock: {e}")
        await message.answer("❌ Database error occurred while trying to list stock.")


@router.message(Command("deliverymode"), IsAdmin(), ThrottlingFilter())
async def cmd_deliverymode(message: types.Message, command: CommandObject, state: FSMContext):
    """Admin tool: Switch between Automatic instant delivery and Manual contact-admin delivery."""
    await state.clear()
    current_mode = STORE_CONFIG["delivery_mode"].upper()
    
    if not command.args or command.args.strip().lower() not in ["auto", "manual"]:
        await message.answer(
            "⚙️ **Store Delivery Configuration**\n\n"
            f"**Current Mode:** `{current_mode}`\n\n"
            "• **MANUAL:** Deducts balance, drops stock, sends key to admin DM, and tells customer to contact you.\n"
            "• **AUTO:** Deducts balance, drops stock, and instantly delivers key directly to the customer's chat.\n\n"
            "💡 **To switch modes, run:**\n"
            "`/deliverymode auto` OR `/deliverymode manual`",
            parse_mode="Markdown"
        )
        return
        
    new_mode = command.args.strip().lower()
    STORE_CONFIG["delivery_mode"] = new_mode
    
    await message.answer(
        "🔄 **Delivery Mode Changed!**\n\n"
        f"Your storefront checkout engine is now set to: **{new_mode.upper()} DELIVERY**.\n\n"
        f"*(All future orders will immediately use the {new_mode.upper()} workflow!)*",
        parse_mode="Markdown"
    )


# ==========================================
# 11. MAIN EXECUTION LOOP
# ==========================================
async def main():
    """Configure logging, assemble router, initiate polling and background workers."""
    global crypto
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - [%(levelname)s] - %(name)s - %(message)s")
    
    # ---------------------------------------------------------
    # FIX: Initialize AioCryptoPay INSIDE the active asyncio event loop
    # ---------------------------------------------------------
    crypto = AioCryptoPay(token=crypto_token, network=network_mode)

    # SILENT DB UPGRADE: Automatically adds database tables for warranties, categories, orders, restocks, and referrals
    ensure_db_upgrades()
    
    bot_token = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
    bot = Bot(token=bot_token)
    dp = Dispatcher()
    
    dp.include_router(router)
    
    # Launch background automated workers
    asyncio.create_task(auto_verify_invoices(bot))
    asyncio.create_task(check_low_stock_watcher(bot))
    
    logger.info("🚀 Starting Enterprise Telegram Storefront Bot with Analytics & Anti-Spam...")
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)
    finally:
        logger.info("🛑 Shutting down network connections gracefully...")
        await crypto.close()
        await bot.session.close()

if __name__ == "__main__":
    # ==========================================
    # START RENDER BACKGROUND WEB SERVER
    # ==========================================
    keep_alive() 
    
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot execution terminated.")
