"""Import legacy VieNeu profiles into the global Nexus voice catalog."""

from __future__ import annotations

import sys
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPOSITORY_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from nexus_engine.services import vieneu_voice_profiles, voice_config  # noqa: E402


VOICE_ID = "bac-ho-ngam-tho"


def migrate() -> dict:
    revisions = vieneu_voice_profiles.import_legacy_bac_ho_profiles()
    imported = {int(item.get("revision") or 0): item for item in revisions}
    if set(imported) != {1, 2, 3} or not all(
        item.get("available") for item in imported.values()
    ):
        raise RuntimeError("Không thể import đầy đủ profile Bác Hồ r1-r3.")

    with voice_config.CONFIG_LOCK:
        config = voice_config.load_voice_config()
        existing = next(
            (voice for voice in config.get("voices") or [] if voice.get("id") == VOICE_ID),
            None,
        )
        if existing is None:
            config["voices"].append(
                {
                "id": VOICE_ID,
                "name": "Bác Hồ ngâm thơ",
                "provider_id": voice_config.VIENEU_PROVIDER_ID,
                "provider_voice_id": VOICE_ID,
                "status": "draft",
                "revision": 3,
                "active_revision": None,
                "draft_revision": 3,
                "config": {
                    "backend": "onnx",
                    "precision": "fp32",
                    "sample_rate": 48000,
                    "use_ref_codes": True,
                    "denoise_reference": False,
                    "apply_watermark": True,
                },
                "casting_profile": {
                    "gender": "male",
                    "age_group": "senior",
                    "languages": ["vi"],
                    "styles": ["poetry"],
                    "roles": ["narrator"],
                    "usage_modes": ["single_narrator"],
                    "voice_pools": [],
                },
                }
            )
        else:
            if existing.get("provider_id") != voice_config.VIENEU_PROVIDER_ID:
                raise RuntimeError("Voice ID Bác Hồ đã thuộc provider khác.")
            existing["draft_revision"] = max(3, int(existing.get("draft_revision") or 0))
            existing["revision"] = int(
                existing.get("active_revision") or existing["draft_revision"]
            )
            if existing.get("status") not in {"active", "archived"}:
                existing["status"] = "draft"

        saved = voice_config.save_voice_config(config)
    return next(voice for voice in saved["voices"] if voice["id"] == VOICE_ID)


if __name__ == "__main__":
    migrated = migrate()
    print(
        f"Imported voice {migrated['id']} as {migrated['status']} "
        f"(active={migrated.get('active_revision')}, draft={migrated.get('draft_revision')})."
    )
