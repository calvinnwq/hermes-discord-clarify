from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest


PLUGIN_DIR = Path(__file__).resolve().parents[1]


def load_plugin():
    module_name = "hermes_discord_clarify_test_plugin"
    sys.modules.pop(module_name, None)
    spec = importlib.util.spec_from_file_location(
        module_name, PLUGIN_DIR / "__init__.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


class FakeButton:
    def __init__(self, *, label: str, custom_id: str):
        self.label = label
        self.custom_id = custom_id
        self.disabled = False
        self.callback = None


class FakeClarifyView:
    def __init__(self, choices, clarify_id, allowed_user_ids, allowed_role_ids=None):
        self.choices = list(choices)[:24]
        self.clarify_id = clarify_id
        self.allowed_user_ids = allowed_user_ids
        self.allowed_role_ids = allowed_role_ids or set()
        self.resolved = False
        self.choice_events = []
        for index, choice in enumerate(self.choices):
            button = FakeButton(
                label=f"{index + 1}. {choice}",
                custom_id=f"clarify:{clarify_id}:{index}",
            )

            async def choice_callback(interaction, *, index=index, choice=choice):
                self.choice_events.append((index, choice))
                interaction.choice = choice

            button.callback = choice_callback
            self.children = getattr(self, "children", []) + [button]

        other = FakeButton(
            label="✏️ Other (type answer)",
            custom_id=f"clarify:{clarify_id}:other",
        )

        async def other_callback(interaction):
            self.choice_events.append(("other", None))
            interaction.other = True

        other.callback = other_callback
        self.children = getattr(self, "children", []) + [other]


class FakeColor:
    @staticmethod
    def orange():
        return "orange"


class FakeEmbed:
    def __init__(self, *, title, description, color):
        self.title = title
        self.description = description
        self.color = color
        self.fields = []

    def add_field(self, *, name, value, inline):
        self.fields.append((name, value, inline))


class FakeDiscord:
    Embed = FakeEmbed
    Color = FakeColor


@dataclass
class FakeSendResult:
    success: bool
    message_id: str | None = None
    error: str | None = None


class FakeBaseAdapter:
    MAX_MESSAGE_LENGTH = 2000

    def __init__(self, config=None):
        self.config = config
        self._client = None
        self._allowed_user_ids = set()
        self._allowed_role_ids = set()
        self.parent_calls = []

    def _self_contained_prompt_content(self, header, body, *, code_block=False, tail=""):
        prefix = f"{header}\n\n"
        suffix = tail
        budget = self.MAX_MESSAGE_LENGTH - len(prefix) - len(suffix)
        if len(body) > budget:
            body = body[: max(0, budget - 19)] + "\n... [truncated]"
        return f"{prefix}{body}{suffix}"

    async def send_clarify(
        self, chat_id, question, choices, clarify_id, session_key, metadata=None
    ):
        self.parent_calls.append(
            (chat_id, question, choices, clarify_id, session_key, metadata)
        )
        return "parent-result"

    def unrelated_method(self):
        return "unchanged"


class FakeRegistry:
    def __init__(self, entry):
        self.entry = entry

    def get(self, name):
        assert name == "discord"
        return self.entry


class FakeContext:
    def __init__(self):
        self.calls = []

    def register_platform(self, **kwargs):
        self.calls.append(kwargs)


@pytest.fixture
def fake_host(monkeypatch):
    host = types.ModuleType("plugins.platforms.discord.adapter")
    host.DISCORD_AVAILABLE = True
    host.discord = FakeDiscord
    host.DiscordAdapter = FakeBaseAdapter
    host.ClarifyChoiceView = FakeClarifyView
    host.SendResult = FakeSendResult

    package = types.ModuleType("plugins")
    package.__path__ = []
    platforms = types.ModuleType("plugins.platforms")
    platforms.__path__ = []
    discord_package = types.ModuleType("plugins.platforms.discord")
    discord_package.__path__ = []
    monkeypatch.setitem(sys.modules, "plugins", package)
    monkeypatch.setitem(sys.modules, "plugins.platforms", platforms)
    monkeypatch.setitem(sys.modules, "plugins.platforms.discord", discord_package)
    monkeypatch.setitem(sys.modules, "plugins.platforms.discord.adapter", host)

    entry = SimpleNamespace(
        name="discord",
        label="Discord",
        check_fn=lambda: True,
        validate_config=lambda cfg: True,
        required_env=["DISCORD_BOT_TOKEN"],
        install_hint="pip install hermes-agent[messaging]",
        is_connected=lambda cfg: True,
        setup_fn=lambda: None,
        allowed_users_env="DISCORD_ALLOWED_USERS",
        allow_all_env="DISCORD_ALLOW_ALL_USERS",
        cron_deliver_env_var="DISCORD_HOME_CHANNEL",
        standalone_sender_fn=lambda *args, **kwargs: None,
        max_message_length=2000,
        emoji="🎮",
        allow_update_command=True,
        platform_hint="discord",
        apply_yaml_config_fn=lambda *args: None,
    )
    registry = types.ModuleType("gateway.platform_registry")
    registry.platform_registry = FakeRegistry(entry)
    monkeypatch.setitem(sys.modules, "gateway", types.ModuleType("gateway"))
    monkeypatch.setitem(sys.modules, "gateway.platform_registry", registry)
    return host, entry


def test_manifest_declares_platform_plugin():
    import yaml

    manifest = yaml.safe_load((PLUGIN_DIR / "plugin.yaml").read_text())
    assert manifest["kind"] == "platform"
    assert manifest["name"] == "hermes-discord-clarify"
    assert manifest["version"]


def test_register_preserves_bundled_metadata_and_overrides_factory(fake_host):
    plugin = load_plugin()
    ctx = FakeContext()

    plugin.register(ctx)

    assert len(ctx.calls) == 1
    call = ctx.calls[0]
    assert call["name"] == "discord"
    assert call["label"] == "Discord"
    assert call["required_env"] == ["DISCORD_BOT_TOKEN"]
    assert call["max_message_length"] == 2000
    assert call["cron_deliver_env_var"] == "DISCORD_HOME_CHANNEL"
    assert call["setup_fn"] is not None
    assert call["check_fn"] is not None
    assert call["adapter_factory"](None).__class__.__name__ == "ReadableDiscordAdapter"


def test_load_host_prefers_installed_dynamic_namespace(fake_host, monkeypatch):
    source_host, _ = fake_host
    dynamic_host = types.ModuleType("hermes_plugins.discord_platform.adapter")
    dynamic_host.DISCORD_AVAILABLE = True
    dynamic_host.discord = FakeDiscord
    dynamic_host.DiscordAdapter = FakeBaseAdapter
    dynamic_host.ClarifyChoiceView = FakeClarifyView
    dynamic_host.SendResult = FakeSendResult

    hermes_plugins = types.ModuleType("hermes_plugins")
    hermes_plugins.__path__ = []
    discord_platform = types.ModuleType("hermes_plugins.discord_platform")
    discord_platform.__path__ = []
    monkeypatch.setitem(sys.modules, "hermes_plugins", hermes_plugins)
    monkeypatch.setitem(
        sys.modules, "hermes_plugins.discord_platform", discord_platform
    )
    monkeypatch.setitem(
        sys.modules, "hermes_plugins.discord_platform.adapter", dynamic_host
    )

    plugin = load_plugin()

    assert plugin._load_host() is dynamic_host
    assert plugin._load_host() is not source_host


def test_load_host_falls_back_to_source_tree(fake_host):
    source_host, _ = fake_host
    plugin = load_plugin()

    assert plugin._load_host() is source_host


def test_register_loads_host_before_legacy_registry_fallback(fake_host, monkeypatch):
    plugin = load_plugin()
    ctx = FakeContext()
    events = []
    registry = sys.modules["gateway.platform_registry"].platform_registry
    original_get = registry.get
    monkeypatch.setattr(
        registry,
        "get",
        lambda name: events.append("registry.get") or original_get(name),
    )
    original_import = plugin.importlib.import_module

    def tracked_import(name, *args, **kwargs):
        if name in {
            "hermes_plugins.discord_platform.adapter",
            "plugins.platforms.discord.adapter",
        }:
            events.append(f"import:{name}")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(plugin.importlib, "import_module", tracked_import)

    plugin.register(ctx)

    assert events[0] == "import:hermes_plugins.discord_platform.adapter"
    assert events[1] == "import:plugins.platforms.discord.adapter"
    assert events[2] == "registry.get"


def test_register_uses_host_metadata_without_resolving_registry(fake_host, monkeypatch):
    host, entry = fake_host

    def bundled_register(capture):
        capture.register_platform(
            name=entry.name,
            label=entry.label,
            adapter_factory=FakeBaseAdapter,
            check_fn=entry.check_fn,
            validate_config=entry.validate_config,
            required_env=entry.required_env,
            install_hint=entry.install_hint,
            is_connected=entry.is_connected,
            setup_fn=entry.setup_fn,
            allowed_users_env=entry.allowed_users_env,
            allow_all_env=entry.allow_all_env,
            cron_deliver_env_var=entry.cron_deliver_env_var,
            standalone_sender_fn=entry.standalone_sender_fn,
            max_message_length=entry.max_message_length,
            emoji=entry.emoji,
            allow_update_command=entry.allow_update_command,
            platform_hint=entry.platform_hint,
            apply_yaml_config_fn=entry.apply_yaml_config_fn,
        )

    host.register = bundled_register
    plugin = load_plugin()
    ctx = FakeContext()
    registry = sys.modules["gateway.platform_registry"].platform_registry
    monkeypatch.setattr(
        registry,
        "get",
        lambda name: pytest.fail("registration must not resolve the deferred platform"),
    )

    plugin.register(ctx)

    assert len(ctx.calls) == 1
    call = ctx.calls[0]
    assert call["label"] == "Discord"
    assert call["required_env"] == ["DISCORD_BOT_TOKEN"]
    assert call["adapter_factory"](None).__class__.__name__ == "ReadableDiscordAdapter"


def test_register_fails_safe_when_discord_symbols_are_unavailable(fake_host):
    host, _ = fake_host
    host.DISCORD_AVAILABLE = False
    plugin = load_plugin()
    ctx = FakeContext()

    plugin.register(ctx)

    assert ctx.calls == []


def test_numeric_view_relabels_without_replacing_callbacks_or_other(fake_host):
    plugin = load_plugin()
    _, NumericView, _ = plugin._build_override_classes(plugin._load_host())

    view = NumericView(
        ["first full answer", "second full answer"],
        "cid",
        {"42"},
    )
    callbacks = [button.callback for button in view.children]
    interaction = SimpleNamespace()

    asyncio.run(view.children[1].callback(interaction))
    assert [button.label for button in view.children] == [
        "1",
        "2",
        "✏️ Other (type answer)",
    ]
    assert callbacks[1] is view.children[1].callback
    assert interaction.choice == "second full answer"
    assert view.children[-1].custom_id == "clarify:cid:other"
    asyncio.run(view.children[-1].callback(interaction))
    assert interaction.other is True


@pytest.mark.asyncio
async def test_multiple_choice_content_has_full_numbered_list_and_numeric_buttons(fake_host):
    plugin = load_plugin()
    host = plugin._load_host()
    _, _, Adapter = plugin._build_override_classes(host)
    adapter = Adapter(None)
    channel = types.SimpleNamespace()
    sent = SimpleNamespace(id=123)

    async def send(**kwargs):
        channel.kwargs = kwargs
        return sent

    channel.send = send
    adapter._client = SimpleNamespace(get_channel=lambda _id: channel)
    adapter._allowed_user_ids = {"42"}

    result = await adapter.send_clarify(
        chat_id="9001",
        question="Pick a color",
        choices=["red", "green", "blue"],
        clarify_id="cid-content",
        session_key="session",
    )

    assert result.success is True
    assert result.message_id == "123"
    assert "1. red" in channel.kwargs["content"]
    assert "2. green" in channel.kwargs["content"]
    assert "3. blue" in channel.kwargs["content"]
    assert [b.label for b in channel.kwargs["view"].children] == [
        "1",
        "2",
        "3",
        "✏️ Other (type answer)",
    ]
    assert "Pick a numbered button" in channel.kwargs["content"]


@pytest.mark.asyncio
async def test_open_ended_delegates_to_host_unchanged(fake_host):
    plugin = load_plugin()
    host = plugin._load_host()
    _, _, Adapter = plugin._build_override_classes(host)
    adapter = Adapter(None)

    result = await adapter.send_clarify(
        "chat",
        "What is your name?",
        None,
        "cid-open",
        "session",
    )

    assert result == "parent-result"
    assert adapter.parent_calls[0][2] is None
    assert adapter.unrelated_method() == "unchanged"


@pytest.mark.asyncio
async def test_overflow_is_bounded_and_keeps_full_choices_in_embed_field(fake_host):
    plugin = load_plugin()
    host = plugin._load_host()
    _, _, Adapter = plugin._build_override_classes(host)
    adapter = Adapter(None)
    channel = types.SimpleNamespace()
    sent = SimpleNamespace(id=456)

    async def send(**kwargs):
        channel.kwargs = kwargs
        return sent

    channel.send = send
    adapter._client = SimpleNamespace(get_channel=lambda _id: channel)
    long_choices = [f"choice-{index}-" + "x" * 140 for index in range(24)]

    result = await adapter.send_clarify(
        "9002",
        "Pick one",
        long_choices,
        "cid-overflow",
        "session",
    )

    assert result.success is True
    assert len(channel.kwargs["content"]) <= 2000
    assert channel.kwargs["embed"].fields
    assert "Choices" in channel.kwargs["embed"].fields[0][0]
