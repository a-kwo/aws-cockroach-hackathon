"""Tenant-scoped external account connections.

OAuth refresh tokens are capabilities, not profile data.  Repository records
therefore keep only ciphertext and safe destination metadata.  The browser and
the Maker model never receive the ciphertext or a decrypted token.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping

GOOGLE_BUSINESS_PROVIDER = "google_business"
CONNECTION_PENDING = "pending_selection"
CONNECTION_CONNECTED = "connected"
CONNECTION_ERROR = "error"
CONNECTION_REVOKED = "revoked"


@dataclass(frozen=True)
class ExternalConnectionRecord:
    connection_id: str
    business_id: str
    provider: str
    account_id: str
    status: str
    token_ciphertext: str
    scopes: tuple[str, ...] = ()
    external_account_name: str | None = None
    external_location_name: str | None = None
    display_name: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    connected_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    last_error: str | None = None

    @property
    def is_ready(self) -> bool:
        return self.status == CONNECTION_CONNECTED and bool(
            self.external_account_name and self.external_location_name
        )


__all__ = [
    "ExternalConnectionRecord",
    "GOOGLE_BUSINESS_PROVIDER",
    "CONNECTION_PENDING",
    "CONNECTION_CONNECTED",
    "CONNECTION_ERROR",
    "CONNECTION_REVOKED",
]
