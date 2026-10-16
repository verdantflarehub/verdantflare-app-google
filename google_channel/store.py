"""Encrypted state owned solely by this channel application."""
from contextlib import contextmanager
from datetime import UTC, datetime
import hashlib
import os
from pathlib import Path
import sqlite3

from cryptography.fernet import Fernet


def now():
    return datetime.now(UTC).isoformat()


class Conflict(ValueError):
    pass


class Store:
    def __init__(self, path, encryption_key):
        self.cipher = Fernet(encryption_key.encode())
        self.operation_key = hashlib.sha256(b'channel-account-operations:' + encryption_key.encode()).digest()
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path.touch(mode=0o600, exist_ok=True)
        os.chmod(self.path, 0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA busy_timeout=30000')
        try:
            with db:
                yield db
        finally:
            db.close()
