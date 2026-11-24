"""Choose the agent provider for each role. Only Codex is available for now."""


def create_provider(name, config, audit, stop):
    """Create the selected agent provider or reject an unsupported name."""

    if name != "codex":
        raise ValueError("Unsupported provider; only codex is implemented")
    from .codex import CodexProvider

    return CodexProvider(config, audit, stop)
