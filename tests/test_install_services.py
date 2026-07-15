import os
import shutil
import socket
import subprocess
from pathlib import Path


def test_installer_renders_units_from_current_clone_without_fixed_paths(tmp_path):
    project = tmp_path / "tmux-bridge"
    scripts = project / "scripts"
    systemd = project / "systemd"
    scripts.mkdir(parents=True)
    systemd.mkdir()

    source_root = Path(__file__).resolve().parents[1]
    shutil.copy2(source_root / "scripts/install-services.sh", scripts)
    for template in (source_root / "systemd").glob("*.service.in"):
        shutil.copy2(template, systemd)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    codex = fake_bin / "codex"
    codex.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    codex.chmod(0o700)
    fake_systemctl = fake_bin / "systemctl"
    fake_systemctl.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_systemctl.chmod(0o700)

    socket_path = tmp_path / "codex" / "tmux-bridge.sock"
    socket_path.parent.mkdir()
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(socket_path))
    listener.listen()

    (project / ".env").write_text(
        "\n".join([
            "FEISHU_APP_ID=test-app",
            "FEISHU_APP_SECRET=test-secret",
            f"APP_SERVER_SOCKET={socket_path}",
            f"WORK_DIR={tmp_path}",
        ]) + "\n",
        encoding="utf-8",
    )
    env = {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "PATH": f"{fake_bin}:/usr/bin:/bin",
    }
    try:
        subprocess.run(
            [str(scripts / "install-services.sh")],
            cwd=project,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
    finally:
        listener.close()

    units = tmp_path / "config/systemd/user"
    bridge = (units / "tmux-bridge.service").read_text(encoding="utf-8")
    appserver = (units / "tmux-bridge-appserver.service").read_text(encoding="utf-8")
    runtime = (project / ".runtime.env").read_text(encoding="utf-8")
    assert f"WorkingDirectory={project}" in bridge
    assert f"ExecStart={project}/start.sh" in bridge
    assert f"ExecStart={codex} app-server" in appserver
    assert str(socket_path) in bridge
    assert str(socket_path) in appserver
    assert "@ROOT@" not in bridge + appserver
    assert "/data00/home/" not in bridge + appserver
    assert f"CODEX_BIN={codex}" in runtime
    assert f"TMUX_BRIDGE_PATH={fake_bin}:/usr/bin:/bin" in runtime
