"""PSX REBORN: OG pet shop.

This module contains the catalog and purchase workflow without importing
Discord, so the money/stock invariants can be tested independently.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
import secrets
import sqlite3
import string
from pathlib import Path
from typing import Any

import database as db

ROOT = Path(__file__).resolve().parent
DISPLAY_ASSETS = ROOT / "assets" / "psx_shop"
RAW_ASSETS = ROOT / "assets" / "raw" / "psx_shop"
RECEIPT_PREFIX = "tap_bot-"
RECEIPT_LENGTH = 12
RECEIPT_ALPHABET = string.ascii_letters + string.digits


@dataclass(frozen=True)
class Pet:
    pet_id: str
    name: str
    price_shards: int
    asset_key: str


PET_CATALOG: tuple[Pet, ...] = (
    Pet(
        "golden_huge_hell_rock",
        "Golden Huge Hell Rock",
        1000,
        "golden_huge_hell_rock",
    ),
    Pet("huge_capcake", "Huge Capcake", 1500, "huge_capcake"),
    Pet("huge_pumpkin_cat", "Huge Pumpkin Cat", 3000, "huge_pumpkin_cat"),
)
PETS_BY_ID = {pet.pet_id: pet for pet in PET_CATALOG}
PETS_BY_NAME = {re.sub(r"[^a-z0-9]+", "_", pet.name.casefold()).strip("_"): pet for pet in PET_CATALOG}


@dataclass(frozen=True)
class PurchaseResult:
    ok: bool
    reason: str
    pet: Pet | None = None
    purchase_id: int | None = None
    code: str | None = None
    price_shards: int = 0
    remaining_shards: int | None = None


def normalize_pet_query(query: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(query).casefold()).strip("_")


def resolve_pet(query: str) -> Pet | None:
    normalized = normalize_pet_query(query)
    return PETS_BY_ID.get(normalized) or PETS_BY_NAME.get(normalized)


def autocomplete_names(current: str) -> list[Pet]:
    needle = normalize_pet_query(current)
    if not needle:
        return list(PET_CATALOG)
    return [
        pet
        for pet in PET_CATALOG
        if needle in pet.pet_id or needle in normalize_pet_query(pet.name)
    ]


def ensure_catalog() -> None:
    db.init_db()
    db.psx_catalog_upsert(
        [
            {
                "pet_id": pet.pet_id,
                "name": pet.name,
                "price_shards": pet.price_shards,
                "asset_key": pet.asset_key,
            }
            for pet in PET_CATALOG
        ]
    )


def catalog_state() -> list[dict[str, Any]]:
    """Return the configured catalog, preserving database claim state."""
    ensure_catalog()
    rows = {row["pet_id"]: row for row in db.psx_catalog_rows()}
    return [
        {
            "pet": pet,
            "claimed": rows.get(pet.pet_id, {}).get("claimed_by") is not None,
            "claimed_by": rows.get(pet.pet_id, {}).get("claimed_by"),
        }
        for pet in PET_CATALOG
    ]


def pet_image_path(pet: Pet | str) -> Path | None:
    item = pet if isinstance(pet, Pet) else resolve_pet(pet)
    if item is None:
        return None
    for suffix in (".jpg", ".jpeg", ".png"):
        path = DISPLAY_ASSETS / f"{item.asset_key}{suffix}"
        if path.is_file():
            return path
    return None


def raw_image_path(pet: Pet | str) -> Path | None:
    item = pet if isinstance(pet, Pet) else resolve_pet(pet)
    if item is None:
        return None
    for suffix in (".png", ".jpg", ".jpeg"):
        path = RAW_ASSETS / f"{item.asset_key}{suffix}"
        if path.is_file():
            return path
    return None


def generate_receipt_code() -> str:
    suffix = "".join(secrets.choice(RECEIPT_ALPHABET) for _ in range(RECEIPT_LENGTH))
    return f"{RECEIPT_PREFIX}{suffix}"


def purchase_pet(user_id: int, pet_query: str, *, code_factory=generate_receipt_code) -> PurchaseResult:
    """Reserve a pet and charge shards, retrying an unlikely code collision."""
    pet = resolve_pet(pet_query)
    if pet is None:
        return PurchaseResult(False, "unknown_pet")

    ensure_catalog()
    for _ in range(5):
        code = code_factory()
        try:
            status, raw = db.psx_reserve_purchase(user_id, pet.pet_id, code)
        except sqlite3.IntegrityError:
            # Only a receipt collision should happen here. The transaction in
            # database.py has already rolled back the charge and claim.
            continue

        if status == "ok" and raw:
            return PurchaseResult(
                True,
                "ok",
                pet=pet,
                purchase_id=int(raw["purchase_id"]),
                code=code,
                price_shards=pet.price_shards,
                remaining_shards=db.shards_of(user_id),
            )
        if status == "already_claimed":
            return PurchaseResult(False, status, pet=pet, price_shards=pet.price_shards)
        if status == "insufficient_shards":
            return PurchaseResult(False, status, pet=pet, price_shards=pet.price_shards)
        return PurchaseResult(False, status, pet=pet)

    return PurchaseResult(False, "receipt_collision", pet=pet, price_shards=pet.price_shards)


def mark_receipt_delivered(purchase_id: int) -> bool:
    return db.psx_mark_dm_sent(purchase_id)


def refund_if_dm_failed(purchase_id: int) -> str:
    return db.psx_refund_pending(purchase_id)
