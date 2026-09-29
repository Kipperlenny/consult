"""Locations and configuration: the toolkit root, its .env, and the project the current directory belongs to.

A project is recognised by `.consult/config.toml` – or `private/consult/config.toml` for public repositories whose
consulting material must stay out of git – searched upwards from the current directory (`CONSULT_CONFIG=<path>`
overrides). Without one, `consult` still works with defaults and writes into `./.consult/reviews/`.
"""

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # the consult checkout
ENV_FILE = ROOT / ".env"  # plain file, or a symlink from a secret manager (dangles while that is locked)
CONFIG_CANDIDATES = (".consult/config.toml", "private/consult/config.toml")
LANGUAGE_NAMES = {"de": "German", "en": "English", "es": "Spanish", "fr": "French"}
MAX_FILE_CHARS = 40_000  # per context file; enough for a goal.md, cheap enough to send every time


class ConsultError(Exception):
    """A problem to show the user in one line, without a traceback."""


def env(key: str, default: str | None = None) -> str | None:
    """A setting from the process environment, else from the toolkit's .env (never from a project's .env)."""
    if os.environ.get(key):
        return os.environ[key]
    try:
        text = ENV_FILE.read_text(encoding="utf-8")
    except FileNotFoundError:
        return default
    m = re.search(r"^%s=(.+)$" % re.escape(key), text, re.M)
    return m.group(1).strip().strip("\"'") if m else default


def setting(*keys: str, default: str | None = None) -> str | None:
    """The first of several names that is set – new generic names first, older OPENAI_* names still honoured."""
    for key in keys:
        value = env(key)
        if value:
            return value
    return default


def require_env(key: str) -> str:
    value = env(key)
    if not value:
        if ENV_FILE.is_symlink() and not ENV_FILE.exists():
            raise ConsultError(f"{key} not set and {ENV_FILE} is a dangling symlink – unlock the secret store it "
                               "points into, or export the key")
        if not ENV_FILE.exists():
            raise ConsultError(f"{key} not set – export it, or put it in {ENV_FILE} (see .env.example)")
        raise ConsultError(f"{key} missing in {ENV_FILE}")
    return value


@dataclass
class Persona:
    name: str
    title: str  # who they are, one sentence – becomes "You are: …"
    instructions: str = ""  # what to do with what they see; used by panel/review, optional for ask
    language: str = ""  # answer language override (else the project's)
    source: str = "global"  # "global" or the project's config path


@dataclass
class Project:
    root: Path
    config_path: Path | None
    name: str
    site: str = ""
    language: str = "en"
    framing: str = ""  # what the project is and what it is trying to achieve; in every system prompt
    kpi: str = "the project's next measurable goal"  # what the synthesis ranks actions by
    context_files: list[str] = field(default_factory=list)
    reviews_dir: Path = field(default_factory=lambda: Path(".consult/reviews"))
    memory_file: Path = field(default_factory=lambda: Path(".consult/memory.md"))
    never_send: list[str] = field(default_factory=list)  # paths that must never go to a consultant
    default_role: str = ""
    panel_personas: list[str] = field(default_factory=list)
    review_personas: list[str] = field(default_factory=list)
    model: str = ""
    effort: str = ""
    personas: dict[str, Persona] = field(default_factory=dict)

    @property
    def threads_dir(self) -> Path:
        return self.reviews_dir / "threads"

    @property
    def config_dir(self) -> Path | None:
        return self.config_path.parent if self.config_path else None

    def language_name(self, code: str = "") -> str:
        code = code or self.language
        return LANGUAGE_NAMES.get(code, code)

    def check_sendable(self, path: Path, allow_outside: bool = False) -> Path:
        """Refuse files the project marked as never-to-send (personal data, subscriber exports …). Every file that
        goes out – context files, --files, --images – passes here. Both the path as given (normalised, so `a/../data`
        is `data`) and its resolved target (symlinks) are checked; a file outside the project needs `allow_outside`.
        Returns the resolved path. This matches paths, not content."""
        given = Path(os.path.normpath(path if path.is_absolute() else self.root / path))
        real = given.resolve()
        rels = []
        for p in dict.fromkeys((given, real)):
            if p.is_relative_to(self.root):
                rels.append(p.relative_to(self.root).as_posix())
            elif not allow_outside:
                via = f" (symlink to {real})" if p == real and given != real else ""
                raise ConsultError(f"refusing to send {path}{via}: outside the project {self.root} – pass "
                                   "--allow-outside to send it anyway")
        for rel in rels:
            for pattern in self.never_send:
                p = pattern.rstrip("/")
                if rel == p or rel.startswith(p + "/") or Path(rel).match(pattern):
                    raise ConsultError(f"refusing to send {path}: {rel} matches never_send pattern {pattern!r} in "
                                       f"{self.config_path}")
        return real


def find_project(start: Path | None = None) -> Project:
    explicit = os.environ.get("CONSULT_CONFIG")
    if explicit:
        return load_project(Path(explicit).expanduser().resolve())
    start = (start or Path.cwd()).resolve()
    for d in (start, *start.parents):
        for rel in CONFIG_CANDIDATES:
            if (d / rel).is_file():
                return load_project(d / rel, root=d)
    return Project(root=start, config_path=None, name=start.name, reviews_dir=start / ".consult" / "reviews",
                   memory_file=start / ".consult" / "memory.md")


def load_project(path: Path, root: Path | None = None) -> Project:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise ConsultError(f"{path}: {e}") from e
    if root is None:  # .consult/config.toml → parent of .consult; private/consult/config.toml → parent of private
        root = path.parents[2] if path.parent.parent.name == "private" else path.parents[1]
    cfg_dir = path.parent
    p, m = data.get("project", {}), data.get("model", {})

    def rel(value, default):
        return (root / value).resolve() if value else default

    project = Project(
        root=root,
        config_path=path,
        name=p.get("name", root.name),
        site=p.get("site", "").rstrip("/"),
        language=p.get("language", "en"),
        framing=p.get("framing", "").strip(),
        kpi=p.get("kpi", Project.kpi),
        context_files=list(p.get("context_files", [])),
        reviews_dir=rel(p.get("reviews_dir"), cfg_dir / "reviews"),
        memory_file=rel(p.get("memory"), cfg_dir / "memory.md"),
        never_send=list(p.get("never_send", [])),
        default_role=p.get("default_role", ""),
        panel_personas=list(p.get("panel_personas", [])),
        review_personas=list(p.get("review_personas", [])),
        model=m.get("name", ""),
        effort=m.get("effort", ""),
    )
    for name, spec in data.get("personas", {}).items():
        project.personas[name] = Persona(name, spec["title"], spec.get("instructions", ""), spec.get("language", ""),
                                         source=str(path))
    for f in sorted((cfg_dir / "personas").glob("*.toml")):
        spec = tomllib.loads(f.read_text(encoding="utf-8"))
        project.personas[f.stem] = Persona(f.stem, spec["title"], spec.get("instructions", ""),
                                           spec.get("language", ""), source=str(f))
    return project
