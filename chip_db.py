# -*- coding: utf-8 -*-
"""
chip_db.py  —  ChipDB: load chip_db.db (SQLite) and resolve form chip strings.

Usage:
    db = ChipDB.load()
    hwid, support, ports = db.lookup("TI TCA9554PWR", "GPIO")
    hwid, support, ports = db.lookup("Rdc Semi A9620LIF2A(EIO-211)", "HWM/Voltage")
    # hwid=None → chip not found or category unavailable → caller emits empty value

Direct SQL queries (outside Python):
    sqlite3 chip_db.db "SELECT name FROM chips ORDER BY name;"
    sqlite3 chip_db.db "SELECT c.name, cat.category, cat.hwid
                        FROM chips c JOIN categories cat ON cat.chip_id=c.id
                        WHERE cat.category='GPIO';"
"""

import re
import sqlite3
import json
import os
from difflib import SequenceMatcher
from pathlib import Path

# ---------------------------------------------------------------------------
# Curated aliases: model keyword → chip_db chips.name
# Needed when the form model number differs from the XML filename stem.
# ---------------------------------------------------------------------------
_ALIASES: dict[str, str] = {
    "EIO-211":   "Advantech EIO_IS200",
    "EIO211":    "Advantech EIO_IS200",
    "EIO-201":   "Advantech EIO_IS200",
    "EIO201":    "Advantech EIO_IS200",
    "EIO_IS200": "Advantech EIO_IS200",
    "EIOIS200":  "Advantech EIO_IS200",
    # EIO-300 is an Advantech module (EC compatible with EIO_IS200) + NCT6694B for GPIO.
    # The EC part uses the same HWID (0x30313640) as EIO_IS200 for all HWM/WDT/SMBus functions.
    "EIO-300":   "Advantech EIO_IS200",
    "EIO300":    "Advantech EIO_IS200",
    # ITE8528 is an Advantech EC chip sharing the same DB entry / HWID (0x0000FFFF)
    "ITE8528":   "Advantech EC",
    # IT-5782VG appears in SUSI request forms as an EC identifier; map to
    # Advantech EC family so WDT/SMBus/VGA/HWM/GPIO categories resolve.
    "IT-5782VG": "Advantech EC",
    "IT5782VG":  "Advantech EC",
    # TI TCA955x ↔ NXP PCA955x: pin-compatible, same HWID in DB
    "TCA9554":   "PCA9554",
    "TCA9538":   "PCA9538",
    "TCA9539":   "PCA9539",
    "TCA9555":   "PCA9555",
    "TCA9698":   "PCA9698",
}

# EC companion modules: chips that never serve EC-side functions alone on
# Advantech boards. NCT6694B is the USB peripheral-expander half of the
# EIO-300 module; the EC half shares HWID 0x30313640 with EIO_IS200
# (verified against the MIO-5354 golden INI: TCPU/WDT1/Area0/TPCH0 all use
# 0x30313640). Consulted in find_chip_key_for_category() only AFTER the
# deterministic alias/exact/substring pass fails, so categories the chip
# itself owns (NCT6694B: GPIO/I2C/SMBus) still resolve to its own HWIDs.
_EC_COMPANION: dict[str, str] = {
    "NCT6694B": "Advantech EIO_IS200",
}


def _normalize(s: str) -> str:
    return re.sub(r"[\s\-_.]", "", s).upper()


# Model-number tokens for the fuzzy-match family guard.
# "ITE" is folded into "IT" so ITE8528 and IT8528 count as the same token.
# Lookbehind instead of \b: underscore is a word char, so \b would miss the
# model in strings like "Nuvoton_NCT6694B".
_MODEL_TOKEN = re.compile(r"(?<![A-Za-z0-9])(NCT|ITE?|PCA|TCA|EIO)[-_ ]?(\d{3,})", re.IGNORECASE)


def _model_tokens(s: str) -> set[tuple[str, str]]:
    """Extract (family, number) tokens, e.g. 'Nuvoton NCT6694B' -> {('NCT','6694')}."""
    out = set()
    for fam, num in _MODEL_TOKEN.findall(s or ""):
        fam = fam.upper()
        if fam == "ITE":
            fam = "IT"
        out.add((fam, num))
    return out


