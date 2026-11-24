"""Exercise the installed RTC binary with Edge and isolated game/account databases."""
import argparse
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import ExitStack, closing
from pathlib import Path

import uvicorn
from playwright.sync_api import expect, sync_playwright


def free_port(kind=socket.SOCK_STREAM):
    with closing(socket.socket(type=kind)) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--binary", type=Path, required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(args.root.resolve()))
    from elevator_web import API, SESSION_COOKIE, create_app
    from elevator_web_auth import AuthStore
    from elevator_web_game import rules

    with ExitStack() as cleanup:
        trial = Path(cleanup.enter_context(tempfile.TemporaryDirectory()))
        sock = cleanup.enter_context(closing(socket.socket()))
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        control = free_port()
        udp = free_port(socket.SOCK_DGRAM)
        game = rules.ElevatorGame(trial / "game.sqlite3")
        auth = AuthStore(trial / "auth.sqlite3")
        child = subprocess.Popen([str(args.binary.resolve()), "-udp-port", str(udp), "-control-port",
                                  str(control), "-game-port", str(port)], creationflags=subprocess.CREATE_NO_WINDOW)
        def stop_gateway():
            child.terminate()
            child.wait(timeout=5)
        cleanup.callback(stop_gateway)
        app = create_app(game.store.path, auth.path, local_http=True, rtc_port=control)
        server = uvicorn.Server(uvicorn.Config(app, access_log=False, log_level="error"))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
        thread.start()
        def stop_web():
            server.should_exit = True
            thread.join(6)
        cleanup.callback(stop_web)
        p = cleanup.enter_context(sync_playwright())
        edge = Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)")) / "Microsoft/Edge/Application/msedge.exe"
        browser = p.chromium.launch(executable_path=str(edge), headless=True,
                                    args=["--allow-loopback-in-peer-connection"])
        cleanup.callback(browser.close)
        url = f"http://127.0.0.1:{port}"
        for width, height, nr_lit in [(390, 844, 1), (1366, 900, 4)]:
            uid, group = str(10000000 + width), "32100000"
            account = auth.register(f"fixture{width}", secrets.token_urlsafe(20), uid)
            auth.confirm(account["challenge"]["code"], uid, group_id=group, nickname="测试乘客")
            game.execute(uid, "测试乘客", group, "rename", "__keep_lobby__")
            with game.store.transaction() as db:
                db.executemany("INSERT INTO inventory(user_id,item_id,quality,value,quantity) VALUES(?,'snack','',?,1)",
                               [(uid, val) for val in range(200, 450)])
                db.executemany("INSERT INTO collections VALUES(?,?,1,?)",
                               [(uid, k, int(time.time())) for k in list(game.catalog.rainbow)[:nr_lit]])
            context = browser.new_context(viewport={"width": width, "height": height},
                                          is_mobile=width < 500, has_touch=width < 500)
            try:
                context.add_cookies([{"name": SESSION_COOKIE, "value": account["session"], "url": url + "/elevator/"}])
                page = context.new_page()
                page.add_init_script("""const makeChannel = RTCPeerConnection.prototype.createDataChannel;
                    RTCPeerConnection.prototype.createDataChannel = function(...args) {
                        window.testPeer = this;
                        return window.testChannel = makeChannel.apply(this, args);
                    };""")
                web_posts = []
                page.on("request", lambda req: web_posts.append(req.url) if
                        req.method == "POST" and "/game/" in req.url else None)
                page.goto(url + "/elevator/")
                expect(page.locator("#game-transport")).to_have_text("WebRTC 直连", timeout=16000)
                network = page.evaluate("""async () => {
                    const report = await testPeer.getStats();
                    const pair = [...report.values()].find(x=>x.type==="candidate-pair" && x.state==="succeeded");
                    const remote = report.get(pair.remoteCandidateId);
                    return {transport:remote.protocol, candidate:remote.candidateType, address:remote.address};
                }""")
                assert network["transport"] == "udp" and network["candidate"] == "host", network
                page.locator('#main-nav [data-view="atlas"]').click()
                atlas = page.locator(".inv-atlas-progress").locator("..")
                expect(atlas.locator(":scope > section").last).to_contain_text("虹彩套装")
                for item in game.catalog.rainbow.values():
                    expect(atlas.locator(".inv-card-name", has_text=item["name"])).to_have_count(1)
                size = page.evaluate("""async () => {
                    const state = await request("/game/state");
                    if (state.state.warehouse.length < 250) throw Error("incomplete inventory");
                    return new TextEncoder().encode(JSON.stringify(state)).length;
                }""")
                assert size > 65536
                credits = page.evaluate("ui.state.player.gold")
                # An actual purchase commits. Drop its reply before the JS client receives the header.
                page.evaluate("""() => {
                    testChannel.onmessage = () => testChannel.close();
                    return perform({action:"buy",control:{command:"buy",params:{id:"coffee",quantity:1}}});
                }""")
                expect(page.locator("#toast")).to_contain_text("已重新读取服务器存档")
                expect(page.locator("#game-transport")).to_have_text("网页连接")
                assert not web_posts, web_posts
                saved = page.request.get(url + API + "/game/state").json()
                assert saved["player"]["gold"] == credits - game.catalog.shop["coffee"]
                assert sum(row["quantity"] for row in saved["warehouse"] if row["id"] == "coffee") == 1
                assert page.evaluate("ui.state.revision") == saved["revision"]
                print("PASS", width, json.dumps(network), "state_bytes", size, "single_purchase", flush=True)
            finally:
                context.close()


if __name__ == "__main__":
    main()
