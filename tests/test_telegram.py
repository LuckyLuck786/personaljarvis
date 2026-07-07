"""Telegram gating: only allow-listed user IDs get anything at all."""

from types import SimpleNamespace

from jarvis.core.audit import AuditLog
from jarvis.core.config import Config, Secrets
from jarvis.core.db import migrate
from jarvis.core.killswitch import KillSwitch
from jarvis.interfaces.telegram_bot import TelegramInterface
from tests.conftest import TEST_API_KEY, TEST_MASTER_KEY


def make_iface(tmp_path, allowed="111", token="fake-token"):
    cfg = Config(
        data_dir=tmp_path,
        secrets=Secrets(
            _env_file=None,
            jarvis_api_key=TEST_API_KEY,
            jarvis_master_key=TEST_MASTER_KEY,
            telegram_bot_token=token,
            telegram_allowed_user_ids=allowed,
        ),
    )
    migrate(cfg.db_path)
    audit = AuditLog(cfg.db_path)
    return TelegramInterface(cfg, agent=None, killswitch=KillSwitch(cfg.db_path),
                             audit=audit, node_status={}), audit


def fake_update(user_id):
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id, username="someone"),
        effective_chat=SimpleNamespace(id=user_id),
        message=None,
    )


def test_allowed_id_authorized(tmp_path):
    iface, _ = make_iface(tmp_path, allowed="111,222")
    assert iface._authorized(fake_update(111)) is True
    assert iface._authorized(fake_update(222)) is True


def test_unknown_id_rejected_and_audited(tmp_path):
    iface, audit = make_iface(tmp_path, allowed="111")
    assert iface._authorized(fake_update(999)) is False
    entries = audit.tail()
    assert entries[-1]["action"] == "auth.rejected"
    assert entries[-1]["outcome"] == "denied"
    assert entries[-1]["params"]["user_id"] == 999


async def test_unauthorized_message_gets_no_reply(tmp_path):
    iface, _ = make_iface(tmp_path, allowed="111")
    update = fake_update(999)
    replied = []
    update.message = SimpleNamespace(
        text="hi",
        reply_text=lambda *a, **k: replied.append(a),
    )
    await iface._on_message(update, context=SimpleNamespace(bot=None))
    assert replied == []  # silence, not even an error message


def test_unconfigured_bot_is_disabled(tmp_path):
    iface, _ = make_iface(tmp_path, allowed="", token="")
    assert iface.configured is False


async def test_unconfigured_start_is_noop(tmp_path):
    iface, _ = make_iface(tmp_path, allowed="", token="")
    await iface.start()  # must not raise or try to reach Telegram
    assert iface._app is None
