import pytest
from nexus_engine.services import google_flow_account
from nexus_engine.services.google_flow_account import FLOW_MODELS_CATALOG


def test_flow_models_catalog_complete():
    """Verify all 9 models and agent are represented in the catalog."""
    model_ids = {m["id"] for m in FLOW_MODELS_CATALOG}
    expected_ids = {
        "flow_creative_agent",
        "nano_banana_2_1",
        "nano_banana_2_lite",
        "nano_banana_pro",
        "omni_1_1_flash",
        "veo_3_1_lite",
        "veo_3_1_fast",
        "veo_3_1_quality",
        "google_veo_intro",
    }
    assert expected_ids.issubset(model_ids), f"Missing model IDs: {expected_ids - model_ids}"


def test_multi_account_crud_and_profile_assignment(tmp_path, monkeypatch):
    """Verify multi-account upsert auto-assigns next PROFILE_GOOGLE_FLOW_{N} and isolates profiles."""
    test_file = tmp_path / "test_accounts.json"
    monkeypatch.setattr(google_flow_account, "GOOGLE_FLOW_ACCOUNTS_PATH", test_file)

    # 1. Add first account
    acc1 = google_flow_account.upsert_account({
        "id": "acc_1",
        "email": "test1@gmail.com",
        "password": "secret_pass_1",
        "totp_secret": "2FA_1",
    })
    assert acc1["profile_name"] == "PROFILE_GOOGLE_FLOW_1"
    assert acc1["email"] == "test1@gmail.com"

    # 2. Add second account -> must get PROFILE_GOOGLE_FLOW_2
    acc2 = google_flow_account.upsert_account({
        "id": "acc_2",
        "email": "test2@gmail.com",
        "password": "secret_pass_2",
    })
    assert acc2["profile_name"] == "PROFILE_GOOGLE_FLOW_2"

    # 3. List accounts should sanitize secrets
    sanitized = google_flow_account.list_accounts(include_secrets=False)
    assert len(sanitized) == 2
    for item in sanitized:
        assert "password" not in item
        assert "totp_secret" not in item
        assert item["password_configured"] is True

    # 4. Quotas update
    google_flow_account.save_account_quotas("acc_1", {
        "veo_3_1_quality": {"status": "exhausted", "message": "Out of credits"},
        "nano_banana_2_1": {"status": "eligible", "message": "OK"},
    }, plan_tier="Google One AI Premium")

    # 5. Smart model selection
    # acc1 has exhausted veo_3_1_quality, but acc2 has not
    best_veo = google_flow_account.select_eligible_account_for_model("veo_3_1_quality", media_type="video")
    assert best_veo["id"] == "acc_2"

    # acc1 still has eligible nano_banana_2_1
    best_img = google_flow_account.select_eligible_account_for_model("nano_banana_2_1", media_type="image")
    assert best_img["id"] == "acc_1"

    # 6. Delete account
    google_flow_account.delete_account("acc_2")
    assert len(google_flow_account.list_accounts()) == 1
