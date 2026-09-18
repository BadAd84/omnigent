"""Explicit local entrypoint for the workspace-profile prototype."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, cast

import yaml

from omnigent.experimental.workspace_profiles.launcher import WorkspaceProfileLauncher
from omnigent.experimental.workspace_profiles.profiles import WorkspaceProfile, load_profiles
from omnigent.onboarding.sandboxes.agent_sandbox_warm_pool import API_VERSION, EXTENSION_GROUP
from omnigent.onboarding.sandboxes.base import SandboxHostLauncher


def install_factory(
    profiles: Sequence[WorkspaceProfile], *, profile_name: str | None = None
) -> None:
    """Substitute the prototype factory in this explicitly opted-in process."""
    from omnigent.server import managed_hosts

    original = managed_hosts._kubernetes_launcher_factory

    def build(*, agent_sandbox: bool = False, **kwargs: Any) -> Callable[[], SandboxHostLauncher]:
        fallback = original(agent_sandbox=agent_sandbox, **kwargs)
        if not agent_sandbox:
            return fallback
        return lambda: WorkspaceProfileLauncher(
            profiles=profiles, profile_name=profile_name, **kwargs
        )

    managed_hosts._kubernetes_launcher_factory = build


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profiles", type=Path, required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="Run the ordinary server with profile selection")
    serve.add_argument("server_args", nargs=argparse.REMAINDER)
    render = commands.add_parser("render", help="Generate a selected profile's template and pool")
    render.add_argument("--config", type=Path, required=True)
    render.add_argument("--profile", required=True)
    render.add_argument("--replicas", type=int, default=1)
    args = parser.parse_args()
    profiles = load_profiles(args.profiles)
    if args.command == "serve":
        install_factory(profiles)
        from omnigent.cli import cli

        server_args = args.server_args
        if server_args[:1] == ["--"]:
            server_args = server_args[1:]
        cli(["server", *server_args])
        return
    if args.replicas < 0:
        parser.error("--replicas must be non-negative")
    selected = next((profile for profile in profiles if profile.name == args.profile), None)
    if selected is None:
        parser.error("the selected profile is not in the operator catalog")
    install_factory(profiles, profile_name=args.profile)
    from omnigent.server.managed_hosts import parse_sandbox_config

    raw = yaml.safe_load(args.config.read_text())
    deployment = parse_sandbox_config(raw.get("sandbox"))
    entry = deployment.for_provider("agent_sandbox") if deployment else None
    if entry is None:
        parser.error("server config must enable provider: agent_sandbox")
    launcher = cast(WorkspaceProfileLauncher, entry.launcher_factory())
    metadata = {"name": selected.warm_pool, "namespace": launcher._resolve_namespace()}
    template = {
        "apiVersion": f"{EXTENSION_GROUP}/{API_VERSION}",
        "kind": "SandboxTemplate",
        "metadata": metadata,
        "spec": {
            **launcher.template_spec(shared=True),
            "networkPolicyManagement": "Unmanaged",
        },
    }
    pool = {
        "apiVersion": f"{EXTENSION_GROUP}/{API_VERSION}",
        "kind": "SandboxWarmPool",
        "metadata": metadata,
        "spec": {
            "replicas": args.replicas,
            "sandboxTemplateRef": {"name": selected.warm_pool},
            "updateStrategy": {"type": "Recreate"},
        },
    }
    print(yaml.safe_dump_all([template, pool], sort_keys=False), end="")


if __name__ == "__main__":
    main()
