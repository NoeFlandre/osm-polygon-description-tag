"""RED tests proving the public CLI has no test-hook injection flags.

The public ``run-and-publish`` command must accept only ``--confirm-repo``
(and the standard shared ``--source-root`` / ``--data-root`` / ``--osmium``
flags). The internal ``--preflight``, ``--upload-runner``, ``--publisher``,
and ``--clock`` hooks must not appear in the public CLI surface.
"""

from __future__ import annotations

from osm_polygon_description_tag.cli import create_parser


def _subparser_text(command: str) -> str:
    """Return the formatted help for a specific subcommand."""
    parser = create_parser()
    # Format the help for the chosen subcommand.
    sub_actions = [
        action
        for action in parser._actions  # type: ignore[attr-defined]
        if action.__class__.__name__ == "_SubParsersAction"
    ]
    assert sub_actions, "no subparsers defined"
    subparsers_action = sub_actions[0]
    subparser = subparsers_action.choices[command]
    return subparser.format_help()


def test_run_and_publish_does_not_expose_preflight_flag() -> None:
    """``--preflight`` is not a public CLI flag."""
    text = _subparser_text("run-and-publish")
    assert "--preflight" not in text


def test_run_and_publish_does_not_expose_upload_runner_flag() -> None:
    """``--upload-runner`` is not a public CLI flag."""
    text = _subparser_text("run-and-publish")
    assert "--upload-runner" not in text


def test_run_and_publish_does_not_expose_clock_flag() -> None:
    """``--clock`` is not a public CLI flag."""
    text = _subparser_text("run-and-publish")
    assert "--clock" not in text


def test_publish_does_not_expose_publisher_flag() -> None:
    """The ``publish`` subcommand must not expose ``--publisher``."""
    text = _subparser_text("publish")
    assert "--publisher" not in text


def test_run_and_publish_requires_confirm_repo() -> None:
    """The run-and-publish command still requires ``--confirm-repo``."""

    from osm_polygon_description_tag.cli import run

    try:
        exit_code = run(["run-and-publish"])
        assert exit_code != 0
    except SystemExit as exc:
        # argparse calls sys.exit(2) on missing required args; that's fine.
        assert exc.code != 0


def test_run_and_publish_help_lists_only_public_flags() -> None:
    """The run-and-publish help lists only the shared flags plus --confirm-repo."""
    text = _subparser_text("run-and-publish")
    for allowed in ("--source-root", "--data-root", "--osmium", "--confirm-repo"):
        assert allowed in text, f"missing public flag in run-and-publish: {allowed}"
    for forbidden in ("--preflight", "--upload-runner", "--clock"):
        assert forbidden not in text, f"public CLI exposes test-hook: {forbidden}"
