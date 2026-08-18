"""Readable multiple-choice clarify prompts for Hermes' Discord adapter.

This is a user-level platform override.  It imports the bundled Discord
adapter lazily, subclasses its view and adapter, and registers the same
``discord`` platform name last so the platform registry selects this adapter.
No Hermes core files are modified.
"""

from __future__ import annotations

import importlib
import inspect
import logging
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)

_HOST_ADAPTER_MODULE = "hermes_plugins.discord_platform.adapter"
_HOST_ADAPTER_FALLBACK_MODULE = "plugins.platforms.discord.adapter"
_MAX_CHOICES = 24
_DISCORD_EMBED_FIELD_LIMIT = 1024
_DISCORD_EMBED_TOTAL_LIMIT = 6000
_EMBED_TRUNCATION_NOTE = "\n… [choice list truncated by Discord limits]"

# PlatformEntry fields that may be forwarded through PluginContext.  The
# registry has added fields over time; fields absent on an older host are
# simply omitted by _platform_registration_kwargs.
_PLATFORM_METADATA_FIELDS = (
    "validate_config",
    "required_env",
    "install_hint",
    "is_connected",
    "setup_fn",
    "allowed_users_env",
    "allow_all_env",
    "cron_deliver_env_var",
    "standalone_sender_fn",
    "max_message_length",
    "pii_safe",
    "emoji",
    "allow_update_command",
    "platform_hint",
    "env_enablement_fn",
    "apply_yaml_config_fn",
)


class CompatibilityError(RuntimeError):
    """Raised when the host does not expose the required Discord seam."""


def _validate_host(host: Any, module_name: str) -> Any:
    """Validate the small host surface used by this compatibility shim."""

    missing = [
        name
        for name in ("DiscordAdapter", "ClarifyChoiceView", "SendResult")
        if not hasattr(host, name)
    ]
    if missing:
        raise CompatibilityError(
            f"{module_name} is missing required symbols: " + ", ".join(missing)
        )
    if not getattr(host, "DISCORD_AVAILABLE", False) or getattr(host, "discord", None) is None:
        raise CompatibilityError(
            f"{module_name} cannot use discord.py; the bundled Discord adapter remains active"
        )
    return host


def _load_host() -> Any:
    """Load and validate the bundled Discord adapter on demand.

    Importing the plugin itself must stay cheap and must not make Discord a
    hard dependency for CLI-only Hermes sessions.  The host adapter already
    owns dependency checks and lazy installation, so this function only
    verifies that the symbols required by this narrow override exist.
    """

    errors: list[str] = []
    for module_name in (_HOST_ADAPTER_MODULE, _HOST_ADAPTER_FALLBACK_MODULE):
        try:
            host = importlib.import_module(module_name)
        except Exception as exc:
            errors.append(f"{module_name} import failed: {exc}")
            continue
        try:
            return _validate_host(host, module_name)
        except CompatibilityError as exc:
            errors.append(str(exc))

    raise CompatibilityError(
        "no compatible bundled Discord adapter namespace was found: "
        + "; ".join(errors)
    )


def _flatten_choice(choice: Any) -> str:
    """Normalize the same choice shapes accepted by the bundled adapter."""

    if choice is None:
        return ""
    if isinstance(choice, str):
        return choice.strip()
    if isinstance(choice, dict):
        for key in ("label", "description", "text", "title"):
            value = choice.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""
    if isinstance(choice, (list, tuple)):
        return " ".join(_flatten_choice(item) for item in choice).strip()
    return str(choice).strip()


def _normalise_choices(choices: Optional[Iterable[Any]]) -> list[str]:
    """Return non-empty, user-facing choices capped for Discord components."""

    return [
        value
        for value in (_flatten_choice(choice) for choice in (choices or []))
        if value
    ][:_MAX_CHOICES]


def _numbered_choices(choices: list[str]) -> str:
    return "\n".join(f"{index}. {choice}" for index, choice in enumerate(choices, 1))


