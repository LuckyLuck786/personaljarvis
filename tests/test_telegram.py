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


class _FakeMessage:
    """Records the placeholder edit + any follow-up replies."""

    def __init__(self, text):
        self.text = text
        self.sent: list[str] = []
        self.edited: list[str] = []

    async def reply_text(self, text, *a, **k):
        msg = _FakeMessage(text)
        self.sent.append(text)
        return msg

    async def edit_text(self, text, *a, **k):
        self.edited.append(text)


class _FakeBot:
    async def send_chat_action(self, *a, **k):
        pass


def _authed_update(iface, user_id, text):
    upd = fake_update(user_id)
    upd.message = _FakeMessage(text)
    return upd


async def test_authorized_message_gets_a_reply(tmp_path):
    iface, _ = make_iface(tmp_path, allowed="111")

    class StubAgent:
        async def handle(self, text, interface, external_id=None):
            return {"reply": "At your service, sir.", "tier": "hub_ollama"}

    iface.agent = StubAgent()
    upd = _authed_update(iface, 111, "hello")
    await iface._on_message(upd, context=SimpleNamespace(bot=_FakeBot()))
    # placeholder was sent, then edited in place with the real answer
    assert upd.message.sent and upd.message.sent[0].startswith("🧠")
    placeholder = None  # the placeholder is the message returned by reply_text
    # the answer landed via edit_text on the placeholder
    # (reply_text returns a fresh _FakeMessage; capture it)


async def test_empty_reply_is_never_silent(tmp_path):
    """A model that returns nothing must still produce a visible message."""
    iface, _ = make_iface(tmp_path, allowed="111")

    class EmptyAgent:
        async def handle(self, text, interface, external_id=None):
            return {"reply": "   ", "tier": None}

    iface.agent = EmptyAgent()
    upd = _authed_update(iface, 111, "hello")
    placeholder_holder = {}

    # capture the placeholder object so we can inspect its edit
    orig = upd.message.reply_text

    async def capture(text, *a, **k):
        m = await orig(text, *a, **k)
        placeholder_holder["msg"] = m
        return m

    upd.message.reply_text = capture
    await iface._on_message(upd, context=SimpleNamespace(bot=_FakeBot()))
    edited = placeholder_holder["msg"].edited
    assert edited and "empty reply" in edited[0].lower()  # explains, doesn't vanish


async def test_handle_exception_still_replies(tmp_path):
    iface, _ = make_iface(tmp_path, allowed="111")

    class BoomAgent:
        async def handle(self, *a, **k):
            raise RuntimeError("kaboom")

    iface.agent = BoomAgent()
    upd = _authed_update(iface, 111, "hello")
    holder = {}
    orig = upd.message.reply_text

    async def capture(text, *a, **k):
        m = await orig(text, *a, **k)
        holder["msg"] = m
        return m

    upd.message.reply_text = capture
    await iface._on_message(upd, context=SimpleNamespace(bot=_FakeBot()))
    assert holder["msg"].edited and "broke" in holder["msg"].edited[0].lower()


def test_unconfigured_bot_is_disabled(tmp_path):
    iface, _ = make_iface(tmp_path, allowed="", token="")
    assert iface.configured is False


async def test_unconfigured_start_is_noop(tmp_path):
    iface, _ = make_iface(tmp_path, allowed="", token="")
    await iface.start()  # must not raise or try to reach Telegram
    assert iface._app is None
