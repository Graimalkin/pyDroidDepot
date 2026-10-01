import pytest
from droiddepot.connection import build_droid_command, build_droid_multi_command, parse_manufacturer_data
from droiddepot.protocol import DroidCommandId, DroidMultipurposeCommand
from droiddepot.hardware import DroidPersonalityIdentifier, DroidAffiliation, DroidAudioBankIdentifier, get_available_audio_in_bank


def test_heartbeat_command():
    assert build_droid_command(DroidCommandId.ConnectionHeartbeat).hex() == "23000e40"


def test_command_with_data():
    assert build_droid_command(DroidCommandId.SetPairingLedState, "00ff").hex() == "2500024200ff"


def test_malformed_data_raises():
    with pytest.raises(ValueError):
        build_droid_command(DroidCommandId.SetMotorSpeed, "zz")


def test_multi_command_matches_droid_toolbox():
    # Droid-Toolbox "set group 7": 27 42 0f 44 44 00 1f 07
    assert build_droid_multi_command(DroidMultipurposeCommand.AudioControllerCommand, "1f07").hex() == "27420f4444001f07"


def test_multi_command_sub_id_is_hex():
    assert build_droid_multi_command(0x0a).hex() == "25420f42440a"


def test_parse_manufacturer_data():
    # affiliation byte 0x8a -> (0x8a - 0x80) // 2 == 5 (Resistance); personality is the last byte
    data = {387: bytes([0x03, 0x04, 0x44, 0x81, 0x8a, 0x01])}
    assert parse_manufacturer_data(data) == (DroidPersonalityIdentifier.RUnit, DroidAffiliation.Resistenace)


def test_personality_ids_match_real_droids():
    # A black R2 reports 0x01; Droid-Toolbox lists 0x02 as BB-series and 0x0e as BD unit.
    assert DroidPersonalityIdentifier.RUnit == 0x01
    assert DroidPersonalityIdentifier.BBUnit == 0x02
    assert DroidPersonalityIdentifier.BDUnit == DroidPersonalityIdentifier.BUnit == 0x0e


def test_parse_manufacturer_data_missing():
    default = (DroidPersonalityIdentifier.RUnit, DroidAffiliation.Scoundrel)
    assert parse_manufacturer_data(None) == default
    assert parse_manufacturer_data({76: b"\x00"}) == default


def test_available_audio_in_bank():
    assert get_available_audio_in_bank(DroidAudioBankIdentifier.GeneralUseAudioBank, DroidPersonalityIdentifier.RUnit) == 4
    assert get_available_audio_in_bank(99, DroidPersonalityIdentifier.RUnit) == 0
