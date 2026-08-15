"""Select polygon meshes that intersect the current Maya mesh selection.

The public entry point is :func:`add_intersecting_geometry_to_selection`.
The implementation uses Maya Python API 2.0 and does not modify geometry.
"""

from __future__ import annotations

import math
import time

import maya.api.OpenMaya as om
import maya.cmds as cmds

try:
    from PySide6 import QtCore
except ImportError:
    try:
        from PySide2 import QtCore
    except ImportError:
        QtCore = None


DEFAULT_TOLERANCE = 1e-5
SPATIAL_INDEX_EDGE_THRESHOLD = 5000
SPATIAL_INDEX_QUERY_THRESHOLD = 2
SPATIAL_INDEX_TARGET_EDGES_PER_CELL = 32
SPATIAL_INDEX_MAX_DIVISIONS = 64
SPATIAL_INDEX_MAX_CELLS_PER_EDGE = 512
SPATIAL_INDEX_FULL_SCAN_RATIO = 0.65
LAST_RUN_STATS = None


class EdgeData:
    """Cached world-space edge data used by the narrow-phase search."""

    __slots__ = ("ray_source", "direction", "length", "bbox")

    def __init__(self, point_a, point_b) -> None:
        self.ray_source = om.MFloatPoint(point_a.x, point_a.y, point_a.z)
        self.direction = om.MFloatVector(
            point_b.x - point_a.x,
            point_b.y - point_a.y,
            point_b.z - point_a.z,
        )
        self.length = self.direction.length()
        self.bbox = (
            min(point_a.x, point_b.x),
            min(point_a.y, point_b.y),
            min(point_a.z, point_b.z),
            max(point_a.x, point_b.x),
            max(point_a.y, point_b.y),
            max(point_a.z, point_b.z),
        )


