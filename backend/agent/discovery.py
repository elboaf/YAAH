"""LAN host discovery over mDNS (python-zeroconf).

Hosts with hosting_enabled advertise a ``_yaah._tcp.local.`` service on
their API port; clients browse for it to populate the host switcher.
The beacon carries only non-secret facts (protocol version, OS, whether
a passphrase is required) — never the passphrase itself. Advertising is
idempotent and started from the app lifespan; browse is a short blocking
sweep run off the event loop by the API layer.

Windows note: the first listen on a non-localhost socket triggers the
firewall prompt; a silent host is usually the firewall eating mDNS.
"""
import logging
import socket
import threading

log = logging.getLogger(__name__)

try:
    from zeroconf import ServiceInfo, Zeroconf
except ImportError:  # pragma: no cover — zeroconf is a hard dep, but a broken
    # sidecar build must not take the whole backend down at import time.
    ServiceInfo = Zeroconf = None

SERVICE_TYPE = "_yaah._tcp.local."

_zc: Zeroconf | None = None
_info: ServiceInfo | None = None
_lock = threading.Lock()


def _display_name() -> str:
    """Instance display name: config override, else the hostname."""
    from backend.agent.config import load_config

    name = (load_config().get("remote") or {}).get("display_name") or ""
    return name.strip() or socket.gethostname()


def _local_ip() -> str | None:
    """Best-effort LAN IP to advertise (UDP connect trick — no packets sent)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
        finally:
            s.close()
    except OSError:
        return None


def beacon_props() -> dict:
    """Shared by advertiser and tests: the properties a host broadcasts."""
    import platform

    from backend.agent.config import load_config
    from backend.agent.remote import INSTANCE_ID, PROTOCOL_VERSION, ensure_host_id

    passphrase_set = bool((load_config().get("remote") or {}).get("passphrase"))
    return {
        "proto": str(PROTOCOL_VERSION),
        "iid": INSTANCE_ID,
        "hid": ensure_host_id(),
        "os": platform.system() or "?",
        "auth": "1" if passphrase_set else "0",
    }


def start_advertising(port: int) -> None:
    """Advertise this backend as a host (idempotent). Never raises: a host
    that can't advertise is still reachable by direct IP."""
    global _zc, _info
    if Zeroconf is None:
        log.warning("zeroconf unavailable; host will not be discoverable")
        return
    with _lock:
        if _info is not None:
            return
        try:
            hostname = socket.gethostname().split(".")[0]
            # Service instance names must be ASCII printable; a non-ASCII
            # hostname still hosts fine, it's just advertised by a safe name.
            safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in hostname) or "yaah"
            props = beacon_props()
            props["name"] = _display_name()
            ip = _local_ip()
            info = ServiceInfo(
                SERVICE_TYPE,
                f"{safe}.{SERVICE_TYPE}",
                port=port,
                properties=props,
                addresses=[socket.inet_aton(ip)] if ip else None,
            )
            zc = Zeroconf()
            zc.register_service(info, ttl=None)
            _zc, _info = zc, info
        except Exception as e:  # noqa: BLE001 — advertising is best-effort
            log.warning("mDNS advertising failed: %s", e)


def stop_advertising() -> None:
    global _zc, _info
    with _lock:
        if _info is not None and _zc is not None:
            try:
                _zc.unregister_service(_info)
                _zc.close()
            except Exception:  # noqa: BLE001
                pass
        _zc, _info = None, None


class _Collector:
    def __init__(self):
        self.found: dict[str, dict] = {}

    def add_service(self, zc, type_: str, name: str) -> None:
        info = zc.get_service_info(type_, name)
        if info is None or info.port is None:
            return
        props = {
            k.decode() if isinstance(k, bytes) else k: v.decode() if isinstance(v, bytes) else v
            for k, v in (info.properties or {}).items()
        }
        ip = None
        if info.addresses:
            import struct

            ip = socket.inet_ntoa(struct.pack("I", struct.unpack("I", info.addresses[0])[0]))
        if not ip:
            return
        host = props.get("name") or name.split(f".{SERVICE_TYPE}")[0]
        self.found[f"{ip}:{info.port}"] = {
            "name": host,
            "host": ip,
            "port": info.port,
            "protocol": int(props.get("proto") or 0),
            "iid": props.get("iid") or "",
            "hid": props.get("hid") or "",
            "os": props.get("os") or "?",
            "auth": props.get("auth") == "1",
        }

    def update_service(self, zc, type_: str, name: str) -> None:  # noqa: ARG002
        pass

    def remove_service(self, zc, type_: str, name: str) -> None:  # noqa: ARG002
        pass


def browse(seconds: float = 2.5) -> list[dict]:
    """Sweep the LAN for hosts; blocking, so callers run it in a thread."""
    if Zeroconf is None:
        return []
    collector = _Collector()
    zc = None
    try:
        zc = Zeroconf()
        from zeroconf import ServiceBrowser

        ServiceBrowser(zc, SERVICE_TYPE, listener=collector)
        import time

        time.sleep(max(0.5, seconds))
    except Exception as e:  # noqa: BLE001
        log.warning("mDNS browse failed: %s", e)
    finally:
        if zc is not None:
            try:
                zc.close()
            except Exception:  # noqa: BLE001
                pass
    # Dedupe by instance id: a machine with several NICs (or a transient
    # double registration) must appear once in the switcher. Entries with
    # no iid fall back to name+port dedupe.
    seen: set = set()
    out = []
    for h in sorted(collector.found.values(), key=lambda h: h["name"]):
        key = h.get("iid") or f'{h["name"]}:{h["host"]}:{h["port"]}'
        if key in seen:
            continue
        seen.add(key)
        out.append(h)
    return out
