from __future__ import annotations

import subprocess

from forven import self_update


def test_git_uses_configured_creation_flags(monkeypatch):
    captured: dict[str, object] = {}

    def fake_run(*args, **kwargs):
        captured.update(kwargs)
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="ok\n", stderr="")

    monkeypatch.setattr(self_update.subprocess, "run", fake_run)

    result = self_update._git("rev-parse", "HEAD")

    assert result.stdout == "ok\n"
    assert captured["shell"] is False
    assert captured["creationflags"] == self_update._GIT_CREATION_FLAGS


def test_windows_git_creation_flags_hide_console():
    if self_update.os.name != "nt":
        return

    assert self_update._GIT_CREATION_FLAGS & getattr(
        subprocess,
        "CREATE_NO_WINDOW",
        0x08000000,
    )
