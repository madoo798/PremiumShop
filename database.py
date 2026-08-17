import os
import libsql
from typing import Optional, List, Dict, Any

class DatabaseManager:
    def __init__(self, db_url: Optional[str] = None, auth_token: Optional[str] = None):
        """Initialize the Turso cloud database connection and create tables if they don't exist."""
        self.db_url = db_url or os.getenv("TURSO_DATABASE_URL", "store.db")
        self.auth_token = auth_token or os.getenv("TURSO_AUTH_TOKEN", "")
        self._init_db()

    def _get_connection(self):
        """Create a database connection to Turso cloud or fallback to a local file."""
        if self.db_url.startswith("libsql://") or self.db_url.startswith("https://"):
            return libsql.connect(self.db_url, auth_token=self.auth_token)
        else:
            return libsql.connect(self.db_url)

    def _init_db(self):
        """Create the core schema for the digital storefront."""
        with self._get_connection() as conn:
            # 1. Users Table: Tracks customer profiles and wallet balances
            conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    balance_usd REAL DEFAULT 0.0,
                    registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # 2. Products Table: Stores digital product catalog
            conn.execute("""
                CREATE TABLE IF NOT EXISTS products (
                    product_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    description TEXT,
                    price_usd REAL NOT NULL,
                    is_active BOOLEAN DEFAULT 1
                )
            """)

            # 3. Inventory Table: Holds digital deliverables (access links, license keys, etc.)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS inventory (
                    item_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    product_id INTEGER NOT NULL,
                    content TEXT NOT NULL,
                    is_sold BOOLEAN DEFAULT 0,
                    FOREIGN KEY (product_id) REFERENCES products (product_id)
                )
            """)

            # 4. Invoices Table: Maps Telegram CryptoBot transaction IDs to users
            conn.execute("""
                CREATE TABLE IF NOT EXISTS invoices (
                    invoice_id INTEGER PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    amount_usd REAL NOT NULL,
                    status TEXT DEFAULT 'pending',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users (user_id)
                )
            """)

            # 5. Orders Table: Logs completed sales and delivered content
            conn.execute("""
                CREATE TABLE IF NOT EXISTS orders (
                    order_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    product_id INTEGER NOT NULL,
                    delivered_content TEXT NOT NULL,
                    purchased_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users (user_id),
                    FOREIGN KEY (product_id) REFERENCES products (product_id)
                )
            """)
            conn.commit()

    # ==========================================
    # USER MANAGEMENT
    # ==========================================
    def add_user(self, user_id: int, username: Optional[str] = None) -> None:
        """Register a new user or update their username if they already exist."""
        with self._get_connection() as conn:
            conn.execute("""
                INSERT INTO users (user_id, username) 
                VALUES (?, ?)
                ON CONFLICT(user_id) DO UPDATE SET username = excluded.username
            """, (user_id, username))
            conn.commit()

    def get_user(self, user_id: int) -> Optional[Dict[str, Any]]:
        """Retrieve user profile and wallet balance."""
        with self._get_connection() as conn:
            cursor = conn.execute("SELECT user_id, username, balance_usd, registered_at FROM users WHERE user_id = ?", (user_id,))
            row = cursor.fetchone()
            if row:
                cols = [desc[0] for desc in cursor.description]
                return dict(zip(cols, row))
            return None

    # ==========================================
    # INVOICE & CRYPTO PAY TRACKING
    # ==========================================
    def create_invoice(self, invoice_id: int, user_id: int, amount_usd: float) -> None:
        """Record a newly generated @CryptoBot invoice."""
        with self._get_connection() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO invoices (invoice_id, user_id, amount_usd, status)
                VALUES (?, ?, ?, 'pending')
            """, (invoice_id, user_id, amount_usd))
            conn.commit()

    def mark_invoice_paid(self, invoice_id: int) -> bool:
        """Mark an invoice as paid and automatically credit the user's wallet balance."""
        with self._get_connection() as conn:
            cursor = conn.execute("SELECT user_id, amount_usd, status FROM invoices WHERE invoice_id = ?", (invoice_id,))
            row = cursor.fetchone()
            
            if not row:
                return False
            
            cols = [desc[0] for desc in cursor.description]
            row_dict = dict(zip(cols, row))
            
            if row_dict["status"] == "paid":
                return False

            user_id = row_dict["user_id"]
            amount = row_dict["amount_usd"]

            conn.execute("UPDATE invoices SET status = 'paid' WHERE invoice_id = ?", (invoice_id,))
            conn.execute("UPDATE users SET balance_usd = balance_usd + ? WHERE user_id = ?", (amount, user_id))
            conn.commit()
            return True

    # ==========================================
    # PRODUCT & DIGITAL INVENTORY MANAGEMENT
    # ==========================================
    def add_product(self, name: str, description: str, price_usd: float) -> int:
        """Create a new product listing and return its product_id."""
        with self._get_connection() as conn:
            cursor = conn.execute("""
                INSERT INTO products (name, description, price_usd) 
                VALUES (?, ?, ?)
            """, (name, description, price_usd))
            conn.commit()
            return cursor.lastrowid

    def add_stock(self, product_id: int, deliverables: List[str]) -> int:
        """Bulk-add digital stock to a product."""
        with self._get_connection() as conn:
            data = [(product_id, item) for item in deliverables]
            cursor = conn.executemany("""
                INSERT INTO inventory (product_id, content) VALUES (?, ?)
            """, data)
            conn.commit()
            return cursor.rowcount

    def get_stock_count(self, product_id: int) -> int:
        """Check how many unsold items remain for a specific product."""
        with self._get_connection() as conn:
            cursor = conn.execute("""
                SELECT COUNT(*) as count FROM inventory 
                WHERE product_id = ? AND is_sold = 0
            """, (product_id,))
            row = cursor.fetchone()
            return row[0] if row else 0

    def fulfill_order(self, user_id: int, product_id: int) -> Optional[str]:
        """Executes a transaction to check balance/stock and fulfill an order."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            
            cursor.execute("SELECT price_usd FROM products WHERE product_id = ? AND is_active = 1", (product_id,))
            product = cursor.fetchone()
            if not product:
                return None
            price = product[0]

            cursor.execute("SELECT balance_usd FROM users WHERE user_id = ?", (user_id,))
            user = cursor.fetchone()
            if not user or user[0] < price:
                return None

            cursor.execute("""
                SELECT item_id, content FROM inventory 
                WHERE product_id = ? AND is_sold = 0 
                LIMIT 1
            """, (product_id,))
            stock = cursor.fetchone()
            if not stock:
                return None

            item_id, content = stock[0], stock[1]

            try:
                cursor.execute("UPDATE users SET balance_usd = balance_usd - ? WHERE user_id = ?", (price, user_id))
                cursor.execute("UPDATE inventory SET is_sold = 1 WHERE item_id = ?", (item_id,))
                cursor.execute("""
                    INSERT INTO orders (user_id, product_id, delivered_content)
                    VALUES (?, ?, ?)
                """, (user_id, product_id, content))
                
                conn.commit()
                return content
            except Exception as e:
                conn.rollback()
                return None

db = DatabaseManager()