class UniformMeshIndex:
    """Uniform grid for querying mesh edges and vertices by world-space box."""

    def __init__(
        self,
        mesh,
        target_edges_per_cell=SPATIAL_INDEX_TARGET_EDGES_PER_CELL,
        max_divisions=SPATIAL_INDEX_MAX_DIVISIONS,
    ) -> None:
        self.bbox = mesh.bbox
        self.extents = (
            max(self.bbox[3] - self.bbox[0], 0.0),
            max(self.bbox[4] - self.bbox[1], 0.0),
            max(self.bbox[5] - self.bbox[2], 0.0),
        )
        edge_count = max(len(mesh.edges), 1)
        target_cells = max(
            1,
            math.ceil(edge_count / float(target_edges_per_cell)),
        )
        largest_extent = max(self.extents)

        if largest_extent <= 1e-12:
            self.divisions = (1, 1, 1)
        else:
            active_axes = [
                axis
                for axis, extent in enumerate(self.extents)
                if extent > largest_extent * 1e-6
            ]
            active_measure = 1.0
            for axis in active_axes:
                active_measure *= self.extents[axis]
            target_cell_size = (
                active_measure / float(target_cells)
            ) ** (1.0 / len(active_axes))
            divisions = [1, 1, 1]
            for axis in active_axes:
                divisions[axis] = max(
                    1,
                    min(
                        max_divisions,
                        math.ceil(self.extents[axis] / target_cell_size),
                    ),
                )
            self.divisions = tuple(divisions)

        self.cell_sizes = tuple(
            extent / divisions if divisions > 1 else max(extent, 1.0)
            for extent, divisions in zip(self.extents, self.divisions)
        )
        self.total_cells = (
            self.divisions[0] * self.divisions[1] * self.divisions[2]
        )
        self.edge_cells = {}
        self.vertex_cells = None
        self.points = mesh.points
        self.global_edge_indices = []

        for edge_index, edge in enumerate(mesh.edges):
            cell_range = self._cell_range(edge.bbox, 0.0)
            if cell_range is None:
                continue
            if self._cell_count(cell_range) > SPATIAL_INDEX_MAX_CELLS_PER_EDGE:
                self.global_edge_indices.append(edge_index)
                continue
            for cell in self._iter_cells(cell_range):
                self.edge_cells.setdefault(cell, []).append(edge_index)

    def _axis_index(self, value, axis: int) -> int:
        divisions = self.divisions[axis]
        if divisions == 1:
            return 0
        offset = value - self.bbox[axis]
        index = int(offset / self.cell_sizes[axis])
        return max(0, min(divisions - 1, index))

    def _cell_range(self, bbox, padding: float):
        expanded = (
            bbox[0] - padding,
            bbox[1] - padding,
            bbox[2] - padding,
            bbox[3] + padding,
            bbox[4] + padding,
            bbox[5] + padding,
        )
        if not _bounding_boxes_overlap(self.bbox, expanded, 0.0):
            return None

        return (
            self._axis_index(max(expanded[0], self.bbox[0]), 0),
            self._axis_index(max(expanded[1], self.bbox[1]), 1),
            self._axis_index(max(expanded[2], self.bbox[2]), 2),
            self._axis_index(min(expanded[3], self.bbox[3]), 0),
            self._axis_index(min(expanded[4], self.bbox[4]), 1),
            self._axis_index(min(expanded[5], self.bbox[5]), 2),
        )

    @staticmethod
    def _cell_count(cell_range) -> int:
        return (
            (cell_range[3] - cell_range[0] + 1)
            * (cell_range[4] - cell_range[1] + 1)
            * (cell_range[5] - cell_range[2] + 1)
        )

    @staticmethod
    def _iter_cells(cell_range):
        for x_index in range(cell_range[0], cell_range[3] + 1):
            for y_index in range(cell_range[1], cell_range[4] + 1):
                for z_index in range(cell_range[2], cell_range[5] + 1):
                    yield x_index, y_index, z_index

    def _point_cell(self, point):
        return (
            self._axis_index(point.x, 0),
            self._axis_index(point.y, 1),
            self._axis_index(point.z, 2),
        )

    def edge_indices(self, bbox, padding: float):
        """Return candidate edge indices, or None when a full scan is cheaper."""

        cell_range = self._cell_range(bbox, padding)
        if cell_range is None:
            return (), 0

        visited_cells = self._cell_count(cell_range)
        if visited_cells >= self.total_cells * SPATIAL_INDEX_FULL_SCAN_RATIO:
            return None, visited_cells

        edge_indices = set(self.global_edge_indices)
        for cell in self._iter_cells(cell_range):
            edge_indices.update(self.edge_cells.get(cell, ()))
        return tuple(edge_indices), visited_cells

    def edge_indices_for_bboxes(self, bboxes, padding: float):
        """Return edges near a collection of surface-element bounding boxes."""

        cells = set()
        for bbox in bboxes:
            cell_range = self._cell_range(bbox, padding)
            if cell_range is None:
                continue
            cells.update(self._iter_cells(cell_range))
            if len(cells) >= self.total_cells * SPATIAL_INDEX_FULL_SCAN_RATIO:
                return None, len(cells)

        edge_indices = set(self.global_edge_indices)
        for cell in cells:
            edge_indices.update(self.edge_cells.get(cell, ()))
        return tuple(edge_indices), len(cells)

    def vertex_indices(self, bbox, padding: float):
        """Return candidate vertex indices, or None when a full scan is cheaper."""

        if self.vertex_cells is None:
            self.vertex_cells = {}
            for vertex_index, point in enumerate(self.points):
                cell = self._point_cell(point)
                self.vertex_cells.setdefault(cell, []).append(vertex_index)

        cell_range = self._cell_range(bbox, padding)
        if cell_range is None:
            return (), 0

        visited_cells = self._cell_count(cell_range)
        if visited_cells >= self.total_cells * SPATIAL_INDEX_FULL_SCAN_RATIO:
            return None, visited_cells

        vertex_indices = []
        for cell in self._iter_cells(cell_range):
            vertex_indices.extend(self.vertex_cells.get(cell, ()))
        return tuple(vertex_indices), visited_cells


