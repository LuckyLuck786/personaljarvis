import pytest

from jarvis.core.crypto import Vault, generate_master_key


def test_roundtrip():
    vault = Vault(generate_master_key())
    token = vault.encrypt("memory", "I decided to use the serif font on the resume")
    assert token != "I decided to use the serif font on the resume"
    assert vault.decrypt_text("memory", token) == (
        "I decided to use the serif font on the resume"
    )


def test_purposes_are_isolated():
    vault = Vault(generate_master_key())
    token = vault.encrypt("memory", "secret")
    from cryptography.fernet import InvalidToken

    with pytest.raises(InvalidToken):
        vault.decrypt("capture", token)  # wrong purpose → wrong derived key


def test_wrong_master_key_fails():
    token = Vault(generate_master_key()).encrypt("memory", "secret")
    from cryptography.fernet import InvalidToken

    with pytest.raises(InvalidToken):
        Vault(generate_master_key()).decrypt("memory", token)


def test_missing_or_malformed_key_rejected():
    with pytest.raises(ValueError):
        Vault("")
    with pytest.raises(ValueError):
        Vault("not-base64!!")
    with pytest.raises(ValueError):
        Vault("c2hvcnQ=")  # decodes, but not 32 bytes
