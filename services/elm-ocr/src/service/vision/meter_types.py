"""
Known meter models and their display format, so the decimal point never depends on the model spotting a tiny dot.

A meter type is matched on the model/type code the vision model copies from the meter body, never on the
brand alone: L&T/Schneider make both the ER300 (no decimal point) and the EM101+ (one decimal digit).

Only types confirmed on our own photos are listed. Add more in a JSON file (METER_TYPES_FILE), e.g.
    [{"id": "genus_xyz", "description": "...", "decimals": 1, "digits": 8, "patterns": ["\\\\bXYZ\\\\s*100\\\\b"]}]
A per-consumer setting (PUT /meters/{consumerId}, stored by service/api/metadata_service.py) always overrides these.
"""
import json
import re
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class MeterType:
    id: str
    description: str
    decimals: int               # digits after the decimal point on the cumulative screen
    digits: int | None          # total digits on the main row, leading zeros included; None if it varies
    patterns: tuple[str, ...]   # regexes, matched case-insensitively against make / model code / faceplate text
    evidence: str = ""

    def to_dict(self):
        d = asdict(self)
        d["patterns"] = list(self.patterns)
        return d


BUILTIN = (
    MeterType("hpl_ppsm01", "HPL PPSM 01 three-phase smart meter (PSPCL, Punjab)", decimals=1, digits=8,
              patterns=(r"\bPPSM\s*-?\s*0?1\b",),
              evidence="Trial photos: 0019712.7, 0055121.5, 0045924.8, 0045883.8"),
    MeterType("lt_er300", "L&T / Schneider ER300 static meter (APDCL, Assam), 8 digits, no decimal point",
              decimals=0, digits=8, patterns=(r"\bER\s*-?\s*300", r"\bWR\s*-?\s*300"),
              evidence="Trial photos: 00018103, 00007696"),
    MeterType("lt_em101", "L&T / Schneider EM101+ single-phase meter (APDCL, Assam), one decimal digit",
              decimals=1, digits=None, patterns=(r"\bEM\s*-?\s*101",),
              evidence="Trial photos: 2344.7, 2330.5"),
    MeterType("genus_saksham345", "Genus SAKSHAM-345 (APDCL, Assam), 8 digits, one decimal digit",
              decimals=1, digits=8, patterns=(r"\bSAKSHAM\s*-?\s*34[5S]\b",),   # the model once read 345 as 34S
              evidence="Hand-reviewed trial photos: 0032740.9, 0033486.0, 0000359.6; model code read on 6 photos (2026-09-30)"),
    # Added 2026-09-30 from the 100-photo run: model code read by GPT-6 Sol, format from the hand review.
    MeterType("hpl_ppem26", "HPL PPEM 26 (PSPCL, Punjab), 7 digits, one decimal digit", decimals=1, digits=7,
              patterns=(r"\bPPEM\s*-?\s*26\b",), evidence="Hand-reviewed trial photos: 3 readings in the 999999.9 format"),
    MeterType("visiontek_37tm", "Visiontek 37TM (Punjab), 8 digits, one decimal digit", decimals=1, digits=8,
              patterns=(r"\b37\s*TM\b",), evidence="Hand-reviewed trial photos: 2 readings in the 9999999.9 format"),
    MeterType("avon_asem306", "Avon ASEM-306 (Punjab), 8 digits, one decimal digit", decimals=1, digits=8,
              patterns=(r"\bASEM\s*-?\s*306\b",), evidence="Hand-reviewed trial photos: 2 readings in the 9999999.9 format"),
    MeterType("aew_aplsm346", "AEW APL-sm346 (Punjab), one decimal digit", decimals=1, digits=None,
              patterns=(r"\bAPL\s*-?\s*SM\s*-?\s*346\b",),
              evidence="Hand-reviewed trial photos: 3 readings in the 9999999.9 format, 1 typed as 99999999.9"),
)


class MeterTypeRegistry:
    def __init__(self, types=BUILTIN):
        self.types = {t.id: t for t in types}

    @classmethod
    def load(cls, path=None):
        reg = cls()
        if path:
            with open(path, encoding="utf-8") as f:
                for item in json.load(f):
                    t = MeterType(id=item["id"], description=item.get("description", ""), decimals=int(item["decimals"]),
                                  digits=item.get("digits"), patterns=tuple(item["patterns"]),
                                  evidence=item.get("evidence", ""))
                    for p in t.patterns:
                        re.compile(p)
                    reg.types[t.id] = t
        return reg

    def get(self, type_id):
        return self.types.get(type_id)

    def identify(self, answer):
        """The single meter type whose pattern matches the printed identifiers, or None (no match, or ambiguous)."""
        texts = [answer.get("meter_model_code") or "", answer.get("meter_make") or "", *(answer.get("faceplate_text") or [])]
        haystack = " | ".join(t for t in texts if t)
        if not haystack:
            return None
        hits = [t for t in self.types.values() if any(re.search(p, haystack, re.IGNORECASE) for p in t.patterns)]
        return hits[0] if len(hits) == 1 else None

    def all(self):
        return list(self.types.values())