class IntersectionStats:
    """Counters from the most recent selector run."""

    def __init__(self, scene_meshes: int) -> None:
        self.scene_meshes = scene_meshes
        self.candidate_meshes = 0
        self.visibility_disqualified_meshes = 0
        self.visible_candidate_meshes = 0
        self.broad_phase_rejections = 0
        self.mesh_pairs_tested = 0
        self.edges_considered = 0
        self.edge_bbox_rejections = 0
        self.degenerate_edges = 0
        self.ray_tests = 0
        self.vertices_considered = 0
        self.vertex_bbox_rejections = 0
        self.closest_point_tests = 0
        self.closest_point_seconds = 0.0
        self.closest_intersectors_built = 0
        self.closest_intersector_failures = 0
        self.closest_intersector_build_seconds = 0.0
        self.containment_tests = 0
        self.containment_rays = 0
        self.containment_ray_seconds = 0.0
        self.ray_test_seconds = 0.0
        self.spatial_indexes_built = 0
        self.spatial_index_build_seconds = 0.0
        self.spatial_query_seconds = 0.0
        self.spatial_edge_queries = 0
        self.spatial_surface_queries = 0
        self.spatial_surface_boxes = 0
        self.spatial_vertex_queries = 0
        self.spatial_cells_visited = 0
        self.spatial_edges_avoided = 0
        self.spatial_vertices_avoided = 0
        self.cancelled = False
        self.elapsed_seconds = 0.0
        self.progress_open = False
        self._last_cancel_poll = 0.0
        self._started_at = time.perf_counter()

    def finish(self) -> None:
        self.elapsed_seconds = time.perf_counter() - self._started_at

    def as_dict(self) -> dict[str, int | float | bool]:
        """Return stable, copy-safe profiling data for callers and bug reports."""

        return {
            "scene_meshes": self.scene_meshes,
            "candidate_meshes": self.candidate_meshes,
            "visibility_disqualified_meshes": self.visibility_disqualified_meshes,
            "visible_candidate_meshes": self.visible_candidate_meshes,
            "broad_phase_rejections": self.broad_phase_rejections,
            "mesh_pairs_tested": self.mesh_pairs_tested,
            "edges_considered": self.edges_considered,
            "edge_bbox_rejections": self.edge_bbox_rejections,
            "degenerate_edges": self.degenerate_edges,
            "ray_tests": self.ray_tests,
            "vertices_considered": self.vertices_considered,
            "vertex_bbox_rejections": self.vertex_bbox_rejections,
            "closest_point_tests": self.closest_point_tests,
            "closest_point_seconds": self.closest_point_seconds,
            "closest_intersectors_built": self.closest_intersectors_built,
            "closest_intersector_failures": self.closest_intersector_failures,
            "closest_intersector_build_seconds": (
                self.closest_intersector_build_seconds
            ),
            "containment_tests": self.containment_tests,
            "containment_rays": self.containment_rays,
            "containment_ray_seconds": self.containment_ray_seconds,
            "ray_test_seconds": self.ray_test_seconds,
            "spatial_indexes_built": self.spatial_indexes_built,
            "spatial_index_build_seconds": self.spatial_index_build_seconds,
            "spatial_query_seconds": self.spatial_query_seconds,
            "spatial_edge_queries": self.spatial_edge_queries,
            "spatial_surface_queries": self.spatial_surface_queries,
            "spatial_surface_boxes": self.spatial_surface_boxes,
            "spatial_vertex_queries": self.spatial_vertex_queries,
            "spatial_cells_visited": self.spatial_cells_visited,
            "spatial_edges_avoided": self.spatial_edges_avoided,
            "spatial_vertices_avoided": self.spatial_vertices_avoided,
            "cancelled": self.cancelled,
            "elapsed_seconds": self.elapsed_seconds,
        }


