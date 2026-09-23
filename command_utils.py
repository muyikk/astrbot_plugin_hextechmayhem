from __future__ import annotations


def recover_command_query(
    raw_message: str,
    fallback: str,
    root_commands: set[str],
    subcommands: set[str],
) -> str:
    """Recover a complete trailing query from an AstrBot command message."""
    tokens = str(raw_message or "").strip().lstrip("/").split()
    for index, token in enumerate(tokens[:-1]):
        if token in subcommands and (
            index == 0
            or any(root in tokens[: index + 1] for root in root_commands)
        ):
            recovered = " ".join(tokens[index + 1 :]).strip()
            if recovered:
                return recovered
    return str(fallback or "").strip()
