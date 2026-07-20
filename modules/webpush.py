"""
Web Push — server-initiated notifications to a phone that isn't open.

Everything else in this codebase notifies a browser that is already running.
Web Push is the only mechanism that reaches a device with the screen off, and
it is what makes a request ("get milk?") arrive when it matters rather than
when someone next opens the page.

What the relay can see
----------------------
Delivery goes through the browser vendor's push service (Google, Mozilla,
Apple). That is unavoidable — the endpoint is baked into the subscription the
browser issues. It is also not a plaintext exposure: RFC 8291 encrypts the
payload with keys only the subscriber's browser holds, so the relay carries
ciphertext it cannot read. It learns that a message went to a device, and its
size. Nothing else.

VAPID (RFC 8292) is the other half: the hub signs each request with a key pair
it generates once, so the push service can attribute traffic to this server and
a stranger cannot push to your subscribers using a stolen endpoint.

Why implemented here rather than via pywebpush
----------------------------------------------
This is a self-contained transform — ECDH, HKDF, one AES-GCM seal — not a
stateful protocol with sessions and ratchets. Implementing it over vetted
primitives (`cryptography`) is standard practice, and costs one dependency
instead of three. The round-trip is unit-tested by decrypting our own output,
which is the property that actually matters.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import struct
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, utils as asym_utils
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

logger = logging.getLogger("modules.webpush")

KEYS_PATH = Path("./data/vapid.json")
SUBS_PATH = Path("./data/push_subscriptions.yaml")

# RFC 8291 record size. One record is plenty: notification payloads are a
# sentence, and multi-record framing exists for streaming bodies we never send.
RECORD_SIZE = 4096

# Push services reject long-lived JWTs. 12h is the usual ceiling; we re-sign
# per send anyway, so there is no reason to sit near it.
VAPID_TTL_S = 12 * 3600

# How long the push service should hold an undelivered message. A request that
# outlives its own timeout is worse than no message at all — it invites someone
# to accept an ask that already lapsed.
DEFAULT_TTL_S = 900


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64url_decode(s: str) -> bytes:
    s = s.strip()
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


# --------------------------------------------------------------------------
# VAPID identity
# --------------------------------------------------------------------------

class VapidKeys:
    """
    The hub's push identity. Generated once and persisted.

    Rotating these invalidates every existing subscription — browsers bind a
    subscription to the applicationServerKey it was created with — so the file
    is written once and left alone.
    """

    def __init__(self, private_key: ec.EllipticCurvePrivateKey, subject: str) -> None:
        self.private_key = private_key
        self.subject = subject

    @property
    def public_bytes(self) -> bytes:
        return self.private_key.public_key().public_bytes(
            serialization.Encoding.X962,
            serialization.PublicFormat.UncompressedPoint,
        )

    @property
    def public_b64(self) -> str:
        """The applicationServerKey the browser needs to subscribe."""
        return b64url(self.public_bytes)

    @classmethod
    def load_or_create(cls, path: Path = KEYS_PATH,
                       subject: str = "mailto:admin@localhost") -> "VapidKeys":
        path = Path(path)
        if path.exists():
            try:
                d = json.loads(path.read_text())
                key = serialization.load_pem_private_key(
                    d["private_pem"].encode(), password=None)
                return cls(key, d.get("subject") or subject)
            except Exception as e:                              # noqa: BLE001
                # Regenerating would silently break every existing
                # subscription, so refuse rather than "recover".
                raise RuntimeError(
                    f"VAPID key file {path} is unreadable ({e}). Move it aside "
                    f"to generate a new identity — every device will then need "
                    f"to re-subscribe."
                ) from e

        key = ec.generate_private_key(ec.SECP256R1())
        path.parent.mkdir(parents=True, exist_ok=True)
        pem = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode()
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"private_pem": pem, "subject": subject}, indent=1))
        tmp.replace(path)
        try:
            path.chmod(0o600)      # a private key; not world-readable
        except OSError:
            pass
        logger.info("[push] generated a new VAPID identity at %s", path)
        return cls(key, subject)

    def auth_header(self, endpoint: str) -> str:
        """VAPID Authorization header for one push endpoint."""
        from urllib.parse import urlparse
        u = urlparse(endpoint)
        aud = f"{u.scheme}://{u.netloc}"

        header = b64url(json.dumps({"typ": "JWT", "alg": "ES256"},
                                   separators=(",", ":")).encode())
        claims = b64url(json.dumps({
            "aud": aud,
            "exp": int(time.time()) + VAPID_TTL_S,
            "sub": self.subject,
        }, separators=(",", ":")).encode())
        signing_input = f"{header}.{claims}".encode()

        der = self.private_key.sign(signing_input, ec.ECDSA(hashes.SHA256()))
        # JWS wants raw r||s, fixed width. DER is variable-length and would be
        # rejected — a classic and silent ES256 mistake.
        r, s = asym_utils.decode_dss_signature(der)
        raw = r.to_bytes(32, "big") + s.to_bytes(32, "big")

        jwt = f"{header}.{claims}.{b64url(raw)}"
        return f"vapid t={jwt}, k={self.public_b64}"


# --------------------------------------------------------------------------
# Payload encryption (RFC 8291, aes128gcm)
# --------------------------------------------------------------------------

def encrypt_payload(plaintext: bytes, p256dh_b64: str, auth_b64: str) -> bytes:
    """
    Seal a payload for one subscriber.

    Returns a complete aes128gcm body: header (salt, record size, server public
    key) followed by the ciphertext, exactly as the push service expects to
    forward it untouched.
    """
    ua_public_bytes = b64url_decode(p256dh_b64)
    auth_secret = b64url_decode(auth_b64)

    ua_public = ec.EllipticCurvePublicKey.from_encoded_point(
        ec.SECP256R1(), ua_public_bytes)

    # Fresh key pair per message: reusing one would let a compromised key open
    # every past payload.
    as_private = ec.generate_private_key(ec.SECP256R1())
    as_public_bytes = as_private.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)

    shared = as_private.exchange(ec.ECDH(), ua_public)

    # RFC 8291 §3.4 — the auth secret is the HKDF *salt* here, and the info
    # string binds both public keys so a captured payload cannot be replayed
    # at a different subscriber.
    prk = HKDF(
        algorithm=hashes.SHA256(), length=32, salt=auth_secret,
        info=b"WebPush: info\x00" + ua_public_bytes + as_public_bytes,
    ).derive(shared)

    salt = os.urandom(16)
    cek = HKDF(algorithm=hashes.SHA256(), length=16, salt=salt,
               info=b"Content-Encoding: aes128gcm\x00").derive(prk)
    nonce = HKDF(algorithm=hashes.SHA256(), length=12, salt=salt,
                 info=b"Content-Encoding: nonce\x00").derive(prk)

    # 0x02 is the last-record delimiter. 0x01 would mean "more records follow"
    # and the browser would wait for one that never arrives.
    padded = plaintext + b"\x02"
    ciphertext = AESGCM(cek).encrypt(nonce, padded, None)

    header = salt + struct.pack("!L", RECORD_SIZE) + \
        bytes([len(as_public_bytes)]) + as_public_bytes
    return header + ciphertext


def decrypt_payload(body: bytes, ua_private: ec.EllipticCurvePrivateKey,
                    auth_secret: bytes) -> bytes:
    """
    Inverse of [encrypt_payload]. Exists so the round trip can be tested.

    Encryption that is never decrypted is encryption that has never been
    checked — a payload the browser silently drops looks exactly like a
    notification nobody tapped.
    """
    salt = body[:16]
    idlen = body[20]
    as_public_bytes = body[21:21 + idlen]
    ciphertext = body[21 + idlen:]

    as_public = ec.EllipticCurvePublicKey.from_encoded_point(
        ec.SECP256R1(), as_public_bytes)
    shared = ua_private.exchange(ec.ECDH(), as_public)

    ua_public_bytes = ua_private.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)

    prk = HKDF(
        algorithm=hashes.SHA256(), length=32, salt=auth_secret,
        info=b"WebPush: info\x00" + ua_public_bytes + as_public_bytes,
    ).derive(shared)
    cek = HKDF(algorithm=hashes.SHA256(), length=16, salt=salt,
               info=b"Content-Encoding: aes128gcm\x00").derive(prk)
    nonce = HKDF(algorithm=hashes.SHA256(), length=12, salt=salt,
                 info=b"Content-Encoding: nonce\x00").derive(prk)

    padded = AESGCM(cek).decrypt(nonce, ciphertext, None)
    return padded.rstrip(b"\x02")


# --------------------------------------------------------------------------
# Subscriptions
# --------------------------------------------------------------------------

@dataclass
class Subscription:
    id: str
    user: str
    endpoint: str
    p256dh: str
    auth: str
    label: str = ""
    created_at: float = 0.0
    last_ok_at: Optional[float] = None
    failures: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def public_view(self) -> Dict[str, Any]:
        # Never hand the keys back out; they are all an attacker needs to
        # decrypt anything sent to this device.
        d = self.to_dict()
        d.pop("p256dh", None)
        d.pop("auth", None)
        d["endpoint_host"] = self.endpoint.split("/")[2] if "//" in self.endpoint else ""
        d.pop("endpoint", None)
        return d


class PushManager:
    """VAPID identity, subscriptions, and delivery."""

    # A push service returning 404/410 means the subscription is dead for good.
    GONE_STATUSES = (404, 410)
    # Give up on a subscription that keeps failing for softer reasons.
    MAX_FAILURES = 10

    def __init__(self, keys: VapidKeys, subs_path: Path = SUBS_PATH) -> None:
        self.keys = keys
        self.subs_path = Path(subs_path)
        self.subs: Dict[str, Subscription] = {}

    # -- persistence -------------------------------------------------------

    def load(self) -> None:
        if not self.subs_path.exists():
            return
        try:
            raw = yaml.safe_load(self.subs_path.read_text()) or {}
        except Exception as e:                                  # noqa: BLE001
            logger.error("[push] could not read %s: %s", self.subs_path, e)
            return
        for entry in (raw.get("subscriptions") or []):
            try:
                s = Subscription(**entry)
            except Exception:                                   # noqa: BLE001
                continue
            self.subs[s.id] = s
        logger.info("[push] loaded %d subscription(s)", len(self.subs))

    def save(self) -> None:
        try:
            self.subs_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.subs_path.with_suffix(".tmp")
            tmp.write_text(yaml.safe_dump(
                {"subscriptions": [s.to_dict() for s in self.subs.values()]},
                sort_keys=False))
            tmp.replace(self.subs_path)
            try:
                self.subs_path.chmod(0o600)   # contains per-device secrets
            except OSError:
                pass
        except OSError as e:
            logger.error("[push] save failed: %s", e)

    # -- registration ------------------------------------------------------

    def subscribe(self, user: str, endpoint: str, p256dh: str, auth: str,
                  label: str = "") -> Dict[str, Any]:
        if not (endpoint and p256dh and auth):
            return {"success": False, "error": "Incomplete subscription"}
        if not endpoint.startswith("https://"):
            return {"success": False, "error": "Endpoint must be https"}

        import hashlib
        sub_id = hashlib.sha256(endpoint.encode()).hexdigest()[:16]

        # Re-subscribing the same browser must update, not duplicate — the
        # endpoint is the device's identity and browsers re-issue it freely.
        existing = self.subs.get(sub_id)
        self.subs[sub_id] = Subscription(
            id=sub_id, user=user, endpoint=endpoint, p256dh=p256dh, auth=auth,
            label=label or (existing.label if existing else ""),
            created_at=existing.created_at if existing else time.time(),
        )
        self.save()
        logger.info("[push] %s subscribed (%s)", user, sub_id)
        return {"success": True, "id": sub_id}

    def unsubscribe(self, sub_id: str, user: Optional[str] = None) -> Dict[str, Any]:
        s = self.subs.get(sub_id)
        if not s:
            return {"success": False, "error": "Not found"}
        if user is not None and s.user != user:
            return {"success": False, "error": "Not your subscription"}
        del self.subs[sub_id]
        self.save()
        return {"success": True}

    def for_user(self, user: str) -> List[Subscription]:
        return [s for s in self.subs.values() if s.user == user]

    # -- delivery ----------------------------------------------------------

    async def send_to_user(self, user: str, payload: Dict[str, Any],
                           ttl_s: int = DEFAULT_TTL_S) -> Dict[str, Any]:
        """
        Push to every device this person has registered.

        Reports per-device outcomes rather than a single boolean: "sent" when
        one of three phones took it is a different fact from "sent" when all
        three did, and a caller deciding whether to escalate needs to know.
        """
        targets = self.for_user(user)
        if not targets:
            return {"sent": 0, "failed": 0, "no_subscriptions": True}

        body = json.dumps(payload).encode()
        sent = failed = 0
        for s in targets:
            ok = await self._send_one(s, body, ttl_s)
            if ok:
                sent += 1
            else:
                failed += 1
        if sent or failed:
            self.save()
        return {"sent": sent, "failed": failed, "no_subscriptions": False}

    async def _send_one(self, s: Subscription, body: bytes, ttl_s: int) -> bool:
        try:
            encrypted = encrypt_payload(body, s.p256dh, s.auth)
        except Exception as e:                                  # noqa: BLE001
            logger.warning("[push] encrypt failed for %s: %s", s.id, e)
            return False

        headers = {
            "Authorization": self.keys.auth_header(s.endpoint),
            "Content-Encoding": "aes128gcm",
            "Content-Type": "application/octet-stream",
            "TTL": str(ttl_s),
            # Low urgency would let the service batch this behind a screen-on
            # event, which defeats a time-limited request.
            "Urgency": "high",
        }

        try:
            import httpx
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.post(s.endpoint, content=encrypted, headers=headers)
            status = r.status_code
        except Exception as e:                                  # noqa: BLE001
            logger.warning("[push] send to %s failed: %s", s.id, e)
            s.failures += 1
            return False

        if 200 <= status < 300:
            s.last_ok_at = time.time()
            s.failures = 0
            return True

        if status in self.GONE_STATUSES:
            # The browser dropped this subscription. Keeping it would mean
            # retrying forever against an endpoint that can never succeed.
            logger.info("[push] subscription %s is gone (%d); removing", s.id, status)
            self.subs.pop(s.id, None)
            return False

        s.failures += 1
        logger.warning("[push] %s returned %d (failures=%d)", s.id, status, s.failures)
        if s.failures >= self.MAX_FAILURES:
            logger.info("[push] dropping %s after %d failures", s.id, s.failures)
            self.subs.pop(s.id, None)
        return False


_manager: Optional[PushManager] = None


def get_push_manager() -> Optional[PushManager]:
    return _manager


def set_push_manager(m: PushManager) -> None:
    global _manager
    _manager = m
