"""Write an operator-owned Prism configuration without loading a provider or handling secrets."""

import argparse
from pathlib import Path

from pydantic import ValidationError

from autoreviewer.model_config import PrismConfig


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--finder", help="provider:model that finds problems")
    parser.add_argument("--checker", help="different provider:model that verifies findings")
    parser.add_argument("--overflow", help="optional overflow finder spec, independent of the checker")
    parser.add_argument("--overflow-url", metavar="URL", help="override the endpoint for a local:<id> overflow finder")
    parser.add_argument("--overflow-lanes", type=int, help="maximum concurrent overflow requests (default: uncapped)")
    parser.add_argument("--local-context", type=int, metavar="TOKENS", help="served window per local model")
    parser.add_argument("--local-url", metavar="URL", help="default endpoint for local:<id> models")
    parser.add_argument("--finder-url", metavar="URL", help="override the endpoint for a local:<id> finder")
    parser.add_argument("--checker-url", metavar="URL", help="override the endpoint for a local:<id> checker")
    parser.add_argument("--finder-lanes", type=int, help="maximum concurrent finder requests (default: 1)")
    parser.add_argument("--checker-lanes", type=int, help="maximum concurrent checker requests (default: 1)")
    parser.add_argument("--output", type=Path, default=Path("prism.json"), help="new file (default: prism.json)")
    args = parser.parse_args(argv)
    try:
        finder = args.finder or input("Finder model (provider:model): ").strip()
        checker = args.checker or input("Checker model (provider:model): ").strip()
        roles = [
            ("finder", finder, args.finder_url, args.finder_lanes),
            ("checker", checker, args.checker_url, args.checker_lanes),
            ("overflow", args.overflow, args.overflow_url, args.overflow_lanes),
        ]
        lanes = {}
        endpoints = {}
        used_default = False
        for role, spec, url, count in roles:
            if spec is None:
                if url is not None or count is not None:
                    parser.error(f"--{role}-url and --{role}-lanes require --{role}")
                continue
            if count is not None and lanes.setdefault(spec, count) != count:
                parser.error(f"conflicting lane counts for {spec}")
            if not spec.startswith("local:"):
                if url is not None:
                    parser.error(f"--{role}-url requires --{role} to use local:<id>")
                continue
            if url is None and args.local_url is not None:
                url = args.local_url
                used_default = True
            if url is not None and endpoints.setdefault(spec, url) != url:
                parser.error(f"conflicting endpoints for {spec}")
        if args.local_url is not None and not used_default:
            parser.error("--local-url needs a local:<id> role without a role-specific URL")
        options: dict[str, object] = {"finder": finder, "checker": checker, "overflow": args.overflow, "lanes": lanes}
        if args.local_context is not None:
            if not endpoints:
                parser.error("--local-context requires a local model endpoint")
            options["local_context"] = dict.fromkeys(endpoints, args.local_context)
        if endpoints:
            options["endpoints"] = endpoints
        config = PrismConfig.model_validate(options)
        with args.output.open("x", encoding="utf-8") as handle:
            handle.write(config.model_dump_json(indent=2, exclude_defaults=True) + "\n")
    except (ValidationError, OSError, EOFError) as exc:
        parser.exit(2, f"Configuration not completed: {exc}\n")
    print(f"Wrote {args.output.resolve()}. Set PRISM_CONFIG to its path inside the receiver.")