class MeshData:
    """Cached Maya mesh information used during intersection tests."""

    def __init__(self, shape: str, transform=None, bbox=None) -> None:
        self.shape = shape
        self.transform = transform or (
            cmds.listRelatives(shape, parent=True, fullPath=True) or [shape]
        )[0]

        selection = om.MSelectionList()
        selection.add(shape)
        self.dag_path = selection.getDagPath(0)
        self.mesh_fn = om.MFnMesh(self.dag_path)
        self.points = self.mesh_fn.getPoints(om.MSpace.kWorld)
        self.accel = self.mesh_fn.autoUniformGridParams()
        self.bbox = bbox or cmds.exactWorldBoundingBox(shape)
        self._edges = None
        self._triangle_bboxes = None
        self._spatial_index = None
        self._spatial_edge_queries = 0
        self._closest_intersector = None
        self._closest_intersector_failed = False
        self._object_to_world = self.dag_path.inclusiveMatrix()

    @property
    def edges(self):
        """Build edge bounds on first use, then reuse them for every mesh pair."""

        if self._edges is None:
            self._edges = []
            for edge_id in range(self.mesh_fn.numEdges):
                vertex_a, vertex_b = self.mesh_fn.getEdgeVertices(edge_id)
                self._edges.append(
                    EdgeData(self.points[vertex_a], self.points[vertex_b])
                )
        return self._edges

    @property
    def triangle_bboxes(self):
        """Build cached world-space triangle bounds for surface-aware queries."""

        if self._triangle_bboxes is None:
            _, triangle_vertices = self.mesh_fn.getTriangles()
            self._triangle_bboxes = []
            for offset in range(0, len(triangle_vertices), 3):
                point_a = self.points[triangle_vertices[offset]]
                point_b = self.points[triangle_vertices[offset + 1]]
                point_c = self.points[triangle_vertices[offset + 2]]
                self._triangle_bboxes.append(
                    (
                        min(point_a.x, point_b.x, point_c.x),
                        min(point_a.y, point_b.y, point_c.y),
                        min(point_a.z, point_b.z, point_c.z),
                        max(point_a.x, point_b.x, point_c.x),
                        max(point_a.y, point_b.y, point_c.y),
                        max(point_a.z, point_b.z, point_c.z),
                    )
                )
        return self._triangle_bboxes

    def _ensure_spatial_index(self, stats: IntersectionStats):
        if self._spatial_index is None:
            started_at = time.perf_counter()
            self._spatial_index = UniformMeshIndex(self)
            stats.spatial_indexes_built += 1
            stats.spatial_index_build_seconds += time.perf_counter() - started_at
        return self._spatial_index

    def edge_indices_for_mesh(self, target, padding: float, stats: IntersectionStats):
        """Return dense-source edges near the target's triangulated surface."""

        self._spatial_edge_queries += 1
        if self.mesh_fn.numEdges < SPATIAL_INDEX_EDGE_THRESHOLD:
            return None
        if self._spatial_edge_queries < SPATIAL_INDEX_QUERY_THRESHOLD:
            return None

        spatial_index = self._ensure_spatial_index(stats)
        started_at = time.perf_counter()
        edge_indices, visited_cells = spatial_index.edge_indices_for_bboxes(
            target.triangle_bboxes,
            padding,
        )
        stats.spatial_edge_queries += 1
        stats.spatial_surface_queries += 1
        stats.spatial_surface_boxes += len(target.triangle_bboxes)
        stats.spatial_cells_visited += visited_cells
        stats.spatial_query_seconds += time.perf_counter() - started_at
        if edge_indices is not None:
            stats.spatial_edges_avoided += self.mesh_fn.numEdges - len(edge_indices)
        return edge_indices

    def vertex_indices_for_bbox(
        self,
        bbox,
        padding: float,
        stats: IntersectionStats,
    ):
        """Reuse an existing edge index for closest-point candidate vertices."""

        if self._spatial_index is None:
            return None

        vertex_index_was_unbuilt = self._spatial_index.vertex_cells is None
        started_at = time.perf_counter()
        vertex_indices, visited_cells = self._spatial_index.vertex_indices(
            bbox, padding
        )
        if vertex_index_was_unbuilt:
            stats.spatial_index_build_seconds += time.perf_counter() - started_at
        stats.spatial_vertex_queries += 1
        stats.spatial_cells_visited += visited_cells
        if vertex_indices is not None:
            stats.spatial_vertices_avoided += len(self.points) - len(vertex_indices)
        return vertex_indices

    def closest_point_distance(self, point, stats: IntersectionStats) -> float:
        """Use a cached octree closest-point query with an exact world distance."""

        started_at = time.perf_counter()
        if not self._closest_intersector_failed:
            try:
                if self._closest_intersector is None:
                    build_started_at = time.perf_counter()
                    self._closest_intersector = om.MMeshIntersector()
                    self._closest_intersector.create(
                        self.dag_path.node(),
                        self.dag_path.inclusiveMatrixInverse(),
                    )
                    stats.closest_intersectors_built += 1
                    stats.closest_intersector_build_seconds += (
                        time.perf_counter() - build_started_at
                    )

                point_on_mesh = self._closest_intersector.getClosestPoint(point)
                if point_on_mesh is not None:
                    object_point = point_on_mesh.point
                    closest_world = om.MPoint(
                        object_point.x,
                        object_point.y,
                        object_point.z,
                    ) * self._object_to_world
                    stats.closest_point_seconds += time.perf_counter() - started_at
                    return point.distanceTo(closest_world)
            except (AttributeError, RuntimeError, TypeError, ValueError):
                self._closest_intersector_failed = True
                stats.closest_intersector_failures += 1

        closest_point, _ = self.mesh_fn.getClosestPoint(
            point,
            om.MSpace.kWorld,
        )
        stats.closest_point_seconds += time.perf_counter() - started_at
        return point.distanceTo(closest_point)


