import ast
import importlib
import pathlib
import random
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


class IndexedEdge:
    def __init__(self, bbox):
        self.bbox = bbox


class IndexedMesh:
    def __init__(self):
        self.bbox = (0, 0, 0, 10, 10, 10)
        self.edges = [
            IndexedEdge((0, 0, 0, 1, 1, 1)),
            IndexedEdge((9, 9, 9, 10, 10, 10)),
        ]
        self.points = [Point(0.5, 0.5, 0.5), Point(9.5, 9.5, 9.5)]


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

    def test_spatial_index_returns_only_nearby_edges_and_vertices(self):
        spatial_index = selector.UniformMeshIndex(
            IndexedMesh(), target_edges_per_cell=0.01, max_divisions=10
        )
        query_bbox = (0, 0, 0, 2, 2, 2)

        edge_indices, _ = spatial_index.edge_indices(query_bbox, 0.0)
        vertex_indices, _ = spatial_index.vertex_indices(query_bbox, 0.0)

        self.assertEqual(edge_indices, (0,))
        self.assertEqual(vertex_indices, (0,))

    def test_spatial_index_falls_back_for_large_queries(self):
        spatial_index = selector.UniformMeshIndex(
            IndexedMesh(), target_edges_per_cell=0.01, max_divisions=10
        )
        edge_indices, _ = spatial_index.edge_indices(
            (0, 0, 0, 10, 10, 10), 0.0
        )
        self.assertIsNone(edge_indices)

    def test_spatial_index_has_no_bounding_box_false_negatives(self):
        generator = random.Random(731)
        mesh = IndexedMesh()
        mesh.edges = []
        mesh.points = []

        for _ in range(200):
            minimums = [generator.uniform(0.0, 9.5) for _ in range(3)]
            maximums = [
                min(10.0, value + generator.uniform(0.0, 0.5))
                for value in minimums
            ]
            mesh.edges.append(IndexedEdge(tuple(minimums + maximums)))
            mesh.points.append(
                Point(
                    generator.uniform(0.0, 10.0),
                    generator.uniform(0.0, 10.0),
                    generator.uniform(0.0, 10.0),
                )
            )

        spatial_index = selector.UniformMeshIndex(
            mesh, target_edges_per_cell=4, max_divisions=12
        )

        for _ in range(50):
            minimums = [generator.uniform(0.0, 8.0) for _ in range(3)]
            maximums = [
                min(10.0, value + generator.uniform(0.1, 2.0))
                for value in minimums
            ]
            query_bbox = tuple(minimums + maximums)
            padding = 0.01

            edge_indices, _ = spatial_index.edge_indices(query_bbox, padding)
            if edge_indices is not None:
                expected_edges = {
                    index
                    for index, edge in enumerate(mesh.edges)
                    if selector._bounding_boxes_overlap(
                        edge.bbox, query_bbox, padding
                    )
                }
                self.assertTrue(expected_edges.issubset(set(edge_indices)))

            vertex_indices, _ = spatial_index.vertex_indices(query_bbox, padding)
            if vertex_indices is not None:
                expected_vertices = {
                    index
                    for index, point in enumerate(mesh.points)
                    if selector._point_in_bounding_box(
                        point, query_bbox, padding
                    )
                }
                self.assertTrue(expected_vertices.issubset(set(vertex_indices)))

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
