"""Exercise portable launchers and browser setup without Facebook or Telegram."""
import hashlib
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

import start
from app.pilot_browser import PilotBrowser, installed_browser


ROOT = Path(__file__).resolve().parent.parent


def prepared_environment(tmp_path):
    environment = tmp_path / ".venv"
    executable = environment / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    executable.parent.mkdir(parents=True)
    executable.touch()
    (tmp_path / "requirements.txt").write_text("example==1.0\n", encoding="ascii")
    return environment, executable


def test_dependency_stamp_skips_reinstallation_and_detects_changes(tmp_path, monkeypatch):
    environment, executable = prepared_environment(tmp_path)
    calls = []
    monkeypatch.setattr(start.subprocess, "run", lambda *args, **kwargs: calls.append((args, kwargs)))
    assert start.prepare_environment(tmp_path) == executable
    assert len(calls) == 1 and calls[0][1]["check"] is True
    assert start.prepare_environment(tmp_path) == executable
    assert len(calls) == 1
    (tmp_path / "requirements.txt").write_text("example==2.0\n", encoding="ascii")
    start.prepare_environment(tmp_path)
    assert len(calls) == 2
    assert (environment / "requirements.sha256").read_text().strip() == hashlib.sha256(
        (tmp_path / "requirements.txt").read_bytes()).hexdigest().upper()


def test_failed_install_keeps_previous_stamp_for_retry(tmp_path, monkeypatch):
    environment, _ = prepared_environment(tmp_path)
    stamp = environment / "requirements.sha256"
    stamp.write_text("previous", encoding="ascii")

    def fail(command, **kwargs):
        raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(start.subprocess, "run", fail)
    with pytest.raises(subprocess.CalledProcessError):
        start.prepare_environment(tmp_path)
    assert stamp.read_text() == "previous"


def test_foreign_or_incomplete_environment_has_actionable_error(tmp_path):
    (tmp_path / ".venv").mkdir()
    with pytest.raises(RuntimeError, match="Rename it"):
        start.prepare_environment(tmp_path)


@pytest.mark.parametrize("platform,commands,expected", [
    ("win32", {"chrome": "chrome-path", "msedge": "edge-path"}, ("edge", "edge-path")),
    ("darwin", {"google-chrome": "chrome-path", "msedge": "edge-path"}, ("chrome", "chrome-path")),
    ("linux", {"chromium": "chromium-path"}, ("chrome", "chromium-path")),
    ("linux", {"microsoft-edge-stable": "edge-path"}, ("edge", "edge-path")),
])
def test_browser_discovery_respects_each_platform(platform, commands, expected, monkeypatch):
    monkeypatch.setattr("app.pilot_browser.sys.platform", platform)
    monkeypatch.setattr("app.pilot_browser.shutil.which", commands.get)
    monkeypatch.setattr(Path, "is_file", lambda path: False)
    assert installed_browser() == expected


def test_explicit_browser_choice_is_preserved(monkeypatch):
    monkeypatch.setattr("app.pilot_browser.shutil.which", lambda command: "/chrome" if command == "google-chrome" else None)
    monkeypatch.setattr(Path, "is_file", lambda path: False)
    assert installed_browser("edge") == ("edge", None)


@pytest.mark.parametrize("platform,variable,relative", [
    ("win32", "PROGRAMFILES(X86)", "Microsoft/Edge/Application/msedge.exe"),
    ("darwin", None, "Google Chrome.app/Contents/MacOS/Google Chrome"),
])
def test_browser_discovery_checks_native_application_directories(platform, variable, relative, monkeypatch, tmp_path):
    monkeypatch.setattr("app.pilot_browser.sys.platform", platform)
    monkeypatch.setattr("app.pilot_browser.shutil.which", lambda command: None)
    if variable:
        monkeypatch.setenv(variable, str(tmp_path))
        expected = tmp_path / relative
    else:
        expected = Path("/Applications") / relative
    monkeypatch.setattr(Path, "is_file", lambda path: path == expected)
    browser, executable = installed_browser()
    assert browser == ("edge" if variable else "chrome")
    assert executable == str(expected)


@pytest.mark.skipif(os.environ.get("SOCIAL_WATCH_BROWSER_SMOKE") != "1", reason="Opt-in native Chrome smoke test")
def test_native_chrome_profile_scripts_and_performance_logs(tmp_path, monkeypatch):
    from selenium import webdriver

    chrome_options = webdriver.ChromeOptions

    def headless_options():
        options = chrome_options()
        options.add_argument("--headless=new")
        return options

    monkeypatch.setattr(webdriver, "ChromeOptions", headless_options)
    browser = PilotBrowser(tmp_path / "browser-profile")
    driver = browser._new_driver("chrome")
    try:
        driver.get("data:text/html,<title>Portable smoke test</title><p>Ready</p>")
        assert driver.title == "Portable smoke test"
        assert driver.execute_script("return document.body.innerText") == "Ready"
        assert isinstance(driver.get_log("performance"), list)
        assert (tmp_path / "browser-profile" / "chrome").is_dir()
    finally:
        driver.quit()


def test_portable_launcher_starts_api_and_shuts_down_cleanly(tmp_path):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    if sys.platform == "win32":
        command = [sys.executable, str(ROOT / "start.py")]
    else:
        script = "start.command" if sys.platform == "darwin" else "start.sh"
        command = ["/bin/sh", str(ROOT / script)]
    command.extend(["--no-browser", "--port", str(port), "--data-dir", str(tmp_path / "profile with spaces")])
    with (tmp_path / "launcher.log").open("w+", encoding="utf-8") as log:
        process = subprocess.Popen(command, cwd=tmp_path, stdout=log, stderr=subprocess.STDOUT)
        url = f"http://127.0.0.1:{port}"
        try:
            deadline = time.monotonic() + 180
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    log.seek(0)
                    pytest.fail(log.read())
                try:
                    with urllib.request.urlopen(url + "/api/health", timeout=1) as response:
                        assert json.load(response)["app"] == "group-watch-pilot"
                    break
                except (urllib.error.URLError, TimeoutError):
                    time.sleep(0.2)
            else:
                log.seek(0)
                pytest.fail("Launcher did not become ready.\n" + log.read())
            with urllib.request.urlopen(url + "/api/state", timeout=2) as response:
                state = json.load(response)
            assert not state["status"]["auto_running"]
            assert not state["status"]["browser_open"]
            assert not state["settings"]["telegram_configured"]
            request = urllib.request.Request(url + "/api/shutdown", data=b"", headers={"X-App-Request": "1"})
            with urllib.request.urlopen(request, timeout=2) as response:
                assert response.status == 200
            assert process.wait(timeout=15) == 0
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=10)
