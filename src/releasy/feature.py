"""Feature management: add, enable, disable, remove, list.

Features live in the session file (``<target_branch>.session.yaml`` by
default), not in ``config.yaml``. Every mutation here writes back via
:func:`save_session`.
"""

from __future__ import annotations

from releasy.termlog import console
from rich.table import Table

from releasy.config import Config, FeatureConfig, save_session


def _require_session(config: Config):
    """Return the live session, or raise a user-facing error.

    All feature subcommands mutate the session, so reaching this with
    ``config.session is None`` is a harness/CLI bug, not a user-facing
    condition — the CLI must have loaded the session already.
    """
    if config.session is None:
        raise RuntimeError(
            "feature subcommands need the session file loaded — this is a "
            "CLI wiring bug."
        )
    return config.session


def add_feature(
    config: Config,
    feature_id: str,
    source_branch: str,
    description: str,
    enabled: bool = True,
) -> bool:
    """Add a new feature to the session."""
    session = _require_session(config)
    if config.get_feature(feature_id):
        console.print(f"[red]Feature '{feature_id}' already exists[/red]")
        return False

    session.features.append(
        FeatureConfig(
            id=feature_id,
            description=description,
            source_branch=source_branch,
            enabled=enabled,
        )
    )
    save_session(session)
    console.print(
        f"[green]✓[/green] Added feature [cyan]{feature_id}[/cyan] "
        f"(source: {source_branch}, branch: feature/<base>/{feature_id})"
    )
    return True


def enable_feature(config: Config, feature_id: str) -> bool:
    session = _require_session(config)
    feat = config.get_feature(feature_id)
    if feat is None:
        console.print(f"[red]Feature '{feature_id}' not found[/red]")
        return False
    if feat.enabled:
        console.print(f"[yellow]Feature '{feature_id}' is already enabled[/yellow]")
        return True
    feat.enabled = True
    save_session(session)
    console.print(f"[green]✓[/green] Enabled feature [cyan]{feature_id}[/cyan]")
    return True


def disable_feature(config: Config, feature_id: str) -> bool:
    session = _require_session(config)
    feat = config.get_feature(feature_id)
    if feat is None:
        console.print(f"[red]Feature '{feature_id}' not found[/red]")
        return False
    if not feat.enabled:
        console.print(f"[yellow]Feature '{feature_id}' is already disabled[/yellow]")
        return True
    feat.enabled = False
    save_session(session)
    console.print(f"[green]✓[/green] Disabled feature [cyan]{feature_id}[/cyan]")
    return True


def remove_feature(config: Config, feature_id: str) -> bool:
    session = _require_session(config)
    feat = config.get_feature(feature_id)
    if feat is None:
        console.print(f"[red]Feature '{feature_id}' not found[/red]")
        return False
    session.features = [f for f in session.features if f.id != feature_id]
    save_session(session)
    console.print(f"[green]✓[/green] Removed feature [cyan]{feature_id}[/cyan]")
    return True


def list_features(config: Config) -> None:
    table = Table(title="Features")
    table.add_column("ID", style="cyan")
    table.add_column("Source Branch")
    table.add_column("Versioned Prefix")
    table.add_column("Depends On")
    table.add_column("Description")
    table.add_column("Enabled")

    for feat in config.features:
        enabled_str = "[green]yes[/green]" if feat.enabled else "[dim]no[/dim]"
        prefix = f"feature/<base>/{feat.id}"
        deps = ", ".join(feat.depends_on) if feat.depends_on else ""
        table.add_row(
            feat.id, feat.source_branch, prefix,
            deps, feat.description, enabled_str,
        )

    if not config.features:
        console.print("[dim]No features configured.[/dim]")
    else:
        console.print(table)