class PanelVisibility:
    """Cache the active panel's display and Isolate Select state."""

    def __init__(self, panel: str) -> None:
        self.polymeshes = cmds.modelEditor(panel, query=True, polymeshes=True)
        self.isolate_enabled = cmds.isolateSelect(panel, query=True, state=True)
        self.isolate_nodes: set[str] = set()

        if not self.isolate_enabled:
            return

        isolate_set = cmds.isolateSelect(panel, query=True, viewObjects=True)
        if not isolate_set:
            return

        for member in cmds.sets(isolate_set, query=True) or []:
            self.isolate_nodes.update(
                cmds.ls(member, long=True, objectsOnly=True) or []
            )

    def includes(self, shape: str) -> bool:
        if not self.polymeshes or not _is_dag_visible(shape):
            return False
        if not self.isolate_enabled:
            return True
        return bool(self.isolate_nodes.intersection(_dag_lineage(shape)))


def get_last_run_stats() -> dict[str, int | float | bool]:
    """Return profiling counters from the most recent selector run."""

    return LAST_RUN_STATS.as_dict() if LAST_RUN_STATS else {}


def _selected_mesh_shapes() -> list[str]:
    """Return non-intermediate mesh shapes beneath the current selection."""

    selected = cmds.ls(selection=True, long=True, objectsOnly=True) or []
    shapes: set[str] = set()

    for node in selected:
        if cmds.nodeType(node) == "mesh":
            if not cmds.getAttr(node + ".intermediateObject"):
                shapes.add(node)
            continue

        descendants = cmds.listRelatives(
            node,
            allDescendents=True,
            type="mesh",
            fullPath=True,
            noIntermediate=True,
        ) or []
        shapes.update(descendants)

    return sorted(shapes)


def _eligible_mesh_candidates(
    scene_shapes,
    source_transforms,
    visibility: PanelVisibility,
):
    """Resolve visible, non-source candidates before geometric processing."""

    candidates = []
    disqualified_count = 0
    for candidate_shape in scene_shapes:
        candidate_transform = (
            cmds.listRelatives(
                candidate_shape,
                parent=True,
                fullPath=True,
            )
            or [candidate_shape]
        )[0]
        if candidate_transform in source_transforms:
            continue
        if not visibility.includes(candidate_shape):
            disqualified_count += 1
            continue
        candidates.append((candidate_shape, candidate_transform))

    return candidates, disqualified_count


def _active_model_panel() -> str | None:
    """Return the active model panel, even when the Script Editor has focus."""

    focused_panel = cmds.getPanel(withFocus=True)
    if focused_panel and cmds.getPanel(typeOf=focused_panel) == "modelPanel":
        return focused_panel

    model_panels = cmds.getPanel(type="modelPanel") or []
    for panel in model_panels:
        if cmds.modelEditor(panel, query=True, activeView=True):
            return panel

    visible_panels = set(cmds.getPanel(visiblePanels=True) or [])
    for panel in model_panels:
        if panel in visible_panels:
            return panel

    return None


def _dag_lineage(node: str) -> list[str]:
    """Return a DAG node followed by each of its parents."""

    lineage = []
    current = node
    while current:
        lineage.append(current)
        parents = cmds.listRelatives(current, parent=True, fullPath=True) or []
        current = parents[0] if parents else None
    return lineage


