import tempfile
import unittest
import re
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from elevator_web import API, create_app
from elevator_web_auth import AuthError, AuthStore
from elevator_web_feed import GameFeed
from elevator_web_game import rules


class ElevatorWebIntegration(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        base = Path(folder.name)
        self.auth = AuthStore(base / "auth.sqlite3")
        self.game_db = base / "game.sqlite3"
        self.web = TestClient(create_app(self.game_db, self.auth.path, public_origin="https://elevator.example.test"),
                              base_url="https://elevator.example.test")
        self.addCleanup(self.web.close)
        self.headers = {"Origin": "https://elevator.example.test"}
        self.qq = "1234567"
        self.group = "7654321"
        field_rng = self.enterContext(patch.object(rules.expedition, "Random")).return_value
        field_rng.choices.return_value = ["field_wrap"]
        field_rng.random.return_value = 1.0
        field_rng.randrange.return_value = 0

    def post(self, route, payload, **kw):
        return self.web.post(API + route, json=payload, headers=kw.pop("headers", self.headers), **kw)

    def test_maintenance_page_and_game_requests_explain_the_pause(self):
        self.bind()
        engine=rules.ElevatorGame(self.game_db)
        with engine.store.transaction() as db:
            db.execute("INSERT INTO elevator_maintenance VALUES(1,?)",(1795520000,))
        page=self.web.get("/elevator/")
        self.assertEqual(page.status_code,503)
        self.assertIn("维护中",page.text)
        response=self.web.get(API+"/game/state")
        self.assertEqual(response.status_code,503)
        self.assertEqual(response.json()["error"]["code"],"maintenance")
        self.assertEqual(self.web.get(API+"/health").status_code,200)

    def test_versioned_assets_and_private_state_cache_policy(self):
        html = self.web.get("/elevator/")
        versioned = re.search(r'src="(/elevator/static/app\.js\?v=[0-9a-f]+)"', html.text).group(1)
        cached = self.web.get(versioned, headers={"Accept-Encoding":"gzip"})
        self.assertIn("immutable", cached.headers["Cache-Control"])
        self.assertEqual(cached.headers["Content-Encoding"], "gzip")
        self.assertNotIn("immutable",self.web.get("/elevator/static/app.js?v=stale").headers["Cache-Control"])
        self.assertEqual(html.headers["Cache-Control"],"no-store")
        self.assertFalse(self.web.get(API+"/maintenance").json()["paused"])

    def test_public_rtc_offer_requires_the_bound_trial_account(self):
        trial = TestClient(
            create_app(self.game_db, self.auth.path, public_origin="https://elevator.example.test",
                       rtc_port=8093, rtc_player=self.qq),
            base_url="https://elevator.example.test",
        )
        self.addCleanup(trial.close)
        route = API+"/rtc/offer"
        offer = {"type":"offer","sdp":"test"}
        self.assertEqual(trial.post(route,json=offer,headers=self.headers).status_code,401)
        self.assertIn("/static/rtc.js?v=",trial.get("/elevator/").text)
        for qq, name in (("2345678","other"),(self.qq,"operator")):
            account = self.auth.register(name,"sample-password!",qq)
            self.auth.confirm(account["challenge"]["code"],qq,group_id=self.group)
            trial.cookies.set("elevator_session",account["session"])
            view = trial.get(API+"/auth/session").json()["account"]
            self.assertEqual(view.get("rtc_trial",False),qq == self.qq)
            with patch("elevator_web.http.client.HTTPConnection") as local:
                response = local.return_value.getresponse.return_value
                response.status = 200
                response.read.return_value = b'{"type":"answer","sdp":"test-answer"}'
                code = trial.post(route,json=offer,headers=self.headers)
                if qq == self.qq:
                    self.assertEqual(code.json(),{"type":"answer","sdp":"test-answer"})
                    body = local.return_value.request.call_args.args[2]
                    self.assertIn(account["session"],body.decode())
                else:
                    self.assertEqual(code.status_code,403)
                    local.assert_not_called()
            self.assertEqual(trial.post(route,json=offer,headers={"Origin":"https://elsewhere.example.test"})
                             .status_code,403)

    def test_rtc_public_accepts_all_bound_accounts(self):
        site = "https://elevator.example.test"
        public = TestClient(
            create_app(self.game_db, self.auth.path, public_origin=site, rtc_port=8093, rtc_public=True),
            base_url=site,
        )
        self.addCleanup(public.close)
        route = API+"/rtc/offer"
        offer = {"type":"offer", "sdp":"test"}
        self.assertEqual(public.post(route, json=offer, headers=self.headers).status_code, 401)
        with patch("elevator_web.http.client.HTTPConnection") as local:
            response = local.return_value.getresponse.return_value
            response.status = 200
            response.read.return_value = b'{"type":"answer","sdp":"test-answer"}'
            for qq, name in ((self.qq,"operator"),("2345678","another")):
                account = self.auth.register(name, "sample-password!", qq)
                public.cookies.set("elevator_session", account["session"])
                self.assertNotIn("rtc_trial", public.get(API+"/auth/session").json()["account"])
                self.assertEqual(public.post(route, json=offer, headers=self.headers).status_code, 403)
                self.auth.confirm(account["challenge"]["code"], qq, group_id=self.group)
                self.assertTrue(public.get(API+"/auth/session").json()["account"]["rtc_trial"])
                self.assertEqual(public.post(route, json=offer, headers=self.headers).status_code, 200)
        self.assertEqual(local.return_value.request.call_count, 2)

    def test_client_delta_rebuilds_full_state_and_stale_commands_resync(self):
        self.bind()
        self.post("/game/command",{"command":"start","params":{"map_id":"mall"},
                  "revision":self.web.get(API+"/game/state").json()["revision"]})
        game=rules.ElevatorGame(self.game_db)
        with game.store.transaction() as db:
            db.executemany("INSERT INTO inventory(user_id,item_id,quality,value,quantity) VALUES(?,'snack','',?,1)",
                           [(self.qq,i) for i in range(1,222)])
        headers={**self.headers,"X-Elevator-Sync":"1"}
        first=self.web.get(API+"/game/state",headers=headers).json()
        initial=first["state"]
        headers["X-Elevator-Base"]=first["sync_token"]
        arg={"command":"down","params":{},"revision":initial["revision"]}
        response=self.post("/game/command",arg,headers=headers)
        self.assertEqual(response.status_code,200,response.text[:200])
        packet=response.json()
        delta=packet["delta"]
        self.assertNotIn("state",packet)
        self.assertNotIn("warehouse",delta["fields"])
        rebuilt={**initial,**delta["fields"]}
        for key,values in delta["sections"].items():
            rebuilt[key]={**rebuilt[key],**values}
        current=self.web.get(API+"/game/state")
        self.assertEqual(rebuilt,current.json())
        self.assertLess(len(response.content),len(current.content)//3)
        stale=self.post("/game/command",arg,headers=headers)
        self.assertEqual(stale.status_code,409)
        self.assertEqual(stale.json()["state"]["run"]["floor"],2)
        missed=self.post("/game/command",{"command":"rush_settings","params":{"floors":5,"skip_valuable":True},
                         "revision":rebuilt["revision"]}, headers={**headers,"X-Elevator-Base":"expired-token"})
        self.assertIn("state",missed.json())

    def test_rush_settings_are_shared_and_older_requests_preserve_the_new_choices(self):
        self.bind()
        state = self.web.get(API+"/game/state").json()
        self.assertEqual(state["player"]["rush_event_mode"], "pause")
        self.assertIs(state["player"]["rush_auto_elite"], False)

        def settings(state, **options):
            return self.post("/game/command", {
                "command": "rush_settings", "params": {"floors": 5, "skip_valuable": True, **options},
                "revision": state["revision"],
            })

        changed = settings(state, event_mode="random", auto_elite=True)
        self.assertEqual(changed.status_code, 200)
        changed = changed.json()["state"]
        self.assertEqual(changed["player"]["rush_event_mode"], "random")
        self.assertIs(changed["player"]["rush_auto_elite"], True)
        self.assertNotEqual(changed["revision"], state["revision"])

        old_client = settings(changed, floors=3).json()["state"]
        self.assertEqual(old_client["player"]["rush_floors"], 3)
        self.assertEqual(old_client["player"]["rush_event_mode"], "random")
        self.assertIs(old_client["player"]["rush_auto_elite"], True)
        one_setting = self.post("/game/command", {
            "command": "rush_settings", "params": {"event_mode": "pause"},
            "revision": old_client["revision"],
        }).json()["state"]
        self.assertEqual(one_setting["player"]["rush_floors"], 3)
        self.assertIs(one_setting["player"]["rush_skip_valuable"], True)
        self.assertIs(one_setting["player"]["rush_auto_elite"], True)
        for invalid in ({"event_mode": ["ignore"]}, {"event_mode": "bogus"}, {"auto_elite": 1}):
            self.assertEqual(settings(one_setting, **invalid).status_code, 422)

        game = rules.ElevatorGame(self.game_db)
        game.execute(self.qq, "网页乘客", self.group, "rush_settings", "无视奇遇 遇精英暂停")
        stale = settings(one_setting)
        self.assertEqual(stale.status_code, 409)
        restored = stale.json()["state"]["player"]
        self.assertEqual(restored["rush_event_mode"], "ignore")
        self.assertIs(restored["rush_auto_elite"], False)

    def test_rush_response_keeps_skipped_event_receipts_for_each_floor(self):
        self.bind()

        def command(name, **params):
            revision = self.web.get(API+"/game/state").json()["revision"]
            response = self.post("/game/command", {"command": name, "params": params, "revision": revision})
            self.assertEqual(response.status_code, 200)
            return response.json()

        command("start", map_id="mall")
        command("rush_settings", floors=3, skip_valuable=True, event_mode="ignore", auto_elite=True)
        with patch.object(rules.Turn, "_event_roll", return_value="choice"):
            receipt = command("rush")["result"]["rush"]["floors"]
        self.assertEqual([entry["floor"] for entry in receipt], [2, 3, 4])
        for entry in receipt:
            self.assertEqual(entry["adventure"]["outcome"], "ignored")
            self.assertTrue(entry["adventure"]["name"])
            self.assertEqual(entry["adventure"]["loot"], [])

    def test_lost_reply_crosses_qq_and_web_without_spending_twice(self):
        self.bind()
        headers={**self.headers,"X-Elevator-Sync":"1"}
        first=self.web.get(API+"/game/state",headers=headers).json()
        buy={"command":"buy","params":{"id":"coffee","quantity":1},"revision":first["state"]["revision"]}
        # Treat the first response as lost at the client after the database commit.
        self.assertEqual(self.post("/game/command",buy,headers={**headers,"X-Elevator-Base":first["sync_token"]}).status_code,200)
        stale=self.post("/game/command",buy,headers={**headers,"X-Elevator-Base":first["sync_token"]})
        self.assertEqual(stale.status_code,409)
        restored=stale.json()["state"]
        self.assertEqual(restored["player"]["gold"],200)
        self.assertEqual(sum(i["quantity"] for i in restored["warehouse"] if i["id"]=="coffee"),1)
        game=rules.ElevatorGame(self.game_db)
        game.execute(self.qq,"网页乘客",self.group,"rename","__keep_lobby__")
        with patch.object(rules.Turn,"_loot",return_value=[{"id":"snack","quality":"","value":10}]):
            game.execute(self.qq,"网页乘客",self.group,"start","商场 | 冰美式 1")
        out_of_date=self.post("/game/command",{**buy,"revision":restored["revision"]},headers=headers)
        self.assertEqual(out_of_date.status_code,409)
        current=out_of_date.json()["state"]
        coffee=next(i for i in current["run"]["bag"] if i["id"]=="coffee")
        saved=self.post("/game/command",{"command":"save_box","params":{"index":coffee["index"]},"revision":current["revision"]},
                        headers={**headers,"X-Elevator-Base":out_of_date.json()["sync_token"]})
        self.assertEqual(saved.status_code,200,saved.text[:180])
        # A new engine process resolves the same run from disk and settles via QQ.
        restored_game=rules.ElevatorGame(self.game_db)
        restored_game.execute(self.qq,"网页乘客",self.group,"extract")
        final=self.web.get(API+"/game/state").json()
        self.assertIsNone(final["run"])
        self.assertEqual(final["player"]["gold"],200)
        self.assertEqual(final["player"]["tickets"],19)
        self.assertEqual(sum(i["quantity"] for i in final["warehouse"] if i["id"]=="coffee"),1)

    def register(self, name="webuser", qq=None):
        return self.post("/auth/register", {"username":name,"password":"test-pass!","qq_id":qq or self.qq})

    def bind(self):
        response = self.register()
        self.assertEqual(response.status_code,201)
        proof = response.json()["challenge"]["code"]
        self.auth.confirm(proof,self.qq,group_id=self.group,nickname="网页乘客")
        rules.ElevatorGame(self.game_db).execute(self.qq,"网页乘客",self.group,"pursue","1 8")

    def prepare_elite_encounter(self, *, powerful=False):
        self.bind()
        engine = rules.ElevatorGame(self.game_db)
        engine.execute(self.qq, "网页乘客", self.group, "rename", "__keep_start__")
        with engine.store.transaction() as db:
            now = db.execute(
                "SELECT updated_at FROM active_runs WHERE user_id=?", (self.qq,),
            ).fetchone()[0]
            turn = rules.Turn(engine, db, self.qq, "网页乘客", self.group, now)
            enemy = rules.combat.monster(engine.catalog.maps["mall"], 4, engine.rng, elite=True)
            enemy["name"] = "测试精英怪"
            turn.run.update(floor=4, event={"kind": "combat", "enemy": enemy})
            if powerful:
                turn.run["bag"].append(engine.catalog.ordinary_item("flare", engine.rng))
            turn.persist()
        state = self.web.get(API + "/game/state").json()
        payload = {"command": "flare", "revision": state["revision"]}
        if powerful:
            flare = next(item for item in state["run"]["bag"] if item["id"] == "flare")
            payload.update(command="use", params={"index": flare["index"]})
        return engine, payload

    def test_public_http_contract_and_origin_boundary(self):
        self.assertEqual(self.web.get(API+"/health").json()["status"],"ok")
        self.assertEqual(self.web.get(API+"/auth/session").json(),{"account":None})
        invalid = self.post("/auth/login", {"username":"webuser","password":"test-pass!"},headers={"Origin":"https://other.example.test"})
        self.assertEqual(invalid.status_code,403)
        self.assertEqual(self.web.get(API+"/game/state").status_code,401)
        response = self.register()
        self.assertEqual(response.status_code,201)
        self.assertIn("HttpOnly",response.headers["set-cookie"])
        self.assertIn("Secure",response.headers["set-cookie"])
        self.assertIn("SameSite=strict",response.headers["set-cookie"])
        self.assertNotIn("session",response.json())
        proof = response.json()["challenge"]["code"]
        self.assertEqual(self.web.get(API+"/game/state").status_code,403)
        with self.assertRaises(AuthError):
            self.auth.confirm(proof,"2345678",group_id=self.group)
        self.auth.confirm(proof,self.qq,group_id=self.group,nickname="网页乘客")
        rules.ElevatorGame(self.game_db).execute(self.qq,"网页乘客",self.group,"pursue","1 8")
        self.assertEqual(self.web.get(API+"/auth/session").json(),{"account":{"username":"webuser","bound":True}})
        state = self.web.get(API+"/game/state").json()
        self.assertEqual(self.post("/game/action",{"action":"delete","revision":state["revision"]}).status_code,422)
        self.assertEqual(self.post("/game/action",{"action":"start","revision":state["revision"],"uid":"2345678"}).status_code,422)

    def test_browser_and_qq_share_state_and_web_replays_are_stale(self):
        self.bind()
        old = self.web.get(API+"/game/state").json()
        named = self.post("/game/action",{"action":"rename","argument":"__keep_lobby__","revision":old["revision"]})
        self.assertEqual(named.status_code,200)
        old = named.json()["state"]
        start = self.post("/game/action",{"action":"start","argument":"mall","revision":old["revision"]})
        self.assertEqual(start.status_code,200)
        qq_game = rules.ElevatorGame(self.game_db)
        self.assertTrue(qq_game.has_run(self.qq))
        repeat = self.post("/game/action",{"action":"start","argument":"mall","revision":old["revision"]})
        self.assertEqual(repeat.status_code,409)
        qq_game.execute(self.qq,"网页乘客",self.group,"down")
        self.assertEqual(self.web.get(API+"/game/state").json()["run"]["floor"],2)

    def test_browser_command_exposes_floor_pickups_without_a_chat_command(self):
        self.bind()
        state = self.web.get(API+"/game/state").json()
        request = {"command":"start","params":{"map_id":"mall","nickname":"网页探员"},"revision":state["revision"]}
        started = self.post("/game/command", request)
        self.assertEqual(started.status_code, 200)
        body = started.json()
        self.assertEqual(body["state"]["run"]["floor"], 1)
        self.assertTrue(body["state"]["run"]["floor_result"]["loot"])
        self.assertTrue(body["state"]["run"]["floor_result"]["field"])
        self.assertEqual(self.post("/game/command", request).status_code, 409)
        bad = {"command":"down","params":{"argument":"随意指令"},"revision":body["state"]["revision"]}
        self.assertEqual(self.post("/game/command", bad).status_code, 422)

    def test_password_change_and_reset_revoke_old_sessions(self):
        self.bind()
        change = self.post("/auth/change",{"current_password":"test-pass!","new_password":"new-pass!1"})
        self.auth.confirm(change.json()["challenge"]["code"],self.qq,group_id=self.group)
        self.assertIsNone(self.web.get(API+"/auth/session").json()["account"])
        self.assertEqual(self.post("/auth/login",{"username":"webuser","password":"new-pass!1"}).status_code,200)
        reset = self.post("/auth/reset",{"username":"webuser","new_password":"reset-pass!2"})
        self.auth.confirm(reset.json()["challenge"]["code"],self.qq,group_id=self.group)
        self.assertIsNone(self.web.get(API+"/auth/session").json()["account"])
        self.assertEqual(self.post("/auth/login",{"username":"webuser","password":"reset-pass!2"}).status_code,200)

    def test_bound_qq_login_and_remembered_cookie(self):
        self.bind()
        self.post("/auth/logout", {})
        login = self.post("/auth/login", {"username":self.qq,"password":"test-pass!","remember":True})
        self.assertEqual(login.status_code, 200)
        self.assertEqual(login.json()["account"], {"username":"webuser","bound":True})
        self.assertIn("Max-Age=34560000",login.headers["set-cookie"])
        remembered = self.web.cookies.get("elevator_session")
        revisit = self.web.get(API+"/auth/session")
        self.assertEqual(revisit.json()["account"]["username"],"webuser")
        self.assertIn("Max-Age=34560000",revisit.headers["set-cookie"])
        account = self.post("/auth/login", {"username":"WEBUSER","password":"test-pass!","remember":False})
        self.assertEqual(account.status_code, 200)
        self.assertNotIn("Max-Age",account.headers["set-cookie"])
        reset = self.post("/auth/reset", {"username":"webuser","new_password":"new-pass!2"})
        self.auth.confirm(reset.json()["challenge"]["code"],self.qq,group_id=self.group)
        with self.assertRaises(AuthError):
            self.auth.get_session(remembered)

    def test_overlapping_numeric_account_and_qq_login(self):
        self.bind()
        self.register(name=self.qq,qq="2345678")
        credentials = {"username":self.qq,"password":"test-pass!","remember":False}
        self.assertEqual(self.post("/auth/login",credentials).json()["error"]["code"],"ambiguous_login")
        account = self.post("/auth/login",{**credentials,"login_type":"account"})
        qq = self.post("/auth/login",{**credentials,"login_type":"qq"})
        self.assertEqual(account.json()["account"],{"username":self.qq,"bound":False})
        self.assertEqual(qq.json()["account"],{"username":"webuser","bound":True})

    def test_extract_records_one_real_group_announcement(self):
        self.bind()
        engine = rules.ElevatorGame(self.game_db)
        engine.execute(self.qq,"网页乘客",self.group,"rename","__keep_start__")
        with engine.store.transaction() as db:
            now = db.execute("SELECT updated_at FROM active_runs WHERE user_id=?", (self.qq,)).fetchone()[0]
            turn = rules.Turn(engine,db,self.qq,"网页乘客",self.group,now)
            red = next(k for k,row in engine.catalog.collections.items() if row["rarity"]=="red")
            turn.run.update(floor=8,event={"kind":"ready"},combat_cleared_floor=8)
            turn.run["bag"].append(engine.catalog.ordinary_item(red,engine.rng))
            turn.persist()
        state = self.web.get(API+"/game/state").json()
        data = {"action":"extract","revision":state["revision"]}
        self.assertEqual(self.post("/game/action",data).status_code,200)
        self.assertEqual(self.post("/game/action",data).status_code,409)
        feed = GameFeed(self.game_db)
        queued = feed.claim()
        self.assertEqual(queued[1],self.group)
        self.assertIn("成功带出",queued[2])
        self.assertIn("刷新本群本月撤离纪录",queued[2])
        self.assertIsNone(feed.claim())
        feed.acknowledge(queued[0])
        self.assertIsNone(feed.claim())

    def test_repeat_rainbow_promotes_stock_without_reannouncing_unlock(self):
        self.bind()
        engine = rules.ElevatorGame(self.game_db)
        engine.execute(self.qq,"网页乘客",self.group,"rename","__keep_start__")
        key = next(iter(engine.catalog.rainbow))
        feed = GameFeed(self.game_db)
        for expected in ("解锁永久虹彩技能", "再次获得虹彩"):
            with engine.store.transaction() as db:
                before = feed.before(db, self.qq, self.group, "fight")
                t = rules.Turn(engine,db,self.qq,"网页乘客",self.group,1795470000)
                t.deposit({"id": key,"value":0,"quality":""},boss_reward=True)
                feed.after(db, self.qq, self.group, "fight", before)
            notice = feed.claim()
            self.assertIn(expected, notice[2])
            self.assertIn("网页乘客", notice[2])
            feed.acknowledge(notice[0])
        snapshot = self.web.get(API+"/game/state").json()
        item = next(i for i in snapshot["warehouse"] if i["id"] == key)
        self.assertEqual(item["quantity"], 2)
        self.assertNotIn("sell", [a.get("action") for a in item["actions"]])

    def test_web_flare_queues_one_announcement_for_the_bound_group(self):
        engine, payload = self.prepare_elite_encounter()
        with engine.store.transaction() as db:
            db.execute("""
                CREATE TABLE elevator_web_announcements(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_id TEXT NOT NULL, body TEXT NOT NULL,
                    created_at INTEGER NOT NULL, next_at INTEGER NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0
                )
            """)
        self.assertEqual(self.post("/game/command", payload).status_code, 200)
        self.assertEqual(self.post("/game/command", payload).status_code, 409)

        feed = GameFeed(self.game_db)
        queued = feed.claim()
        self.assertIsNotNone(queued)
        self.assertEqual(queued[1], self.group)
        self.assertIn("网页乘客", queued[2])
        self.assertIn("测试精英怪", queued[2])
        self.assertIn("支援", queued[2])
        feed.acknowledge(queued[0])
        self.assertIsNone(feed.claim())

    def test_web_use_of_powerful_flare_queues_a_group_announcement(self):
        _, payload = self.prepare_elite_encounter(powerful=True)
        self.assertEqual(self.post("/game/command", payload).status_code, 200)

        queued = GameFeed(self.game_db).claim()
        self.assertIsNotNone(queued)
        self.assertEqual(queued[1], self.group)
        self.assertIn("网页乘客", queued[2])
        self.assertIn("测试精英怪", queued[2])
        self.assertIn("支援", queued[2])

    def test_no_group_announcement_for_a_signal_already_resolved_in_qq(self):
        engine, payload = self.prepare_elite_encounter()
        self.assertEqual(self.post("/game/command", payload).status_code, 200)
        support = engine.execute("2345678", "测试群友", self.group, "support", self.qq)
        self.assertIn("空降", support.text)

        self.assertIsNone(GameFeed(self.game_db).claim())
