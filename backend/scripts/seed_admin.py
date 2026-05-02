"""
SocialProof — seed_admin.py
===========================
Run this ONCE to create the first admin account in an empty database.

Usage:
    python seed_admin.py
    python seed_admin.py --username admin --email me@example.com --password YourStrongPassword123

Requirements:
    pip install bcrypt pymysql python-dotenv

The script reads DATABASE_URL from your .env (same as the FastAPI app),
so run it from the project root where .env lives.
"""
import argparse
import sys
import bcrypt
import sqlalchemy as sa

# ── Load config the same way the app does ─────────────────────────────────────
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import os

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "mysql+pymysql://user:password@localhost/socialproof_db",
)


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def seed_admin(username: str, email: str, password: str) -> None:
    engine = sa.create_engine(DATABASE_URL, pool_pre_ping=True)

    with engine.connect() as conn:
        # ── Check if users table exists ────────────────────────────────────────
        try:
            conn.execute(sa.text("SELECT 1 FROM users LIMIT 1"))
        except Exception:
            print("✗  Could not reach the 'users' table.")
            print("   Make sure the database is running and migrations have been applied.")
            print(f"   DATABASE_URL = {DATABASE_URL}")
            sys.exit(1)

        # ── Check for existing account ─────────────────────────────────────────
        existing = conn.execute(
            sa.text("SELECT id, role FROM users WHERE email=:e OR username=:u"),
            {"e": email, "u": username},
        ).fetchone()

        if existing:
            if existing.role == "admin":
                print(f"✓  Admin account already exists (id={existing.id}, username={username}).")
                print("   No changes made.")
            else:
                # Promote existing user to admin
                conn.execute(
                    sa.text("UPDATE users SET role='admin' WHERE id=:id"),
                    {"id": existing.id},
                )
                conn.commit()
                print(f"✓  Existing user id={existing.id} promoted to admin.")
            return

        # ── Insert new admin ───────────────────────────────────────────────────
        conn.execute(
            sa.text(
                "INSERT INTO users (username, email, password_hash, role) "
                "VALUES (:u, :e, :h, 'admin')"
            ),
            {"u": username, "e": email, "h": hash_password(password)},
        )
        conn.commit()

        new_id = conn.execute(
            sa.text("SELECT id FROM users WHERE email=:e"), {"e": email}
        ).scalar()

        print(f"✓  Admin account created successfully!")
        print(f"   id       : {new_id}")
        print(f"   username : {username}")
        print(f"   email    : {email}")
        print(f"   role     : admin")
        print()
        print("   You can now log in at http://localhost:5500/pages/login.html")
        print("   Admins are redirected to admin.html after login.")


# ── CLI ────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Create the first SocialProof admin account.")
    parser.add_argument("--username", default="admin",       help="Admin username (default: admin)")
    parser.add_argument("--email",    default="admin@socialproof.local", help="Admin email")
    parser.add_argument("--password", default=None,          help="Password (prompted if omitted)")
    args = parser.parse_args()

    password = args.password
    if not password:
        import getpass
        print("Enter a password for the admin account (input hidden):")
        password = getpass.getpass("  Password: ")
        confirm  = getpass.getpass("  Confirm : ")
        if password != confirm:
            print("✗  Passwords do not match.")
            sys.exit(1)
        if len(password) < 8:
            print("✗  Password must be at least 8 characters.")
            sys.exit(1)

    seed_admin(args.username, args.email, password)