def _is_dag_visible(shape: str) -> bool:
    """Check Maya DAG, draw-override, and display-layer visibility."""

    for node in _dag_lineage(shape):
        if cmds.attributeQuery("visibility", node=node, exists=True):
            if not cmds.getAttr(node + ".visibility"):
                return False

        if cmds.attributeQuery("lodVisibility", node=node, exists=True):
            if not cmds.getAttr(node + ".lodVisibility"):
                return False

        if (
            cmds.attributeQuery("overrideEnabled", node=node, exists=True)
            and cmds.getAttr(node + ".overrideEnabled")
            and cmds.attributeQuery("overrideVisibility", node=node, exists=True)
            and not cmds.getAttr(node + ".overrideVisibility")
        ):
            return False

        display_layers = cmds.listConnections(node, type="displayLayer") or []
        for layer in set(display_layers):
            if cmds.attributeQuery("visibility", node=layer, exists=True):
                if not cmds.getAttr(layer + ".visibility"):
                    return False

    return True


def _bounding_boxes_overlap(a, b, tolerance: float) -> bool:
    """Run a fast world-space broad-phase bounding-box test."""

    return not (
        a[3] < b[0] - tolerance
        or b[3] < a[0] - tolerance
        or a[4] < b[1] - tolerance
        or b[4] < a[1] - tolerance
        or a[5] < b[2] - tolerance
        or b[5] < a[2] - tolerance
    )


def _point_in_bounding_box(point, bbox, tolerance: float) -> bool:
    """Return whether a point lies within an expanded world-space box."""

    return (
        bbox[0] - tolerance <= point.x <= bbox[3] + tolerance
        and bbox[1] - tolerance <= point.y <= bbox[4] + tolerance
        and bbox[2] - tolerance <= point.z <= bbox[5] + tolerance
    )


def _ray_bbox_padding(bbox, tolerance: float) -> float:
    """Conservatively bound Maya's edge-relative ray tolerance."""

    diagonal = (
        (bbox[3] - bbox[0]) ** 2
        + (bbox[4] - bbox[1]) ** 2
        + (bbox[5] - bbox[2]) ** 2
    ) ** 0.5
    return tolerance * max(diagonal, 1.0)


def _pump_ui_events() -> None:
    """Allow Maya to receive keyboard input during synchronous API loops."""

    if QtCore is not None:
        QtCore.QCoreApplication.processEvents()
    else:
        # Yield briefly so Escape can be delivered when Qt bindings are absent.
        cmds.pause(seconds=0.001)


def _cancel_requested(stats: IntersectionStats, force=False) -> bool:
    """Pump UI events and poll cancellation at a bounded frequency."""

    if stats.cancelled:
        return True
    if not stats.progress_open:
        return False

    now = time.perf_counter()
    if not force and now - stats._last_cancel_poll < 0.05:
        return False
    stats._last_cancel_poll = now

    try:
        _pump_ui_events()
        stats.cancelled = cmds.progressWindow(
            query=True,
            isCancelled=True,
        )
    except RuntimeError:
        stats.progress_open = False
    return stats.cancelled


def _point_touches_mesh(
    point,
    target: MeshData,
    tolerance: float,
    stats: IntersectionStats,
) -> bool:
    stats.closest_point_tests += 1
    return target.closest_point_distance(point, stats) <= tolerance


def _edges_hit_mesh(
    source: MeshData,
    target: MeshData,
    tolerance: float,
    stats: IntersectionStats,
) -> bool:
    """Cast every source edge as a finite ray against the target mesh."""

    bbox_padding = _ray_bbox_padding(target.bbox, tolerance)
    edge_indices = source.edge_indices_for_mesh(
        target, bbox_padding, stats
    )
    if edge_indices is None:
        edges = source.edges
    else:
        edges = (source.edges[index] for index in edge_indices)

    for edge in edges:
        stats.edges_considered += 1
        if stats.edges_considered % 256 == 0 and _cancel_requested(stats):
            return False

        if not _bounding_boxes_overlap(edge.bbox, target.bbox, bbox_padding):
            stats.edge_bbox_rejections += 1
            continue

        if edge.length <= tolerance:
            stats.degenerate_edges += 1
            continue

        stats.ray_tests += 1
        started_at = time.perf_counter()
        hit = target.mesh_fn.anyIntersection(
            edge.ray_source,
            edge.direction,
            om.MSpace.kWorld,
            1.0,
            False,
            accelParams=target.accel,
            tolerance=tolerance,
        )
        stats.ray_test_seconds += time.perf_counter() - started_at
        if hit:
            return True

    return False


