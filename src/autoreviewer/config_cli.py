"""Write an operator-owned Prism configuration without loading a provider or handling secrets."""

import argparse
from pathlib import Path

from pydantic import ValidationError

from autoreviewer.model_config import PrismConfig


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--finder", help="provider:model that finds problems")
    parser.add_argument("--checker", help="different provider:model that verifies findings")
    parser.add_argument("--finder-lanes", type=int, help="maximum concurrent finder requests (default: 1)")
    parser.add_argument("--checker-lanes", type=int, help="maximum concurrent checker requests (default: 1)")
    parser.add_argument("--output", type=Path, default=Path("prism.json"), help="new file (default: prism.json)")
    args = parser.parse_args(argv)
    try:
        finder = args.finder or input("Finder model (provider:model): ").strip()
        checker = args.checker or input("Checker model (provider:model): ").strip()
        lanes = {
            spec: count
            for spec, count in ((finder, args.finder_lanes), (checker, args.checker_lanes))
            if count is not None
        }
        config = PrismConfig(finder=finder, checker=checker, lanes=lanes)
        with args.output.open("x", encoding="utf-8") as handle:
            handle.write(config.model_dump_json(indent=2, exclude_defaults=True) + "\n")
    except (ValidationError, OSError, EOFError) as exc:
        parser.exit(2, f"Configuration not completed: {exc}\n")
    print(f"Wrote {args.output.resolve()}. Set PRISM_CONFIG to its path inside the receiver.")
