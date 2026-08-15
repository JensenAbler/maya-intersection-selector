"""Select polygon meshes that intersect the current Maya mesh selection.

The public entry point is :func:`add_intersecting_geometry_to_selection`.
The implementation uses Maya Python API 2.0 and does not modify geometry.
"""

from __future__ import annotations

import time

import maya.api.OpenMaya as om
import maya.cmds as cmds


DEFAULT_TOLERANCE = 1e-5
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


class IntersectionStats:
    """Counters from the most recent selector run."""

    def __init__(self, candidate_meshes: int) -> None:
        self.candidate_meshes = candidate_meshes
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
        self.containment_tests = 0
        self.containment_rays = 0
        self.cancelled = False
        self.elapsed_seconds = 0.0
        self.progress_open = False
        self._started_at = time.perf_counter()

    def finish(self) -> None:
        self.elapsed_seconds = time.perf_counter() - self._started_at

    def as_dict(self) -> dict[str, int | float | bool]:
        """Return stable, copy-safe profiling data for callers and bug reports."""

        return {
            "candidate_meshes": self.candidate_meshes,
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
            "containment_tests": self.containment_tests,
            "containment_rays": self.containment_rays,
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


def _cancel_requested(stats: IntersectionStats) -> bool:
    """Poll Maya's progress window without making every loop iteration a UI call."""

    if stats.cancelled:
        return True
    if not stats.progress_open:
        return False

    try:
        stats.cancelled = cmds.progressWindow(query=True, isCancelled=True)
    except RuntimeError:
        stats.progress_open = False
    return stats.cancelled


def _point_touches_mesh(point, target: MeshData, tolerance: float) -> bool:
    closest_point, _ = target.mesh_fn.getClosestPoint(point, om.MSpace.kWorld)
    return point.distanceTo(closest_point) <= tolerance


def _edges_hit_mesh(
    source: MeshData,
    target: MeshData,
    tolerance: float,
    stats: IntersectionStats,
) -> bool:
    """Cast every source edge as a finite ray against the target mesh."""

    bbox_padding = _ray_bbox_padding(target.bbox, tolerance)

    for edge in source.edges:
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
        hit = target.mesh_fn.anyIntersection(
            edge.ray_source,
            edge.direction,
            om.MSpace.kWorld,
            1.0,
            False,
            accelParams=target.accel,
            tolerance=tolerance,
        )
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

    stats.closest_point_tests += 1
    if _point_touches_mesh(point, target, tolerance):
        return True

    ray_source = om.MFloatPoint(point.x, point.y, point.z)
    ray_direction = om.MFloatVector(1.0, 0.37139, 0.52917)
    ray_direction.normalize()

    stats.containment_rays += 1
    hits = target.mesh_fn.allIntersections(
        ray_source,
        ray_direction,
        om.MSpace.kWorld,
        1e10,
        False,
        accelParams=target.accel,
        tolerance=tolerance,
    )
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

    for point in source.points:
        stats.vertices_considered += 1
        if stats.vertices_considered % 256 == 0 and _cancel_requested(stats):
            return False
        if not _point_in_bounding_box(point, target.bbox, tolerance):
            stats.vertex_bbox_rejections += 1
            continue

        stats.closest_point_tests += 1
        if _point_touches_mesh(point, target, tolerance):
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

    try:
        try:
            cmds.progressWindow(
                title="Select Intersecting Geometry",
                status="Preparing mesh candidates...",
                progress=0,
                maxValue=max(len(scene_shapes), 1),
                isInterruptable=True,
            )
            stats.progress_open = True
        except RuntimeError:
            # Maya allows only one progress window at a time. The search can
            # still run if another tool already owns it.
            pass

        source_data = [MeshData(shape) for shape in source_shapes]
        source_transforms = {mesh.transform for mesh in source_data}

        for index, candidate_shape in enumerate(scene_shapes, start=1):
            if _cancel_requested(stats):
                break
            if stats.progress_open:
                cmds.progressWindow(
                    edit=True,
                    progress=index,
                    status="Checking mesh {} of {}".format(
                        index, len(scene_shapes)
                    ),
                )

            candidate_transform = (
                cmds.listRelatives(
                    candidate_shape, parent=True, fullPath=True
                )
                or [candidate_shape]
            )[0]

            if candidate_transform in source_transforms:
                continue
            if candidate_transform in added_transforms:
                continue
            if not visibility.includes(candidate_shape):
                continue

            stats.visible_candidate_meshes += 1
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
        "{} closest-point test(s)."
    ).format(
        "Cancelled. " if stats.cancelled else "",
        len(added_transforms),
        stats.elapsed_seconds,
        stats.ray_tests,
        stats.edge_bbox_rejections,
        stats.closest_point_tests,
    )
    if stats.cancelled:
        om.MGlobal.displayWarning(message)
    else:
        om.MGlobal.displayInfo(message)
    return sorted(added_transforms)


if __name__ == "__main__":
    add_intersecting_geometry_to_selection()
