import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from elevator_web_game import rules


class SetKnowledge(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(__file__).resolve().parents[1] / "plugins/chat_bot/elevator_knowledge.py"
        package = types.ModuleType("gear_guide_test")
        package.llm = types.SimpleNamespace()
        with patch.dict(sys.modules, {package.__name__: package}):
            spec = importlib.util.spec_from_file_location(package.__name__ + ".guide", path)
            cls.guide = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(cls.guide)
        cls.catalog = rules.Catalog()

    def test_every_set_slot_can_be_queried_through_the_tool(self):
        game = object.__new__(rules.ElevatorGame)
        game.catalog = self.catalog
        with patch.object(self.guide, "_get_live_elevator_game", return_value=game):
            for part in self.catalog.equipment.values():
                if not (code := part.get("gear_set")):
                    continue
                suit = self.catalog.gear_sets[code]["name"]
                with self.subTest(suit=suit, slot=part["slot"]):
                    text = self.guide.lookup_guide("equipment", f"{suit}的{part['slot']}叫什么？")
                    self.assertIn(f"{part['slot']}「{part['name']}」", text.splitlines()[1])
                    self.assertIn(suit, self.guide.lookup_guide("search", part["name"]))
            self.assertIn('挂件「潮线航标」', self.guide.lookup_guide("sets", "潮汐套的饰品"))
            self.assertIn('护甲「弧光隔热袍」', self.guide.lookup_guide("atlas", "日冕的衣服"))
            full = self.guide.lookup_guide("sets", "潮汐引航四件套有哪些")
            for name in ("引潮齿刃","潜航压力衣","潮线航标","涡轮心轴"):
                self.assertIn(name, full)
