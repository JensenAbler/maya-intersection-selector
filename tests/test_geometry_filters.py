import ast
import importlib
import pathlib
import sys
import types
import unittest


maya = types.ModuleType("maya")
maya_api = types.ModuleType("maya.api")
open_maya = types.ModuleType("maya.api.OpenMaya")
maya_cmds = types.ModuleType("maya.cmds")
maya.api = maya_api
maya.cmds = maya_cmds
maya_api.OpenMaya = open_maya

sys.modules.setdefault("maya", maya)
sys.modules.setdefault("maya.api", maya_api)
sys.modules.setdefault("maya.api.OpenMaya", open_maya)
sys.modules.setdefault("maya.cmds", maya_cmds)

selector = importlib.import_module("maya_intersection_selector")


class Point:
    def __init__(self, x, y, z):
        self.x = x
        self.y = y
        self.z = z


class GeometryFilterTests(unittest.TestCase):
    def test_bounding_boxes_reject_separated_meshes(self):
        left = (0, 0, 0, 1, 1, 1)
        right = (2, 0, 0, 3, 1, 1)
        self.assertFalse(selector._bounding_boxes_overlap(left, right, 0.0))

    def test_bounding_boxes_honor_tolerance(self):
        left = (0, 0, 0, 1, 1, 1)
        right = (1.001, 0, 0, 2, 1, 1)
        self.assertTrue(selector._bounding_boxes_overlap(left, right, 0.01))

    def test_point_filter_honors_expanded_bounds(self):
        bbox = (0, 0, 0, 1, 1, 1)
        self.assertTrue(
            selector._point_in_bounding_box(Point(1.001, 0.5, 0.5), bbox, 0.01)
        )
        self.assertFalse(
            selector._point_in_bounding_box(Point(1.1, 0.5, 0.5), bbox, 0.01)
        )

    def test_ray_padding_scales_with_target_size(self):
        unit_bbox = (0, 0, 0, 1, 0, 0)
        large_bbox = (0, 0, 0, 100, 0, 0)
        self.assertEqual(selector._ray_bbox_padding(unit_bbox, 1e-5), 1e-5)
        self.assertEqual(selector._ray_bbox_padding(large_bbox, 1e-5), 1e-3)

    def test_intersection_calls_keep_maya_2025_compatible_signature(self):
        source_path = pathlib.Path(selector.__file__)
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"anyIntersection", "allIntersections"}
        ]

        self.assertEqual(len(calls), 2)
        for call in calls:
            self.assertEqual(len(call.args), 5)
            self.assertEqual(
                {keyword.arg for keyword in call.keywords},
                {"accelParams", "tolerance"},
            )


if __name__ == "__main__":
    unittest.main()