def _fuzzy_family_conflict(query: str, db_name: str) -> bool:
    """True when both sides carry model numbers of the same chip family but no
    number matches — e.g. NCT6694B must never fuzzy-match NCT5523D. Such pairs
    look similar to a ratio matcher but are entirely different silicon."""
    q = _model_tokens(query)
    b = _model_tokens(db_name)
    if not q or not b or (q & b):
        return False
    return bool({f for f, _ in q} & {f for f, _ in b})


class ChipDB:
    def __init__(self, db_path: Path):
        self._con = sqlite3.connect(str(db_path))
        self._con.row_factory = sqlite3.Row

        # Build in-memory normalized index for fast chip name resolution
        # normalize(chip name) → chip name  (used in find_chip_key)
        rows = self._con.execute("SELECT name FROM chips").fetchall()
        self._norm_index: dict[str, str] = {
            _normalize(r["name"]): r["name"] for r in rows
        }
        # Precompute normalized alias keys
        self._norm_aliases: dict[str, str] = {
            _normalize(k): v for k, v in _ALIASES.items()
        }
        # Precompute normalized EC-companion keys
        self._norm_companions: dict[str, str] = {
            _normalize(k): v for k, v in _EC_COMPANION.items()
        }

    def _has_category(self, chip_name: str, category: str) -> bool:
        row = self._con.execute(
            """
            SELECT 1
            FROM chips c
            JOIN categories cat ON cat.chip_id = c.id
            WHERE c.name = ? AND cat.category = ?
            LIMIT 1
            """,
            (chip_name, category),
        ).fetchone()
        return row is not None

    def _chips_for_category(self, category: str) -> list[str]:
        rows = self._con.execute(
            """
            SELECT c.name
            FROM chips c
            JOIN categories cat ON cat.chip_id = c.id
            WHERE cat.category = ?
            ORDER BY c.name
            """,
            (category,),
        ).fetchall()
        return [r["name"] for r in rows]

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    @classmethod
    def load(cls, db_path: Path | None = None) -> "ChipDB":
        if db_path is None:
            db_path = Path(__file__).parent / "chip_db.db"
        if not db_path.exists():
            raise FileNotFoundError(
                f"chip_db.db not found at {db_path}. Run build_chip_db.py first."
            )
        return cls(db_path)

    def close(self):
        self._con.close()

    # ------------------------------------------------------------------
    # Chip name resolution
    # ------------------------------------------------------------------

    def _candidates(self, form_string: str) -> list[str]:
        s = form_string.strip()
        if not s:
            return []

        cands = [s]

        # Composite strings often look like: "NCT6694B-\nA(EIO-300)"
        # or "Rdc Semi A9620LIF2A(EIO-211)".
        for part in re.split(r"[\n;,/|]+", s):
            p = part.strip()
            if p:
                cands.append(p)

        for m in re.finditer(r"\(([^)]+)\)", s):
            inner = m.group(1).strip()
            if not re.match(r"P/N\s*:", inner, re.IGNORECASE):
                cands.append(inner)

        # Model-like tokens are strong hints. Treat "_" as a separator first:
        # \b sees underscore as a word char, so "Nuvoton_NCT6694B" would
        # otherwise yield no token at all.
        s_sep = s.replace("_", " ")
        token_pat = r"\b(?:EIO-\d+|NCT\d+[A-Z0-9\-]*|ITE\d+[A-Z0-9\-]*|PCA\d+[A-Z0-9\-]*|TCA\d+[A-Z0-9\-]*)\b"
        cands += re.findall(token_pat, s_sep, flags=re.IGNORECASE)

        cands += [tok for tok in s_sep.split() if len(tok) >= 4]

        # Deduplicate while preserving order.
        out: list[str] = []
        seen = set()
        for c in cands:
            k = _normalize(c)
            if not k or k in seen:
                continue
            seen.add(k)
            out.append(c)
        return out

    def find_chip_key(self, form_string: str) -> str | None:
        """Return chips.name that best matches form_string, or None."""
        for cand in self._candidates(form_string):
            norm = _normalize(cand)

            # 1. Curated alias: exact or prefix match (handles package suffixes)
            for alias_norm, chip_name in self._norm_aliases.items():
                if norm == alias_norm or norm.startswith(alias_norm):
                    if _normalize(chip_name) in self._norm_index:
                        return chip_name

            # 2. Exact normalized DB key
            if norm in self._norm_index:
                return self._norm_index[norm]

            # 3. Substring match (candidate ⊂ DB key or DB key ⊂ candidate).
            # Digit required: a vendor-only word ("Nuvoton") must not
            # substring-match an arbitrary chip of that vendor.
            if len(norm) >= 5 and any(c.isdigit() for c in norm):
                for db_norm, db_name in self._norm_index.items():
                    if norm in db_norm or db_norm in norm:
                        return db_name

        return None

    def _best_fuzzy_match(self, form_string: str, names: list[str], threshold: float = 0.74) -> str | None:
        """Fuzzy resolver for near-equal names.

        Example: "Nuvoton NCT6694B" ~ "NCT6694B" or packaging suffix variants.
        """
        if not form_string or not names:
            return None

        cand_norms = [_normalize(c) for c in self._candidates(form_string)]
        best_name = None
        best_score = 0.0

        for name in names:
            if _fuzzy_family_conflict(form_string, name):
                continue
            db_norm = _normalize(name)
            local_best = 0.0
            for cn in cand_norms:
                if not cn:
                    continue
                # The 0.90 containment shortcut needs a digit in the candidate:
                # a bare vendor word ("NUVOTON") is contained in every chip of
                # that vendor and would otherwise auto-win the threshold.
                if (cn in db_norm or db_norm in cn) and any(ch.isdigit() for ch in cn):
                    local_best = max(local_best, 0.90)
                else:
                    local_best = max(local_best, SequenceMatcher(a=cn, b=db_norm).ratio())
            if local_best > best_score:
                best_score = local_best
                best_name = name

        if best_score >= threshold:
            return best_name
        return None

    def _llm_pick_chip(self, form_string: str, category: str, candidates: list[str]) -> str | None:
        """Optional AI-assisted normalization for ambiguous chip text.

        Enabled only when LLM_API_KEY is present. Returns exact chip name from
        candidates, or None.
        """
        api_key = os.environ.get("LLM_API_KEY", "").strip()
        if not api_key or not candidates:
            return None

        try:
            from openai import OpenAI
        except Exception:
            return None

        base_url = os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1")
        model = os.environ.get("LLM_MODEL", "gpt-5.3-codex")

        client = OpenAI(api_key=api_key, base_url=base_url)
        prompt = {
            "task": "Map noisy/composite chip text to one exact DB chip name.",
            "raw_chip_text": form_string,
            "category": category,
            "candidate_chip_names": candidates,
            "rules": [
                "Return one value from candidate_chip_names only, or NONE.",
                "Prefer same-family/module aliases (e.g., EIO-*).",
                "Handle composite strings and parenthesized model hints.",
                "Do not invent names."
            ],
            "output_schema": {"chip_name": "<exact candidate or NONE>"}
        }

        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": "You normalize hardware chip names for SQL lookup."},
                    {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
                ],
                response_format={"type": "json_object"},
                temperature=0,
            )
            content = resp.choices[0].message.content
            data = json.loads(content)
            picked = (data.get("chip_name") or "").strip()
            if picked in candidates:
                return picked
        except Exception:
            return None

        return None

    def find_chip_key_for_category(self, form_string: str, category: str) -> str | None:
        """Category-aware chip resolver.

        Resolution order:
        1) deterministic alias/exact/substring over candidate tokens
        2) fuzzy match among chips that support the category
        3) optional AI-assisted pick among top fuzzy candidates
        """
        category_names = self._chips_for_category(category)
        if not category_names:
            return None

        # 1) deterministic path, but require category availability
        for cand in self._candidates(form_string):
            norm = _normalize(cand)

            for alias_norm, chip_name in self._norm_aliases.items():
                if norm == alias_norm or norm.startswith(alias_norm):
                    if self._has_category(chip_name, category):
                        return chip_name

            if norm in self._norm_index:
                chip_name = self._norm_index[norm]
                if self._has_category(chip_name, category):
                    return chip_name

            # Digit required — same vendor-word guard as find_chip_key.
            if len(norm) >= 5 and any(c.isdigit() for c in norm):
                for db_norm, db_name in self._norm_index.items():
                    if norm in db_norm or db_norm in norm:
                        if self._has_category(db_name, category):
                            return db_name

        # 1.5) EC companion: the named chip does not own this category, but on
        # Advantech boards it always ships alongside a known EC that does
        # (e.g. bare "NCT6694B" asked for WDT/Storage/HWM -> EIO_IS200).
        for cand in self._candidates(form_string):
            norm = _normalize(cand)
            for comp_norm, ec_name in self._norm_companions.items():
                if norm == comp_norm or norm.startswith(comp_norm):
                    if self._has_category(ec_name, category):
                        return ec_name

        # 2) fuzzy over category-supported chip names
        fuzzy_pick = self._best_fuzzy_match(form_string, category_names)
        if fuzzy_pick:
            return fuzzy_pick

        # 3) AI assist over top fuzzy shortlist (same family guard as step 2)
        scored = []
        cand_norms = [_normalize(c) for c in self._candidates(form_string)]
        for nm in category_names:
            if _fuzzy_family_conflict(form_string, nm):
                continue
            db_norm = _normalize(nm)
            score = 0.0
            for cn in cand_norms:
                if cn and (cn in db_norm or db_norm in cn):
                    score = max(score, 0.90)
                else:
                    score = max(score, SequenceMatcher(a=cn, b=db_norm).ratio() if cn else 0.0)
            scored.append((score, nm))
        scored.sort(reverse=True)
        shortlist = [nm for _, nm in scored[:20]]
        ai_pick = self._llm_pick_chip(form_string, category, shortlist)
        if ai_pick:
            return ai_pick

        return None

    # ------------------------------------------------------------------
    # Public lookup — queries SQLite
    # ------------------------------------------------------------------

    def lookup(
        self, form_string: str, category: str
    ) -> tuple[str | None, list[str], list[str]]:
        """
        Resolve form chip string + category → (hwid, support_list, ports_list).
        Returns (None, [], []) when chip or category not found.

        support_list : raw Support entries  e.g. ["12VS0,0x0000F030", ...]
        ports_list   : PortList entries     e.g. ["0x40", "0x42", ...]
        """
        chip_name = self.find_chip_key_for_category(form_string, category)
        if chip_name is None:
            # Backward-compatible fallback (non-category-aware)
            chip_name = self.find_chip_key(form_string)
        if chip_name is None:
            return None, [], []

        row = self._con.execute(
            """
            SELECT cat.id, cat.hwid
            FROM chips c
            JOIN categories cat ON cat.chip_id = c.id
            WHERE c.name = ? AND cat.category = ?
            """,
            (chip_name, category),
        ).fetchone()

        if row is None:
            return None, [], []

        cat_id = row["id"]
        hwid   = row["hwid"]

        support = [
            r["entry"]
            for r in self._con.execute(
                "SELECT entry FROM supports WHERE category_id=? ORDER BY id",
                (cat_id,),
            ).fetchall()
        ]
        ports = [
            r["address"]
            for r in self._con.execute(
                "SELECT address FROM ports WHERE category_id=? ORDER BY id",
                (cat_id,),
            ).fetchall()
        ]
        return hwid, support, ports

    # ------------------------------------------------------------------
    # Helpers for callers
    # ------------------------------------------------------------------

    def support_as_dict(self, support: list[str]) -> dict[str, str]:
        """Convert ["name,code", ...] → {name: code}.  For HWM/WDT/SMBus."""
        result = {}
        for entry in support:
            if "," in entry:
                name, _, code = entry.partition(",")
                result[name.strip()] = code.strip()
            else:
                result[entry.strip()] = ""
        return result

    def support_as_pins(self, support: list[str]) -> list[tuple[int, int]]:
        """Convert ["0,0","0,1",...] → [(group,bit),...].  For GPIO."""
        pins = []
        for entry in support:
            parts = entry.split(",")
            if len(parts) == 2:
                try:
                    pins.append((int(parts[0].strip()), int(parts[1].strip())))
                except ValueError:
                    pass
        return pins

    def categories_for(self, form_string: str) -> list[str]:
        """Return available categories for a chip (for debugging)."""
        chip_name = self.find_chip_key(form_string)
        if chip_name is None:
            return []
        rows = self._con.execute(
            "SELECT cat.category FROM chips c JOIN categories cat ON cat.chip_id=c.id WHERE c.name=? ORDER BY cat.category",
            (chip_name,),
        ).fetchall()
        return [r["category"] for r in rows]
