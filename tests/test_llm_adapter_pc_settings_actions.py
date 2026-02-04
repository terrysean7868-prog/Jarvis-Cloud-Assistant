from src.core.llm_adapter import LLMAdapter


def _types(actions):
    return [a.get("type") for a in (actions or []) if isinstance(a, dict)]


def _find_device_action(actions, name: str):
    for a in (actions or []):
        if not isinstance(a, dict):
            continue
        if a.get("type") != "device_action":
            continue
        if (a.get("name") or a.get("action")) == name:
            return a
    return None


def test_set_brightness_emits_device_action():
    parsed = {"text": "", "actions": []}
    out = LLMAdapter._postprocess_pc_settings_actions("set brightness to 40%", parsed)
    da = _find_device_action(out.get("actions"), "set_brightness")
    assert da is not None
    assert (da.get("args") or {}).get("value") == 40


def test_increase_brightness_emits_delta_action():
    parsed = {"text": "", "actions": []}
    out = LLMAdapter._postprocess_pc_settings_actions("increase brightness", parsed)
    da = _find_device_action(out.get("actions"), "set_brightness")
    assert da is not None
    assert (da.get("args") or {}).get("delta") in (10, 5)


def test_energy_saver_emits_power_plan_action():
    parsed = {"text": "", "actions": []}
    out = LLMAdapter._postprocess_pc_settings_actions("turn on energy saver", parsed)
    da = _find_device_action(out.get("actions"), "set_power_plan")
    assert da is not None
    assert "power" in str((da.get("args") or {}).get("plan") or "").lower()


def test_set_volume_emits_device_action():
    parsed = {"text": "", "actions": []}
    out = LLMAdapter._postprocess_pc_settings_actions("set volume to 30%", parsed)
    da = _find_device_action(out.get("actions"), "set_volume")
    assert da is not None
    assert (da.get("args") or {}).get("value") == 30


def test_mute_emits_device_action():
    parsed = {"text": "", "actions": []}
    out = LLMAdapter._postprocess_pc_settings_actions("mute volume", parsed)
    da = _find_device_action(out.get("actions"), "set_mute")
    assert da is not None
    assert (da.get("args") or {}).get("muted") is True


def test_unmute_emits_device_action():
    parsed = {"text": "", "actions": []}
    out = LLMAdapter._postprocess_pc_settings_actions("unmute", parsed)
    da = _find_device_action(out.get("actions"), "set_mute")
    assert da is not None
    assert (da.get("args") or {}).get("muted") is False


def test_wifi_toggle_emits_device_action():
    parsed = {"text": "", "actions": []}
    out = LLMAdapter._postprocess_pc_settings_actions("turn off wifi", parsed)
    da = _find_device_action(out.get("actions"), "set_wifi")
    assert da is not None
    assert (da.get("args") or {}).get("enabled") is False


def test_bluetooth_toggle_emits_device_action():
    parsed = {"text": "", "actions": []}
    out = LLMAdapter._postprocess_pc_settings_actions("turn on bluetooth", parsed)
    da = _find_device_action(out.get("actions"), "set_bluetooth")
    assert da is not None
    assert (da.get("args") or {}).get("enabled") is True


def test_night_light_opens_settings_via_device_action():
    parsed = {"text": "", "actions": []}
    out = LLMAdapter._postprocess_pc_settings_actions("turn on night light", parsed)
    open_settings = _find_device_action(out.get("actions"), "open_settings")
    assert open_settings is not None
    assert "ms-settings:display" in str((open_settings.get("args") or {}).get("uri") or "")


def test_dnd_opens_notifications_settings_via_device_action():
    parsed = {"text": "", "actions": []}
    out = LLMAdapter._postprocess_pc_settings_actions("turn on do not disturb", parsed)
    open_settings = _find_device_action(out.get("actions"), "open_settings")
    assert open_settings is not None
    assert "ms-settings:notifications" in str((open_settings.get("args") or {}).get("uri") or "")
