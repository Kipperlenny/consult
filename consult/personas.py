"""Personas: reusable experts in personas/*.toml (global), overridden or extended per project."""

import tomllib

from .config import ROOT, ConsultError, Persona, Project

GLOBAL_DIR = ROOT / "personas"


def global_personas() -> dict[str, Persona]:
    out = {}
    for f in sorted(GLOBAL_DIR.glob("*.toml")):
        spec = tomllib.loads(f.read_text(encoding="utf-8"))
        out[f.stem] = Persona(f.stem, spec["title"], spec.get("instructions", ""), spec.get("language", ""))
    return out


def all_personas(project: Project) -> dict[str, Persona]:
    return global_personas() | project.personas  # project wins on the same name


def resolve(project: Project, names: list[str]) -> list[Persona]:
    have = all_personas(project)
    missing = [n for n in names if n not in have]
    if missing:
        raise ConsultError(f"unknown persona(s): {', '.join(missing)}. Known: {', '.join(sorted(have))}")
    return [have[n] for n in names]


def ad_hoc(title: str) -> Persona:
    """A one-off persona from a --role string."""
    return Persona("adhoc", title, source="--role")
