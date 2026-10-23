import tempfile
import unittest
from pathlib import Path
import video_engine as app


class DecisionsDuringAnalysis(unittest.TestCase):
    def test_decisions_survive_analysis_updates(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as folder:
            project = Path(folder)
            raw = dict(status="analyzing", candidates=[dict(id="one", decision="pending")])
            app.save_json(project / "project.json", raw)
            for choice in ("accepted", "rejected", "pending", "accepted"):
                app.record_decision(project, "one", choice)
                self.assertEqual(app.load_project(project)["candidates"][0]["decision"], choice)
                self.assertEqual(app.read_json(project / "project.json"), raw)
            raw["candidates"].append(dict(id="two", decision="pending"))
            app.save_json(project / "project.json", raw)
            self.assertEqual(app.load_project(project)["candidates"][0]["decision"], "accepted")
            app.record_decision(project, "two", "rejected")
            raw["status"] = "ready"
            app.save_json(project / "project.json", raw)
            self.assertEqual([x["decision"] for x in app.load_project(project)["candidates"]],
                             ["accepted", "rejected"])
            with self.assertRaises(ValueError):
                app.record_decision(project, "missing", "accepted")
            with self.assertRaises(ValueError):
                app.record_decision(project, "one", "invalid")


if __name__ == "__main__":
    unittest.main(verbosity=2)