def _utf16_len(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def _prefix_utf16(value: str, limit: int) -> str:
    """Return a prefix whose UTF-16 length does not exceed *limit*."""

    if _utf16_len(value) <= limit:
        return value
    output: list[str] = []
    used = 0
    for character in value:
        units = _utf16_len(character)
        if used + units > limit:
            break
        output.append(character)
        used += units
    return "".join(output)


def _bounded_text(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    if _utf16_len(text) <= limit:
        return text
    return _prefix_utf16(text, max(0, limit - 3)) + "..."


def _split_embed_value(value: str, limit: int = _DISCORD_EMBED_FIELD_LIMIT) -> list[str]:
    """Split a numbered list into Discord-safe field values without losing lines."""

    chunks: list[str] = []
    current = ""
    for line in value.splitlines():
        # A single choice can be longer than a field.  Split it conservatively;
        # the plain content and callback still retain the original choice.
        pieces: list[str] = []
        remaining = line
        while remaining:
            piece = _prefix_utf16(remaining, limit)
            if not piece:
                break
            pieces.append(piece)
            remaining = remaining[len(piece) :]
        if not pieces:
            pieces = [""]
        for piece in pieces:
            candidate = piece if not current else f"{current}\n{piece}"
            if _utf16_len(candidate) <= limit:
                current = candidate
            else:
                chunks.append(current)
                current = piece
    if current or not chunks:
        chunks.append(current)
    return chunks


def _add_choice_fields(embed: Any, numbered: str, question: str) -> None:
    """Add a complete list when possible, bounded to Discord's embed limits."""

    # Discord's aggregate embed budget is 6000 characters.  Leave room for
    # the title, description, field names, and the response hint.  Normal
    # prompts are unaffected; pathological prompts get a clear truncation note
    # instead of an HTTP 400 from the API.
    remaining = max(
        _DISCORD_EMBED_FIELD_LIMIT,
        _DISCORD_EMBED_TOTAL_LIMIT - _utf16_len(question) - 200,
    )
    display = numbered
    if _utf16_len(display) > remaining:
        display = _prefix_utf16(
            display,
            max(0, remaining - _utf16_len(_EMBED_TRUNCATION_NOTE)),
        ).rstrip()
        display += _EMBED_TRUNCATION_NOTE

    for index, chunk in enumerate(_split_embed_value(display)):
        name = "Choices" if index == 0 else "Choices (continued)"
        embed.add_field(name=name, value=chunk or "(none)", inline=False)


def _relabel_choice_buttons(view: Any, choice_count: int, clarify_id: str) -> None:
    """Change labels only; retain host callbacks and custom IDs unchanged."""

    choice_index = 1
    prefix = f"clarify:{clarify_id}:"
    for child in getattr(view, "children", []):
        custom_id = str(getattr(child, "custom_id", "") or "")
        if custom_id == f"{prefix}other" or custom_id.endswith(":other"):
            continue
        if choice_index > choice_count:
            break
        # The bundled view creates one choice button per choice.  Matching the
        # custom-ID prefix avoids touching a future non-choice child if Hermes
        # adds another component to the view.
        if custom_id.startswith(prefix):
            child.label = str(choice_index)
            choice_index += 1


async def _send_multiple_choice(
    adapter: Any,
    host: Any,
    view_class: type,
    chat_id: str,
    question: str,
    choices: list[str],
    clarify_id: str,
    session_key: str,
    metadata: Optional[dict[str, Any]],
) -> Any:
    """Copy only the host's current multiple-choice send branch."""

    if not adapter._client or not host.DISCORD_AVAILABLE:
        return host.SendResult(success=False, error="Not connected")

    try:
        target_id = chat_id
        if metadata and metadata.get("thread_id"):
            target_id = metadata["thread_id"]

        channel = adapter._client.get_channel(int(target_id))
        if not channel:
            channel = await adapter._client.fetch_channel(int(target_id))

        body = _bounded_text(question, 4088)
        numbered = _numbered_choices(choices)
        embed = host.discord.Embed(
            title="❓ Hermes needs your input",
            description=body,
            color=host.discord.Color.orange(),
        )
        _add_choice_fields(embed, numbered, body)
        embed.add_field(
            name="Reply",
            value="Pick a numbered button below, or click ✏️ Other to type a custom answer.",
            inline=False,
        )

        tail = "\n\nPick a numbered button below, or click ✏️ Other to type a custom answer."
        content_body = f"{str(question or '').strip()}\n\n{numbered}"
        content = adapter._self_contained_prompt_content(
            "❓ **Hermes needs your input**",
            content_body,
            tail=tail,
        )
        view = view_class(
            choices=choices,
            clarify_id=clarify_id,
            allowed_user_ids=adapter._allowed_user_ids,
            allowed_role_ids=adapter._allowed_role_ids,
        )
        message = await channel.send(content=content, embed=embed, view=view)
        view._message = message
        return host.SendResult(success=True, message_id=str(message.id))
    except Exception as exc:
        logger.warning(
            "[%s] readable Discord send_clarify failed: %s",
            getattr(adapter, "name", "discord"),
            exc,
        )
        return host.SendResult(success=False, error=str(exc))


def _build_override_classes(host: Any) -> tuple[type, type, type]:
    """Build subclasses against the exact host classes loaded at runtime."""

    base_view = host.ClarifyChoiceView
    base_adapter = host.DiscordAdapter

    class NumericClarifyChoiceView(base_view):
        """Host clarify view with numeric labels and untouched callbacks."""

        def __init__(
            self,
            choices: list[str],
            clarify_id: str,
            allowed_user_ids: set,
            allowed_role_ids: Optional[set] = None,
        ) -> None:
            super().__init__(
                choices=choices,
                clarify_id=clarify_id,
                allowed_user_ids=allowed_user_ids,
                allowed_role_ids=allowed_role_ids,
            )
            _relabel_choice_buttons(self, len(self.choices), clarify_id)

    class ReadableDiscordAdapter(base_adapter):
        """Discord adapter overriding only multiple-choice clarify rendering."""

        async def send_clarify(
            self,
            chat_id: str,
            question: str,
            choices: Optional[list],
            clarify_id: str,
            session_key: str,
            metadata: Optional[dict[str, Any]] = None,
        ) -> Any:
            clean_choices = _normalise_choices(choices)
            if not clean_choices:
                # This is intentional: open-ended prompts, including malformed
                # or all-empty choices, use the host's exact behavior.
                return await super().send_clarify(
                    chat_id,
                    question,
                    choices,
                    clarify_id,
                    session_key,
                    metadata,
                )
            return await _send_multiple_choice(
                self,
                host,
                NumericClarifyChoiceView,
                chat_id,
                question,
                clean_choices,
                clarify_id,
                session_key,
                metadata,
            )

    NumericClarifyChoiceView.__name__ = "NumericClarifyChoiceView"
    ReadableDiscordAdapter.__name__ = "ReadableDiscordAdapter"
    NumericClarifyChoiceView.__module__ = __name__
    ReadableDiscordAdapter.__module__ = __name__
    return base_adapter, NumericClarifyChoiceView, ReadableDiscordAdapter


def _platform_registration_kwargs(entry: Any, adapter_factory: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "name": "discord",
        "label": getattr(entry, "label", "Discord"),
        "adapter_factory": adapter_factory,
        "check_fn": getattr(entry, "check_fn"),
    }
    for field_name in _PLATFORM_METADATA_FIELDS:
        if hasattr(entry, field_name):
            value = getattr(entry, field_name)
            if field_name == "required_env" and value is not None:
                value = list(value)
            kwargs[field_name] = value
    return kwargs


def _filter_context_kwargs(ctx: Any, kwargs: dict[str, Any]) -> dict[str, Any]:
    """Avoid passing newer PlatformEntry fields to an older host context."""

    try:
        signature = inspect.signature(ctx.register_platform)
    except (TypeError, ValueError):
        return kwargs
    if any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    ):
        return kwargs
    return {name: value for name, value in kwargs.items() if name in signature.parameters}


def register(ctx: Any) -> None:
    """Register the Discord override, failing safe on incompatible hosts."""

    try:
        from gateway.platform_registry import platform_registry

        # Resolving the bundled entry first preserves auth, setup, YAML, cron,
        # standalone sending, and display metadata. Registry registration is
        # last-writer-wins, so our same-name entry becomes the active override.
        bundled_entry = platform_registry.get("discord")
        if bundled_entry is None:
            raise CompatibilityError("bundled Discord platform entry is unavailable")

        # In an installed Hermes runtime, resolving the deferred entry above
        # materializes the adapter under hermes_plugins.discord_platform. The
        # source-tree namespace remains available for development/test hosts.
        host = _load_host()
        _, _, adapter_class = _build_override_classes(host)
        kwargs = _platform_registration_kwargs(
            bundled_entry,
            lambda config: adapter_class(config),
        )
        ctx.register_platform(**_filter_context_kwargs(ctx, kwargs))
        logger.info("Registered Discord clarify readability override")
    except Exception as exc:
        # A disabled/missing/incompatible plugin must never replace a working
        # bundled adapter with a partially initialized one.
        logger.warning(
            "Discord clarify readability override disabled; keeping bundled adapter: %s",
            exc,
        )
