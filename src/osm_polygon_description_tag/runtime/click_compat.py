"""The one place allowed to import Typer's vendored Click exceptions.

Typer does not re-export ``ClickException`` or ``UsageError`` publicly and,
since 0.26, vendors Click as the private ``typer._click`` package instead of
depending on ``click``. ``pyproject.toml`` pins ``typer>=0.26`` for that
reason; ``Exit`` comes from public ``typer`` because 0.27.2 moved it out of
``typer._click``. Ruff ``TID251`` bans ``typer._click`` everywhere else.
"""

from typer import Exit
from typer._click.exceptions import ClickException, UsageError  # noqa: TID251

__all__ = ["ClickException", "Exit", "UsageError"]