def _point_inside_mesh(
    point,
    target: MeshData,
    tolerance: float,
    stats: IntersectionStats,
) -> bool:
    """Use ray parity to test a point against a closed target mesh."""

    stats.containment_tests += 1
    if not _point_in_bounding_box(point, target.bbox, tolerance):
        return False

    if _point_touches_mesh(point, target, tolerance, stats):
        return True

    ray_source = om.MFloatPoint(point.x, point.y, point.z)
    ray_direction = om.MFloatVector(1.0, 0.37139, 0.52917)
    ray_direction.normalize()

    stats.containment_rays += 1
    started_at = time.perf_counter()
    hits = target.mesh_fn.allIntersections(
        ray_source,
        ray_direction,
        om.MSpace.kWorld,
        1e10,
        False,
        accelParams=target.accel,
        tolerance=tolerance,
    )
    stats.containment_ray_seconds += time.perf_counter() - started_at
    if not hits:
        return False

    unique_parameters = []
    for value in sorted(hits[1]):
        if not unique_parameters or abs(value - unique_parameters[-1]) > tolerance:
            unique_parameters.append(value)

    return bool(len(unique_parameters) % 2)


def _vertices_touch_mesh(
    source: MeshData,
    target: MeshData,
    tolerance: float,
    stats: IntersectionStats,
) -> bool:
    """Check only source vertices that can reach the target bounding box."""

    vertex_indices = source.vertex_indices_for_bbox(
        target.bbox, tolerance, stats
    )
    if vertex_indices is None:
        points = source.points
    else:
        points = (source.points[index] for index in vertex_indices)

    for point in points:
        stats.vertices_considered += 1
        if stats.vertices_considered % 256 == 0 and _cancel_requested(stats):
            return False
        if not _point_in_bounding_box(point, target.bbox, tolerance):
            stats.vertex_bbox_rejections += 1
            continue

        if _point_touches_mesh(point, target, tolerance, stats):
            return True

    return False


def _meshes_intersect(
    mesh_a: MeshData,
    mesh_b: MeshData,
    tolerance: float,
    stats: IntersectionStats,
) -> bool:
    """Check surface crossing, touching, and closed-mesh containment."""

    if not _bounding_boxes_overlap(mesh_a.bbox, mesh_b.bbox, tolerance):
        return False

    # Complete containment is cheap to detect and avoids exhaustive edge scans
    # when one closed mesh sits entirely inside another.
    if mesh_a.points and _point_inside_mesh(
        mesh_a.points[0], mesh_b, tolerance, stats
    ):
        return True
    if mesh_b.points and _point_inside_mesh(
        mesh_b.points[0], mesh_a, tolerance, stats
    ):
        return True

    # Try the cheaper edge direction first, while retaining the reverse test
    # required for cases where only the other mesh's edges cross a surface.
    if mesh_a.mesh_fn.numEdges <= mesh_b.mesh_fn.numEdges:
        first_source, first_target = mesh_a, mesh_b
        second_source, second_target = mesh_b, mesh_a
    else:
        first_source, first_target = mesh_b, mesh_a
        second_source, second_target = mesh_a, mesh_b

    if _edges_hit_mesh(first_source, first_target, tolerance, stats):
        return True
    if stats.cancelled:
        return False
    if _edges_hit_mesh(second_source, second_target, tolerance, stats):
        return True
    if stats.cancelled:
        return False

    # These checks also catch many coincident-face cases.
    if _vertices_touch_mesh(mesh_a, mesh_b, tolerance, stats):
        return True
    if stats.cancelled:
        return False
    if _vertices_touch_mesh(mesh_b, mesh_a, tolerance, stats):
        return True

    return False


