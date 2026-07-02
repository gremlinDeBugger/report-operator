"""
registry.py — Keyed-client credential store (custodial lane).

Holds the set of clients who have handed you a live API key and want reports
pulled and generated on a schedule. Each client's secret is encrypted at rest
with Fernet (AES-128-CBC + HMAC); the plaintext key exists only transiently in
memory during that one client's run.

IMPORTANT — lane separation:
    This module is the ONLY home of client secrets and the ONLY thing the
    scheduler iterates. The basic CSV lane (runner.run_csv) never imports this
    module and never touches it. A customer with no record here cannot be seen
    by the scheduler and cannot have a report fired for them. No record = no run.

The master encryption key is read from the environment (OPERATOR_MASTER_KEY).
It is NEVER written to disk by this module and NEVER stored in the registry.
That is what makes the store portable across hosts (laptop -> mac mini -> VPS):
move the data file and set the env var on the new host; nothing else changes.

Author: Jared Jowett (GremlinHunter)
"""
from __future__ import annotations

import os
import json
import base64
import logging
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone

from cryptography.fernet import Fernet, InvalidToken

log = logging.getLogger("registry")

MASTER_KEY_ENV = "OPERATOR_MASTER_KEY"
DEFAULT_STORE = os.path.join(os.path.dirname(__file__), "data", "clients.json")


class RegistryError(Exception):
    """Raised for registry-level problems (missing master key, bad store, etc.)."""


# --------------------------------------------------------------------------- #
# Master key handling
# --------------------------------------------------------------------------- #
def generate_master_key() -> str:
    """Make a new Fernet master key. Run once; store it in your env, never in git."""
    return Fernet.generate_key().decode()


def _load_cipher() -> Fernet:
    raw = os.environ.get(MASTER_KEY_ENV)
    if not raw:
        raise RegistryError(
            f"{MASTER_KEY_ENV} is not set. The credential store cannot be opened "
            f"without it. Generate one with:\n"
            f"    python -c \"import registry; print(registry.generate_master_key())\"\n"
            f"then set it in your environment (NEVER commit it)."
        )
    try:
        return Fernet(raw.encode())
    except (ValueError, TypeError) as e:
        raise RegistryError(f"{MASTER_KEY_ENV} is not a valid Fernet key: {e}")


# --------------------------------------------------------------------------- #
# Client record
# --------------------------------------------------------------------------- #
@dataclass
class Client:
    """
    One keyed client. The api_key field holds the *encrypted* token at rest;
    decrypt() returns the plaintext only when a run actually needs it.
    """
    client_id: str
    brand: str
    api_key_enc: str                 # Fernet-encrypted secret (base64 text)
    schedule_days: list[int] = field(default_factory=list)  # 0=Mon .. 6=Sun
    report_config: dict = field(default_factory=dict)
    # optional branding placed on the report — all blank-safe
    business_name: str = ""
    email: str = ""
    phone: str = ""
    logo_file: str = ""              # filename inside the client's workspace, if any
    # AI insight: customer opts in at signup; optional self-set call ceiling
    ai_insight: bool = False
    ai_ceiling: int | None = None
    report_type: str = "auto"        # customer-declared data type for routing
    active: bool = True
    last_run: str | None = None
    created: str = ""

    def decrypt_key(self, cipher: Fernet) -> str:
        try:
            return cipher.decrypt(self.api_key_enc.encode()).decode()
        except InvalidToken:
            raise RegistryError(
                f"client '{self.client_id}': key could not be decrypted — wrong "
                f"master key for this store, or the record is corrupt."
            )


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #
class Registry:
    def __init__(self, store_path: str = DEFAULT_STORE):
        self.store_path = store_path
        self._cipher = _load_cipher()
        self._clients: dict[str, Client] = {}
        self._load()

    # ---- persistence ---- #
    def _load(self):
        if not os.path.exists(self.store_path):
            log.info("no store at %s yet — starting empty", self.store_path)
            return
        try:
            with open(self.store_path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            raise RegistryError(f"could not read store {self.store_path}: {e}")
        for cid, rec in data.items():
            self._clients[cid] = Client(**rec)
        log.info("loaded %d client(s) from %s", len(self._clients), self.store_path)

    def _save(self):
        os.makedirs(os.path.dirname(self.store_path), exist_ok=True)
        tmp = self.store_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({cid: asdict(c) for cid, c in self._clients.items()}, f, indent=2)
        os.replace(tmp, self.store_path)   # atomic; never leaves a half-written store

    # ---- operations ---- #
    def add_client(self, client_id: str, brand: str, api_key: str,
                   schedule_days: list[int] | None = None,
                   report_config: dict | None = None,
                   business_name: str = "", email: str = "",
                   phone: str = "", logo_file: str = "",
                   ai_insight: bool = False, ai_ceiling: int | None = None,
                   report_type: str = "auto") -> Client:
        if client_id in self._clients:
            raise RegistryError(f"client '{client_id}' already exists")
        enc = self._cipher.encrypt(api_key.encode()).decode()
        c = Client(
            client_id=client_id,
            brand=brand,
            api_key_enc=enc,
            schedule_days=schedule_days or [],
            report_config=report_config or {},
            business_name=business_name,
            email=email,
            phone=phone,
            logo_file=logo_file,
            ai_insight=ai_insight,
            ai_ceiling=ai_ceiling,
            report_type=report_type,
            created=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
        self._clients[client_id] = c
        self._save()
        log.info("added client '%s' (%s)", client_id, brand)
        return c

    def get(self, client_id: str) -> Client:
        if client_id not in self._clients:
            raise RegistryError(f"no such client: '{client_id}'")
        return self._clients[client_id]

    def list_clients(self) -> list[Client]:
        return list(self._clients.values())

    def active_clients(self) -> list[Client]:
        return [c for c in self._clients.values() if c.active]

    def revoke(self, client_id: str):
        """Deactivate + wipe the stored secret. Scheduler stops seeing them."""
        c = self.get(client_id)
        c.active = False
        c.api_key_enc = ""    # destroy the secret on revoke
        self._save()
        log.info("revoked client '%s' (secret wiped, deactivated)", client_id)

    def mark_run(self, client_id: str, when: str | None = None):
        c = self.get(client_id)
        c.last_run = when or datetime.now(timezone.utc).isoformat(timespec="seconds")
        self._save()

    def decrypt_key_for(self, client_id: str) -> str:
        return self.get(client_id).decrypt_key(self._cipher)
