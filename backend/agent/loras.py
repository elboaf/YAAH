"""LoRA support for image generation (issue #398).

Three pieces, all degrade-to-noop by design - metadata absence is a
reportable condition, never an error path:

SCAN    At image-provider configure time: list the server's LoRAs
        (GET /sdapi/v1/loras), read each safetensors header from disk
        (8-byte LE u64 length + JSON; `__metadata__` only, never the
        weights), and classify every file:
          triggered      ss_tag_frequency non-empty -> has a trigger word
          triggerless    empty-caption signature, or ai-toolkit with no tag
          img2img_helper (bundled KB / user override knowledge only -
                         file metadata cannot express it)
          unknown        nothing conclusive - needs enrichment or the
                         /loras slash command
        Results persist in config under `lora_scan` keyed by api_base.
        The scan NEVER blocks provider save; it runs in the background.

KB      A bundled, hash-keyed knowledge base (bundled_skills/loras/lora_kb.json)
        of LoRAs that manual research classified - shipped so no future
        user repeats it. Lookup is BY SHA256 ONLY: filenames collide and
        lie; the hash is the identity. The hash is computed only for
        UNKNOWN-classified files (the population a KB entry can rescue)
        and for files a user documents via /loras - never for a whole
        directory on every scan.

APPLY   At generate_image time: consult the persisted scan (no scan work
        in the hot path), pick at most one matching LoRA, attach the
        structured `lora[]` request field, and inject the trigger word
        ONLY for the triggered class. `<lora:...>` prompt tags are never
        emitted (the server rejects them by design). img2img helpers key
        off init_image presence; their steering is an instruction prompt.
"""
import hashlib
import json
import logging
import re
from pathlib import Path

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Classification (validated on the live server's 13-file catalog, 2026-10-15)
# ---------------------------------------------------------------------------

CLASS_TRIGGERED = "triggered"
CLASS_TRIGGERLESS = "triggerless"
CLASS_IMG2IMG = "img2img_helper"
CLASS_UNKNOWN = "unknown"

# Multipliers. Triggered/triggerless: the 0.8 baseline the issue's catalog
# used across the board. img2img helpers: quality enhancers run gentler as
# a t2i fallback (the ultra_real entry's 0.6-0.8 band); editing LoRAs (the
# consistency trainer) want strength above 1 (its documented 1.6).
DEFAULT_MULTIPLIER = 0.8
TRIGGERLESS_MULTIPLIER = 0.8
IMG2IMG_T2I_MULTIPLIER = 0.7
IMG2IMG_MULTIPLIER = 1.6

# #406: style words so generic they would fire a filename-derived
# match on nearly every art prompt ("a photo of a castle" -> a
# Realistic_studio LoRA), defeating the conservative auto-apply bar.
# Name tokens in this set never become match criteria.
_STOP_TOKENS = {
    "style", "flux", "flux2", "klein", "9b", "v5", "pro", "ultra", "real",
    "checkpoint", "lora", "epoch", "final", "v1", "v2", "v3", "v4",
    # content-less fragments: 'ck' = checkpoint abbreviations, bare
    # version numbers, hash-ish step counters ('000005000').
    "ck", "sd", "v", "t", "000005000",
}

# Longest token that may still count as a style word: drops bare '2'
# fragments ('[FLUX.2.Klein]') and stray digits from filenames like
# 'Ghibli_style_Flux 9B'.
_MIN_TOKEN_LEN = 3


def _name_tokens(name: str) -> list[str]:
    """Style-word candidates derived from a LoRA's FILENAME (#406).

    Triggerless style LoRAs carry no training-time tokens, but their
    filenames describe them ('ck-neon-retrowave-klein-9b' IS a retrowave
    style). Split on separators, case-fold, drop the base-model noise and
    the stop words above. Used ONLY for the triggerless class: a
    triggered LoRA's training tokens are authoritative and stay
    untouched.
    """
    # bracket-grouped names ('[FLUX.2.Klein]PixelArt_Redmond') keep their
    # glued fragments out: brackets are noise, so strip them BEFORE the
    # separator split (a '[flux' fragment would never match a prompt).
    cleaned = str(name or "").replace("[", " ").replace("]", " ")
    raw = re.split(r"[\s_\-.]+", cleaned)
    out: list[str] = []
    for tok in raw:
        t = tok.lower().strip()
        if not t or t in _STOP_TOKENS or t in out:
            continue
        # digit-only or too-short fragments ('9', '2') say nothing about
        # the style.
        if t.isdigit() or len(t) < _MIN_TOKEN_LEN:
            continue
        out.append(t)
    return out


def classify_metadata(meta: dict | None) -> dict:
    """Classify one LoRA from its safetensors `__metadata__` (or None).

    Returns {class, trigger, base_model, match (strings to match against
    prompts)}. Never raises.

    Algorithm per issue #398, verified on all 12 catalog files:
      1. no __metadata__                     -> UNKNOWN until enriched
      2. ss_tag_frequency non-empty          -> TRIGGERED (keys are
         "N_<trigger>" with N = dataset repeat count; strip the prefix,
         split values on commas)
      3. ss_tag_frequency == {"1_": {"": 1}} -> TRIGGERLESS (empty-caption
         signature; "Civitai Spine"-trained files)
      4. ai-toolkit-trained, no ss_tag_frequency -> TRIGGERLESS (the
         toolkit records the tag only when a trigger was configured)
      5. base-model check: ss_base_model_version (ai-toolkit) or
         modelspec.architecture (sd-scripts) recorded for mismatch skips
    """
    if not isinstance(meta, dict) or not meta:
        return {
            "class": CLASS_UNKNOWN,
            "trigger": None,
            "base_model": None,
            "match": [],
        }
    base = (
        meta.get("ss_base_model_version")
        or meta.get("modelspec.architecture")
        or None
    )
    tags = meta.get("ss_tag_frequency")
    if isinstance(tags, str):
        # ai-toolkit records ss_tag_frequency as a JSON STRING inside the
        # metadata dict (verified on the live catalog: elusarca, PixelArt,
        # consistency-V2) - parse it before the shape checks below.
        try:
            tags = json.loads(tags)
        except ValueError:
            tags = None
    if isinstance(tags, dict) and tags:
        # {"1_elusarca_ancient_style": {"elusarca_ancient_style, photo": 12}}
        # -> trigger candidates from the keys (strip "N_" repeat prefix)
        # and from the caption values. Keys first: the training trigger is
        # the directory the images were collated into.
        triggers: list[str] = []
        for key in tags.keys():
            name = re.sub(r"^\d+_", "", str(key)).strip()
            if name and name not in triggers:
                triggers.append(name)
        for value in tags.values():
            if isinstance(value, dict):
                for caption in value.keys():
                    for token in str(caption).split(","):
                        token = token.strip()
                        if token and token not in triggers:
                            triggers.append(token)
        if triggers:
            out = {
                "class": CLASS_TRIGGERED,
                "trigger": triggers[0],
                "base_model": base,
                "match": triggers,
            }
            # One dataset dir whose every caption appears exactly once is
            # indistinguishable from a training-artifact tag (the
            # consistency-V2 "consistency" trap): flag it so the scan
            # hashes the file and lets the KB adjudicate.
            out["count1_signature"] = len(tags) == 1 and all(
                isinstance(v, dict) and v
                and all(c == 1 for c in v.values())
                for v in tags.values()
            )
            return out
    # Empty-caption signature (all files trained with empty captions) or an
    # ai-toolkit file with no tag record: triggerless either way.
    return {
        "class": CLASS_TRIGGERLESS,
        "trigger": None,
        "base_model": base,
        "match": [],
    }


def read_safetensors_metadata(path: Path) -> dict | None:
    """Read ONLY the header JSON of a safetensors file: 8-byte LE u64
    header length, then the JSON. The weights are never touched, so this
    stays cheap even on multi-GB files. None on anything unreadable or
    without a `__metadata__` block (absence is the UNKNOWN signal, not an
    error)."""
    try:
        with open(path, "rb") as f:
            raw_len = f.read(8)
            if len(raw_len) < 8:
                return None
            header_len = int.from_bytes(raw_len, "little", signed=False)
            if header_len <= 0 or header_len > 50_000_000:
                return None  # corrupt or not a safetensors file
            raw = f.read(min(header_len, 4_000_000))
        header = json.loads(raw)
    except (OSError, ValueError):
        return None
    if not isinstance(header, dict):
        return None
    meta = header.get("__metadata__")
    return meta if isinstance(meta, dict) else None


def sha256_file(path: Path) -> str | None:
    """SHA256 of a file, streamed (KB lookup keys on it). None on I/O
    failure - a missing hash can never produce a KB match."""
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


# ---------------------------------------------------------------------------
# Bundled knowledge base (issue #398 addendum). Hash-keyed; user entries
# (config `lora_overrides`) take precedence over builtin entries, which
# take precedence over the file-metadata scan.
# ---------------------------------------------------------------------------

def _kb_source() -> Path | None:
    """The KB lives in the bundled /loras skill folder: tauri ships
    bundled_skills/**/* as resources and skills.ensure_dir() copies the
    whole folder into the user's skills dir, so one file serves both."""
    from backend.agent.gitproc import bundle_dirs

    candidates = [
        base / "loras" / "lora_kb.json"
        for base in bundle_dirs("bundled_skills")
    ]
    candidates.append(
        Path(__file__).parent.parent / "bundled_skills" / "loras" / "lora_kb.json"
    )
    for c in candidates:
        if c.is_file():
            return c
    return None


def builtin_kb() -> dict:
    """The shipped knowledge base: sha256 -> entry. A missing/corrupt file
    yields {} (the KB is an enrichment, never a dependency)."""
    src = _kb_source()
    if src is None:
        return {}
    try:
        data = json.loads(src.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def user_overrides() -> dict:
    """User-authored KB entries from the /loras flow: sha256 -> entry, from
    config `lora_overrides`. A corrupt block degrades to {}."""
    try:
        from backend.agent.config import load_config

        data = load_config().get("lora_overrides")
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001 - config unreadable: scan still works
        return {}


def kb_lookup(sha256: str | None) -> dict | None:
    """Hash-only lookup: user entry > builtin KB. A filename is never
    sufficient to apply builtin knowledge (filenames collide and lie)."""
    if not sha256:
        return None
    entry = user_overrides().get(sha256)
    if isinstance(entry, dict) and entry:
        return {**entry, "source_of_truth": "user"}
    entry = builtin_kb().get(sha256)
    if isinstance(entry, dict) and entry:
        return {**entry, "source_of_truth": "builtin"}
    return None


def entry_is_contradiction(entry: dict, classification: dict) -> bool:
    """A hash-tied KB entry is authoritative, but if the file's own
    metadata claims a DIFFERENT conclusive class, surface it instead of
    silently overwriting. UNKNOWN files never "contradict" - they are
    exactly what the KB exists to rescue."""
    kls = entry.get("class")
    if not kls or kls == classification.get("class"):
        return False
    return classification.get("class") in (CLASS_TRIGGERED, CLASS_TRIGGERLESS)


# ---------------------------------------------------------------------------
# Server process discovery: which directory holds the LoRA files?
# ---------------------------------------------------------------------------

_LORA_FLAG_RE = re.compile(r"--lora-model-dir(?:[=\s]+)(\"[^\"]+\"|[^\s]+)")


def discover_lora_dir(api_base: str) -> Path | None:
    """Locate the sd-server's LoRA directory by reading the command line of
    the process listening on api_base's port (`--lora-model-dir`, how the
    operator's own deployment runs). Returns None on anything unexpected -
    the caller reports UNKNOWN files and moves on. The server's HTTP API
    does not expose the directory (verified against api.md and the live
    server), so the local process is the only source. psutil is an
    optional dependency: without it, discovery degrades to None."""
    if not api_base:
        return None
    m = re.match(r"^https?://([^/:]+):(\d+)", api_base.strip())
    if not m:
        return None
    host, port = m.group(1), m.group(2)
    if host not in ("127.0.0.1", "localhost", "::1"):
        return None  # remote server: files live on another machine
    try:
        import psutil

        for proc in psutil.process_iter(["cmdline"]):
            try:
                cmd = proc.info.get("cmdline") or []
            except Exception:  # noqa: BLE001 - vanished/protected process
                continue
            if not cmd:
                continue
            try:
                listening = False
                for c in proc.net_connections(kind="tcp"):
                    if (
                        c.status == psutil.CONN_LISTEN
                        and c.laddr
                        and str(c.laddr.port) == port
                    ):
                        listening = True
                        break
            except Exception:  # noqa: BLE001 - perms/collapsed process
                continue
            if not listening:
                continue
            joined = " ".join(str(c) for c in cmd)
            dm = _LORA_FLAG_RE.search(joined)
            if not dm:
                return None
            p = Path(dm.group(1).strip('"'))
            if p.is_dir():
                return p
            # Relative flag values (models/loras) resolve against the
            # SERVER's working directory, not ours.
            try:
                cwd = proc.cwd()
            except Exception:  # noqa: BLE001 - access denied / gone
                cwd = None
            if cwd:
                q = Path(cwd) / p
                if q.is_dir():
                    return q
            return None
    except ImportError:
        return None  # psutil absent: degrade to UNKNOWN, never fail
    except Exception:  # noqa: BLE001 - discovery is best-effort by contract
        return None
    return None


# ---------------------------------------------------------------------------
# The scan itself
# ---------------------------------------------------------------------------

async def fetch_lora_list(api_base: str) -> list[dict] | None:
    """GET {api_base}/sdapi/v1/loras -> [{name, path}]. None on any
    failure (server down, old build without the endpoint)."""
    import httpx

    try:
        async with httpx.AsyncClient(timeout=6.0) as client:
            r = await client.get(api_base.rstrip("/") + "/sdapi/v1/loras")
        if r.status_code != 200:
            return None
        data = r.json()
    except (httpx.HTTPError, ValueError):
        return None
    if not isinstance(data, list):
        return None
    out = []
    for item in data:
        if isinstance(item, dict) and item.get("path"):
            out.append(
                {"name": item.get("name") or item["path"], "path": item["path"]}
            )
    return out


def scan_loras(
    lora_dir: Path | None,
    entries: list[dict],
    prev: dict | None = None,
) -> dict:
    """Classify every listed LoRA. Pure function of (dir, entries) so the
    test suite can drive it with fixtures. Shape:

        {scanned_at, lora_dir, files: {path: {name, class, trigger,
         multiplier, base_model, match, sha256?, kb?, warning?}},
         unknown_count}

    SHA256 is computed ONLY for UNKNOWN-classified files (the population
    a KB entry can rescue) - never for the whole directory on every scan.
    `prev` (the persisted previous scan for this server) donates the hash
    a file already carried, so a KB entry added after that scan can be
    consulted - and a contradiction surfaced - on THIS scan without
    rehashing classified files.
    """
    import datetime

    prev_files = (prev or {}).get("files") or {}
    files: dict = {}
    unknown = 0
    for entry in entries:
        rel = entry["path"]
        info: dict = {
            "name": entry["name"],
            "class": CLASS_UNKNOWN,
            "trigger": None,
            "multiplier": None,
            "base_model": None,
            "match": [],
        }
        classification = {
            "class": CLASS_UNKNOWN,
            "trigger": None,
            "base_model": None,
            "match": [],
        }
        resolved = None
        if lora_dir is not None:
            # `path` is server-relative to the LoRA dir; refuse escapes.
            p = (lora_dir / rel).resolve()
            root = lora_dir.resolve()
            if root == p or root in p.parents:
                resolved = p
                classification = classify_metadata(
                    read_safetensors_metadata(p)
                )
            else:
                log.warning("lora path escapes lora dir: %s", rel)
        info.update(classification)
        digest = None
        hash_worthy = info["class"] == CLASS_UNKNOWN or (
            info["class"] == CLASS_TRIGGERED
            and classification.get("count1_signature")
        )
        if hash_worthy and resolved is not None:
            digest = sha256_file(resolved)
        else:
            # already classified on metadata: reuse the hash a previous
            # scan persisted for this path, if any
            digest = (prev_files.get(rel) or {}).get("sha256")
        if digest:
            info["sha256"] = digest
            kb = kb_lookup(digest)
            if kb:
                info["kb"] = kb
                info["class"] = kb.get("class", CLASS_UNKNOWN)
                info["trigger"] = kb.get("trigger")
                info["multiplier"] = _kb_multiplier(kb, info["class"])
                if entry_is_contradiction(kb, classification):
                    info["warning"] = (
                        "bundled knowledge says "
                        f"'{kb.get('class')}' but the file's own "
                        f"metadata says '{classification['class']}'"
                    )
        if info["class"] == CLASS_UNKNOWN:
            unknown += 1
        files[rel] = info
    return {
        "scanned_at": datetime.datetime.now(datetime.UTC).isoformat(
            timespec="seconds"
        ),
        "lora_dir": str(lora_dir) if lora_dir else None,
        "files": files,
        "unknown_count": unknown,
    }


def _kb_multiplier(kb: dict, kls: str) -> float:
    """Multiplier from a KB entry's `multiplier` map ({t2i_fallback: [lo,
    hi]}, {img2img: [lo, hi]}), falling back to the class default."""
    mult = kb.get("multiplier")
    if isinstance(mult, dict):
        for key in ("t2i_fallback", "img2img", "t2i"):
            rng = mult.get(key)
            if isinstance(rng, (list, tuple)) and rng:
                try:
                    return float(rng[0])
                except (TypeError, ValueError):
                    pass
    return {CLASS_IMG2IMG: IMG2IMG_T2I_MULTIPLIER}.get(kls, DEFAULT_MULTIPLIER)


async def run_scan(api_base: str) -> dict:
    """The whole scan for one api_base: server list + dir discovery +
    classification. Never raises; a failed list yields an empty scan
    (which the settings notice treats as 'nothing to say')."""
    entries = await fetch_lora_list(api_base)
    if not entries:
        return {"scanned_at": None, "files": {}, "unknown_count": 0}
    lora_dir = discover_lora_dir(api_base)
    prev = load_scan(api_base)
    # Header reads + (UNKNOWN-file) hashing are disk-bound: to_thread, so
    # a multi-GB file's SHA256 never stalls the backend's event loop.
    import asyncio

    return await asyncio.to_thread(scan_loras, lora_dir, entries, prev)


# ---------------------------------------------------------------------------
# Persistence: config `lora_scan` (keyed by api_base) + `lora_overrides`
# (keyed by sha256, written by the /loras flow).
# ---------------------------------------------------------------------------

def _scan_key(api_base: str) -> str:
    return (api_base or "").strip().rstrip("/").lower()


def load_scan(api_base: str) -> dict:
    """The persisted scan for api_base, or {}. Read live so a fresh scan
    applies to the next generation without a restart."""
    try:
        from backend.agent.config import load_config

        scans = load_config().get("lora_scan")
        if isinstance(scans, dict):
            scan = scans.get(_scan_key(api_base))
            if isinstance(scan, dict):
                return scan
    except Exception:  # noqa: BLE001 - unreadable config = no auto-apply
        pass
    return {}


def save_scan(api_base: str, scan: dict) -> None:
    """Persist one api_base's scan. The map keeps OTHER servers' scans
    intact (a user may point Settings at a second sd.cpp server without
    losing the first's classifications)."""
    from backend.agent.config import load_config, save_config

    cfg = load_config()
    scans = dict(cfg.get("lora_scan") or {})
    scans[_scan_key(api_base)] = scan
    save_config({"lora_scan": scans})


def save_override(sha256: str, entry: dict) -> None:
    """Write one user KB entry (from the /loras flow)."""
    from backend.agent.config import load_config, save_config

    cfg = load_config()
    overrides = dict(cfg.get("lora_overrides") or {})
    overrides[sha256] = entry
    save_config({"lora_overrides": overrides})


# ---------------------------------------------------------------------------
# Auto-apply: the generate_image hot path. Consults the persisted scan
# only - no server calls, no header reads, no hashing here.
# ---------------------------------------------------------------------------

def _squash(s: str) -> str:
    """Case-folded, separator-free form of a string: 'Retro-Wave' and
    'retro_wave' and 'retrowave' all collapse to the same run of letters.
    Token matching runs on BOTH the spaced and the squashed form (#406):
    prompts and filenames disagree about where the separators go."""
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def _match_score(info: dict, prompt: str) -> float:
    """How strongly a LoRA's match tokens hit a prompt: the fraction of
    tokens present, 0 = no match. An empty match list never fires on
    content - select_lora derives filename tokens for the triggerless
    class (see there) and enforces the conservative bar."""
    # Separators are unreliable spelling: the live-session prompt said
    # "retro-wave" for a 'retrowave'-named LoRA (#406). A token hits when
    # it appears in the spaced form ("pix art" in "pix art castle") OR
    # its squashed form equals a squashed prompt word/bigram
    # ("retrowave" == "retro wave" glued).
    prompt_l = re.sub(r"[\s_\-]+", " ", (prompt or "").lower())
    # squashed WORD list plus squashed BIGRAM glues: 'phone' must match a
    # whole prompt word (never the interior of 'photography'), while
    # 'retrowave' matches the glue of adjacent words 'retro wave' - the
    # separator disagreement is exactly what squashing erases (#406).
    words = [
        w for w in re.split(r"[^a-z0-9]+", (prompt or "").lower()) if w
    ]
    prompt_sq_words = {_squash(w) for w in words}
    # bigram glue: ADJACENT prompt words in ORDER, squashed and
    # concatenated - 'retro wave' -> 'retrowave' is exactly the spelling
    # collision (a set-iteration order would shuffle the pairs).
    sq_words = [_squash(w) for w in words]
    prompt_sq_words |= {
        sq_words[i] + sq_words[i + 1] for i in range(len(sq_words) - 1)
    }
    tokens: list[str] = []
    for m in info.get("match") or []:
        # match entries can be whole caption lines ("Pixel Art, PixArFK"):
        # score token-by-token so a prompt naming any ingredient hits.
        # Underscores normalize to spaces: training triggers are
        # snake_case by convention, prompts use either form.
        for part in re.sub(r"[\s_\-]+", " ", str(m)).split(","):
            part = part.strip().lower()
            if part and part not in tokens:
                tokens.append(part)
    if not tokens:
        return 0.0
    hits = 0
    for t in tokens:
        # spaced-substring OR squashed-word hit: "phone" inside
        # "photography" is a prefix collision, not a style match - the
        # squashed check requires the squashed token to BE one of the
        # prompt's squashed words/bigrams, never an interior fragment.
        if t in prompt_l or _squash(t) in prompt_sq_words:
            hits += 1
    if not hits:
        return 0.0
    return hits / len(tokens)


def select_lora(scan: dict, prompt: str, has_init_image: bool) -> dict | None:
    """Pick at most ONE LoRA for a request (v1: no stacking). Returns
    {path, multiplier, trigger, name, cls} or None.

    - img2img helpers apply only when an init image is present (they are
      enhancers/editors; in plain t2i they would drag every render).
    - triggered/triggerless classes apply on match only.
    - never invents a trigger word; a triggered class without a
      surviving trigger is skipped, not guessed.
    """
    files = (scan or {}).get("files") or {}
    if not files:
        return None
    best: tuple[float, dict] | None = None
    for path, info in files.items():
        kls = info.get("class")
        cand: tuple[float, dict] | None = None
        if kls == CLASS_IMG2IMG:
            if has_init_image:
                cand = (
                    1.0,
                    {
                        "path": path,
                        "multiplier": float(
                            info.get("multiplier") or IMG2IMG_MULTIPLIER
                        ),
                        "trigger": None,
                        "name": info.get("name") or path,
                        "cls": kls,
                    },
                )
        elif kls in (CLASS_TRIGGERED, CLASS_TRIGGERLESS):
            trigger = info.get("trigger") if kls == CLASS_TRIGGERED else None
            if kls == CLASS_TRIGGERED and not trigger:
                cand = None  # classified triggered, trigger lost: skip
            else:
                # #406: a triggerless style LoRA has no training tokens -
                # derive match criteria from its FILENAME so a prompt
                # naming the style ("retro-wave") can summon it
                # ("ck-neon-retrowave-klein-9b"). HALF the derived tokens
                # (at least one) must hit: 'neon'+'retrowave' against
                # "retro-wave style" hits one of two, and that IS the
                # prompt naming the style - the separator squash makes
                # the spelling difference not matter. Still conservative:
                # a generic prompt ("a cozy village") scores 0. Triggered
                # class keeps score>0 (training-token presence).
                if kls == CLASS_TRIGGERLESS and not info.get("match"):
                    name_toks = _name_tokens(info.get("name") or path)
                    if name_toks:
                        info = {**info, "match": name_toks, "match_from": "filename"}
                score = _match_score(info, prompt)
                if score > 0 and (
                    info.get("match_from") != "filename"
                    or score >= 0.5
                ):
                    cand = (
                        score,
                        {
                            "path": path,
                            "multiplier": float(
                                info.get("multiplier")
                                or (
                                    DEFAULT_MULTIPLIER
                                    if kls == CLASS_TRIGGERED
                                    else TRIGGERLESS_MULTIPLIER
                                )
                            ),
                            "trigger": trigger,
                            "name": info.get("name") or path,
                            "cls": kls,
                        },
                    )
        if cand and (best is None or cand[0] > best[0]):
            best = cand
    return best[1] if best else None


def apply_to_body(body: dict, scan: dict, prompt: str) -> dict:
    """Attach `lora[]` (and inject the trigger word when a triggered LoRA
    matched) onto an img_gen request body. Mutates and returns `body`.
    Any failure yields the body unchanged - auto-apply must never break
    a generation that worked without it."""
    try:
        has_init = bool(body.get("init_image"))
        sel = select_lora(scan, prompt, has_init)
        if not sel:
            return body
        body["lora"] = [
            {
                "path": sel["path"],
                "multiplier": sel["multiplier"],
                "is_high_noise": False,
            }
        ]
        if sel.get("trigger"):
            # Trigger word = steering wheel (training-time association);
            # injected at the head so it anchors the style tokens.
            body["prompt"] = f"{sel['trigger']}, {prompt}"
            body["lora"][0]["trigger_injected"] = sel["trigger"]
        body["lora_applied"] = sel["name"]
        return body
    except Exception:  # noqa: BLE001 - never break the request
        return body
