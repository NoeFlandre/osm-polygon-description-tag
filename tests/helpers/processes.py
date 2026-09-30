"""Subprocess response builders for offline integration tests."""


def preflight_response(command: list[str], output: str | bytes, *, text: bool) -> object:
    """Return a completed-process stub matching subprocess text/binary mode."""
    import subprocess

    if text:
        stdout = output if isinstance(output, str) else output.decode("utf-8")
        return subprocess.CompletedProcess(command, returncode=0, stdout=stdout, stderr="")
    stdout = output if isinstance(output, bytes) else output.encode("utf-8")
    return subprocess.CompletedProcess(command, returncode=0, stdout=stdout, stderr=b"")


def standard_preflight_runner(command: list[str], text: bool = False, **_kwargs: object) -> object:
    """Answer the standard offline version, identity, font, and revision probes."""
    if command == ["osmium", "--version"]:
        return preflight_response(command, "osmium version 1.19.1\n", text=text)
    if command == ["hf", "auth", "whoami"]:
        return preflight_response(command, "fake-user\n", text=text)
    if command and command[0] == "fc-list":
        return preflight_response(command, "", text=text)
    if len(command) >= 3 and command[0] == "git" and "rev-parse" in command:
        return preflight_response(command, "abc123\n", text=text)
    raise AssertionError(f"unexpected preflight subprocess: {command!r}")
