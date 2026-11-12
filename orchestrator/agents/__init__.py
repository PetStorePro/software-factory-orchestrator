"""Small provider selection point; workflow code is independent of Codex."""


def create_provider(name, config, audit, stop):
    if name != "codex":
        raise ValueError("Unsupported provider; only codex is implemented")
    from .codex import CodexProvider

    return CodexProvider(config, audit, stop)
