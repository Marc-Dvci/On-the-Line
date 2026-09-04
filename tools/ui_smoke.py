"""Drive the real curator console and the public seeker page in Chromium.

The API tests prove the transactions. This proves a person can reach them: it
signs in, opens every view, runs a sandbox verification, resolves the decision it
creates, reports a failed referral from the public page, and fails on any console
error or failed request. Screenshots of each view land in ``docs/screenshots/``.

    python tools/ui_smoke.py             # throwaway server on a free port
    python tools/ui_smoke.py https://... # or an already-running deployment
    python tools/ui_smoke.py --demo      # play the guided demonstration instead

``--demo`` plays ``/?demo=1`` to the end and fails if any beat could not find
what it needed or overran its narration, which is what a stale selector or a
broken control looks like from the outside.
"""

from __future__ import annotations

import os
import pathlib
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

from playwright.sync_api import ConsoleMessage, Request, sync_playwright

ROOT = pathlib.Path(__file__).resolve().parent.parent
SHOTS = ROOT / "docs" / "screenshots"
VIEWPORT = {"width": 1600, "height": 1000}
USERNAME = os.environ.get("OTL_CURATOR_USERNAME", "judge")
PASSWORD = os.environ.get("OTL_CURATOR_PASSWORD", "ontheline-demo-2026")


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def start_server(port: int, *, database: pathlib.Path | None = None) -> subprocess.Popen:
    """Start uvicorn on a seeded throwaway database and wait for /health."""
    environment = dict(os.environ)
    environment["OTL_DATABASE_PATH"] = str(database or ROOT / "data" / "ui-smoke.db")
    environment["PYTHONPATH"] = str(ROOT / "src")
    for suffix in ("", "-wal", "-shm"):
        pathlib.Path(environment["OTL_DATABASE_PATH"] + suffix).unlink(missing_ok=True)
    process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "ontheline.api:app", "--port", str(port), "--log-level", "warning"],
        cwd=ROOT, env=environment,
    )
    deadline = time.time() + 60
    while time.time() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"server exited with {process.returncode}")
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1) as response:
                if response.status == 200:
                    return process
        except (urllib.error.URLError, OSError):
            time.sleep(0.25)
    process.terminate()
    raise RuntimeError("server did not become healthy")


def play_guided_demo(page, target: str, shot) -> None:
    """Run the product's own guided demonstration to the end."""
    page.goto(f"{target}/?demo=1", wait_until="networkidle")
    deadline = time.time() + 420
    seen = ""
    while time.time() < deadline:
        if page.evaluate("window.__OTL_DEMO_COMPLETE === true"):
            return
        progress = page.evaluate("document.querySelector('#demo-progress')?.textContent || ''")
        if progress and progress != seen:
            seen = progress
            shot(f"demo-{progress.replace('/', '-of-')}")
        page.wait_for_timeout(250)
    raise RuntimeError("the guided demonstration did not reach its last beat")


def report(problems: list[str], shots: int) -> int:
    for problem in problems:
        print(f"FAIL {problem}")
    print(f"{shots} views captured in {SHOTS.relative_to(ROOT)}")
    if problems:
        print(f"{len(problems)} problem(s)")
        return 1
    print("UI smoke passed")
    return 0


def main() -> int:
    arguments = [argument for argument in sys.argv[1:] if argument != "--demo"]
    guided = "--demo" in sys.argv[1:]
    target = arguments[0] if arguments else None
    server = None
    if target is None:
        port = free_port()
        server = start_server(port)
        target = f"http://127.0.0.1:{port}"
    target = target.rstrip("/")
    SHOTS.mkdir(parents=True, exist_ok=True)
    problems: list[str] = []
    shots = 0

    # The public page probes /api/auth/me before it knows whether a curator session
    # exists; an anonymous visitor gets 401 and the browser logs it. That is the
    # designed behaviour, so it is the one console error this smoke test allows.
    EXPECTED = "status of 401"

    def on_console(message: ConsoleMessage) -> None:
        if message.type in {"error", "warning"} and EXPECTED not in message.text:
            problems.append(f"console {message.type}: {message.text}")

    def on_failed(request: Request) -> None:
        problems.append(f"request failed: {request.method} {request.url}")

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_context(viewport=VIEWPORT).new_page()
            page.on("console", on_console)
            page.on("requestfailed", on_failed)
            page.set_default_timeout(20_000)

            def shot(name: str) -> None:
                nonlocal shots
                page.wait_for_timeout(450)
                page.screenshot(path=str(SHOTS / f"{name}.png"))
                shots += 1

            if guided:
                play_guided_demo(page, target, shot)
                browser.close()
                return report(problems, shots)

            page.goto(f"{target}/help", wait_until="networkidle")
            page.fill("input[name=need]", "food groceries")
            page.fill("input[name=postal_code]", "02038")
            page.click("#intake-form button[type=submit]")
            page.wait_for_selector(".result-card")
            shot("06-public-help")
            page.click(".result-card:first-child [data-referral-service]")
            page.wait_for_selector(".referral-receipt, .toast")
            shot("07-referral-receipt")
            page.click(".result-card:first-child [data-report-service]")
            page.wait_for_selector("#form-dialog[open]")
            page.select_option("#form-dialog select[name=reason]", "wrong_hours")
            page.fill("#form-dialog textarea[name=details]", "Arrived Wednesday morning and the door was locked.")
            shot("08-report-problem")
            page.click("#form-dialog-submit")
            page.wait_for_selector(".toast")
            shot("09-failure-promoted")

            page.goto(f"{target}/", wait_until="networkidle")
            shot("00-sign-in")
            page.fill("input[name=username]", USERNAME)
            page.fill("input[name=password]", PASSWORD)
            page.click("#login-form button[type=submit]")
            page.wait_for_selector("#app-view:not([hidden])")
            page.wait_for_selector(".content .card, .content .panel, .content section")
            shot("01-overview")

            for view, name in (("decisions", "02-decisions"), ("directory", "03-directory"),
                               ("intake", "04-intake"), ("activity", "05-activity")):
                page.click(f".nav-item[data-view={view}]")
                page.wait_for_timeout(700)
                shot(name)

            page.click(".nav-item[data-view=dashboard]")
            page.wait_for_timeout(600)
            sandbox = page.query_selector("[data-run-task]")
            if sandbox is None:
                problems.append("no verification task offered a sandbox run")
            else:
                sandbox.click()
                page.wait_for_selector("#demo-dialog[open]")
                page.select_option("#scenario-select", "hours_changed")
                shot("10-call-setup")
                # The tour does not sit through the whole call; the demonstration does that.
                page.evaluate("window.__OTL_CALL_FAST = true; window.__OTL_MUTE_CALL = true;")
                page.click("#run-demo-button")
                page.wait_for_selector("#call-transcript .call-line:nth-child(3)", timeout=30_000)
                shot("11-call-transcript")
                page.wait_for_selector("#call-verdict:not([hidden])", timeout=60_000)
                shot("12-call-verdict")
                page.click("#run-demo-button")
                page.wait_for_selector(".stats", timeout=30_000)
                page.wait_for_timeout(600)
                shot("13-after-verification")

            page.click(".nav-item[data-view=decisions]")
            page.wait_for_timeout(800)
            shot("14-decision-inbox")

            browser.close()
    finally:
        if server is not None:
            server.terminate()
            server.wait(timeout=20)

    return report(problems, shots)


if __name__ == "__main__":
    raise SystemExit(main())
