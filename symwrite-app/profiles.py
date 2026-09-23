"""Create portable profiles from explicitly chosen text, without modifying the active one."""

import argparse
from pathlib import Path
import uuid

import yaml

from backend.configuration import APP_DIR, Profile, Source, load_profile


def write_profile(profile: Profile, output: Path):
    output.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive create: never replace an active/private profile accidentally.
    with output.open("x", encoding="utf-8") as handle:
        handle.write(
            yaml.safe_dump(
                profile.model_dump(mode="json"), sort_keys=False, allow_unicode=True
            )
        )
    output.chmod(0o600)


def create_profile(name, writings, facts, style=None):
    template = load_profile(APP_DIR / "config/config.yaml")
    sources = []
    for i, filename in enumerate(writings):
        path = Path(filename)
        if path.suffix.lower() not in {".txt", ".md"} or path.stat().st_size > 500_000:
            raise ValueError("Use .txt or .md writing samples under 500 KB each")
        text = path.read_text(encoding="utf-8").strip()
        if not text:
            raise ValueError("Writing samples must not be empty")
        for j in range(0, len(text), 4500):
            sources.append(
                Source(
                    id=f"writing-{i + 1}-{j // 4500 + 1}",
                    title=path.stem[:140],
                    kind="writing",
                    text=text[j : j + 4500],
                )
            )
    for i, fact in enumerate(facts):
        sources.append(
            Source(
                id=f"fact-{i + 1}",
                title=f"Approved fact {i + 1}",
                kind="fact",
                text=fact,
            )
        )
    return Profile(
        id=str(uuid.uuid4()),
        name=name,
        synthetic=False,
        style=style or template.style,
        models=template.models,
        sources=tuple(sources),
    )


def migrate_profile(path, name=None):
    """Recover the original September 2025 config's user-selected facts and prose."""
    if path.stat().st_size > 1_500_000:
        raise ValueError("Legacy profile is too large")
    data = yaml.safe_load(path.read_text())
    user = data["user"]
    template = load_profile(APP_DIR / "config/config.yaml")
    sources = []
    for field, kind in (("facts", "fact"), ("writing_samples", "writing")):
        for i, value in enumerate(user.get(field, [])):
            if not isinstance(value, str):
                raise ValueError("Legacy facts and writing samples must be plain text")
            for j in range(0, len(value), 4500):
                sources.append(
                    Source(
                        id=f"{kind}-{i + 1}-{j // 4500 + 1}",
                        title=f"Imported {kind} {i + 1}",
                        kind=kind,
                        text=value[j : j + 4500],
                    )
                )
    return Profile(
        id=str(uuid.uuid4()),
        name=name or user["name"],
        synthetic=False,
        style=template.style,
        models=template.models,
        sources=tuple(sources),
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create")
    create.add_argument("--name", required=True)
    create.add_argument("--writing", type=Path, action="append", default=[])
    create.add_argument("--fact", action="append", default=[])
    create.add_argument("--style")
    create.add_argument("--output", type=Path, required=True)
    migrate = commands.add_parser("migrate")
    migrate.add_argument("legacy", type=Path)
    migrate.add_argument("--name")
    migrate.add_argument("--output", type=Path, required=True)
    validate = commands.add_parser("validate")
    validate.add_argument("profile", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "validate":
            load_profile(args.profile)
            print("Profile is valid.")
            return
        profile = (
            create_profile(args.name, args.writing, args.fact, args.style)
            if args.command == "create"
            else migrate_profile(args.legacy, args.name)
        )
        write_profile(profile, args.output)
        print(
            f"Created {args.output}. Set SYMWRITE_CONFIG to its absolute path, then restart SymWrite."
        )
    except (ValueError, OSError, KeyError, TypeError):
        # Validation exceptions can include private source text; keep CLI output content-free.
        parser.exit(
            2,
            "Profile was not created. Check the schema, selected text files, and that the output path does not already exist.\n",
        )


if __name__ == "__main__":
    main()
