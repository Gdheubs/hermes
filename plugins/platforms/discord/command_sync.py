from typing import Any, Dict, Optional


class _CommandSyncHelpers:
    def _canonicalize_app_command_payload(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Reduce command payloads to the semantic fields Hermes manages."""
        contexts = payload.get("contexts")
        integration_types = payload.get("integration_types")
        return {
            "type": int(payload.get("type", 1) or 1),
            "name": str(payload.get("name", "") or ""),
            "description": str(payload.get("description", "") or ""),
            "default_member_permissions": self._normalize_permissions(
                payload.get("default_member_permissions")
            ),
            "dm_permission": bool(payload.get("dm_permission", True)),
            "nsfw": bool(payload.get("nsfw", False)),
            "contexts": sorted(int(c) for c in contexts) if contexts else None,
            "integration_types": (
                sorted(int(i) for i in integration_types) if integration_types else None
            ),
            "options": [
                self._canonicalize_app_command_option(item)
                for item in payload.get("options", []) or []
                if isinstance(item, dict)
            ],
        }

    @staticmethod
    def _normalize_permissions(value: Any) -> Optional[str]:
        """Discord emits default_member_permissions as str server-side but discord.py
        sets it as int locally. Normalize to str-or-None so the comparison is stable."""
        if value is None:
            return None
        return str(value)

    def _existing_command_to_payload(self, command: Any) -> Dict[str, Any]:
        """Build a canonical-ready dict from an AppCommand.

        discord.py's AppCommand.to_dict() does NOT include nsfw,
        dm_permission, or default_member_permissions (they live only on the
        attributes). Pull them from the attributes so the canonicalizer sees
        the real server-side values instead of defaults — otherwise any
        command using non-default permissions would diff on every startup.
        """
        payload = dict(command.to_dict())
        nsfw = getattr(command, "nsfw", None)
        if nsfw is not None:
            payload["nsfw"] = bool(nsfw)
        guild_only = getattr(command, "guild_only", None)
        if guild_only is not None:
            payload["dm_permission"] = not bool(guild_only)
        default_permissions = getattr(command, "default_member_permissions", None)
        if default_permissions is not None:
            payload["default_member_permissions"] = getattr(
                default_permissions, "value", default_permissions
            )
        return payload

    def _canonicalize_app_command_option(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "type": int(payload.get("type", 0) or 0),
            "name": str(payload.get("name", "") or ""),
            "description": str(payload.get("description", "") or ""),
            "required": bool(payload.get("required", False)),
            "autocomplete": bool(payload.get("autocomplete", False)),
            "choices": [
                {
                    "name": str(choice.get("name", "") or ""),
                    "value": choice.get("value"),
                }
                for choice in payload.get("choices", []) or []
                if isinstance(choice, dict)
            ],
            "channel_types": list(payload.get("channel_types", []) or []),
            "min_value": payload.get("min_value"),
            "max_value": payload.get("max_value"),
            "min_length": payload.get("min_length"),
            "max_length": payload.get("max_length"),
            "options": [
                self._canonicalize_app_command_option(item)
                for item in payload.get("options", []) or []
                if isinstance(item, dict)
            ],
        }

    def _patchable_app_command_payload(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Fields supported by discord.py's edit_global_command route."""
        canonical = self._canonicalize_app_command_payload(payload)
        return {
            "name": canonical["name"],
            "description": canonical["description"],
            "options": canonical["options"],
        }