def add_intersecting_geometry_to_selection(
    tolerance: float = DEFAULT_TOLERANCE,
    panel: str | None = None,
) -> list[str]:
    """Add every scene mesh intersecting the selected meshes to the selection.

    Args:
        tolerance: World-space tolerance used for touching-surface tests.
        panel: Model panel whose visibility state should be honored. When
            omitted, the active or most recently active model panel is used.

    Returns:
        Long names of the mesh transforms added to the selection.

    Raises:
        ValueError: If tolerance is negative.
    """

    global LAST_RUN_STATS

    if tolerance < 0:
        raise ValueError("tolerance must be zero or greater")

    source_shapes = _selected_mesh_shapes()
    if not source_shapes:
        cmds.warning("Select at least one polygon mesh.")
        return []

    panel = panel or _active_model_panel()
    model_panels = cmds.getPanel(type="modelPanel") or []
    if not panel or panel not in model_panels:
        cmds.warning("No active Maya model panel was found.")
        return []

    scene_shapes = cmds.ls(type="mesh", long=True, noIntermediate=True) or []
    added_transforms: set[str] = set()
    stats = IntersectionStats(len(scene_shapes))
    LAST_RUN_STATS = stats
    visibility = PanelVisibility(panel)
    source_transforms = {
        (
            cmds.listRelatives(shape, parent=True, fullPath=True)
            or [shape]
        )[0]
        for shape in source_shapes
    }
    # Resolve visibility before opening the progress bar so its denominator
    # describes only meshes eligible for geometric processing.
    candidates, stats.visibility_disqualified_meshes = (
        _eligible_mesh_candidates(
            scene_shapes,
            source_transforms,
            visibility,
        )
    )

    stats.candidate_meshes = len(candidates)
    stats.visible_candidate_meshes = len(candidates)

    try:
        try:
            stats.progress_open = bool(
                cmds.progressWindow(
                    title="Select Intersecting Geometry",
                    status="Preparing visible mesh candidates...",
                    progress=0,
                    maxValue=max(len(candidates), 1),
                    isInterruptable=True,
                )
            )
        except RuntimeError:
            # The search can still run if another progress window is open.
            pass

        source_data = [MeshData(shape) for shape in source_shapes]

        for index, candidate_info in enumerate(candidates, start=1):
            if _cancel_requested(stats, force=True):
                break
            if stats.progress_open:
                cmds.progressWindow(
                    edit=True,
                    progress=index,
                    status="Checking mesh {} of {}".format(
                        index, len(candidates)
                    ),
                )
                _pump_ui_events()

            candidate_shape, candidate_transform = candidate_info

            if candidate_transform in added_transforms:
                continue

            candidate_bbox = cmds.exactWorldBoundingBox(candidate_shape)
            overlapping_sources = [
                source
                for source in source_data
                if _bounding_boxes_overlap(
                    source.bbox, candidate_bbox, tolerance
                )
            ]
            if not overlapping_sources:
                stats.broad_phase_rejections += 1
                continue

            # Build point, edge, and accelerator caches only after the cheap
            # visibility and world-bounding-box tests have passed.
            candidate = MeshData(
                candidate_shape,
                transform=candidate_transform,
                bbox=candidate_bbox,
            )

            for source in overlapping_sources:
                stats.mesh_pairs_tested += 1
                if _meshes_intersect(source, candidate, tolerance, stats):
                    added_transforms.add(candidate.transform)
                    break
                if stats.cancelled:
                    break

            if stats.cancelled:
                break

        if added_transforms:
            cmds.select(sorted(added_transforms), add=True)
    finally:
        if stats.progress_open:
            try:
                cmds.progressWindow(endProgress=True)
            except RuntimeError:
                pass
        stats.progress_open = False
        stats.finish()

    message = (
        "{}Added {} intersecting object(s) in {:.2f}s; "
        "{} ray test(s), {} edge-box rejection(s), "
        "{} closest-point test(s), {} indexed edge(s) avoided."
    ).format(
        "Cancelled. " if stats.cancelled else "",
        len(added_transforms),
        stats.elapsed_seconds,
        stats.ray_tests,
        stats.edge_bbox_rejections,
        stats.closest_point_tests,
        stats.spatial_edges_avoided,
    )
    if stats.cancelled:
        om.MGlobal.displayWarning(message)
    else:
        om.MGlobal.displayInfo(message)
    return sorted(added_transforms)


if __name__ == "__main__":
    add_intersecting_geometry_to_selection()
