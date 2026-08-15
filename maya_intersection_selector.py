"""Select polygon meshes that intersect the current Maya mesh selection.

The public entry point is :func:`add_intersecting_geometry_to_selection`.
The implementation uses Maya Python API 2.0 and does not modify geometry.
"""

from __future__ import annotations

import maya.api.OpenMaya as om
import maya.cmds as cmds


DEFAULT_TOLERANCE = 1e-5


class MeshData:
    """Cached Maya mesh information used during intersection tests."""

    def __init__(self, shape: str) -> None:
        self.shape = shape
        self.transform = (
            cmds.listRelatives(shape, parent=True, fullPath=True) or [shape]
        )[0]

        selection = om.MSelectionList()
        selection.add(shape)
        self.dag_path = selection.getDagPath(0)
        self.mesh_fn = om.MFnMesh(self.dag_path)
        self.points = self.mesh_fn.getPoints(om.MSpace.kWorld)
        self.accel = self.mesh_fn.autoUniformGridParams()
        self.bbox = cmds.exactWorldBoundingBox(shape)


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


def _is_in_isolate_set(shape: str, panel: str) -> bool:
    """Check whether a mesh is included in a panel's Isolate Select set."""

    if not cmds.isolateSelect(panel, query=True, state=True):
        return True

    isolate_set = cmds.isolateSelect(panel, query=True, viewObjects=True)
    if not isolate_set:
        return False

    lineage = set(_dag_lineage(shape))

    # Isolate Select normally stores transforms. Test shapes and ancestors so
    # selecting a group also admits all mesh descendants beneath that group.
    for node in lineage:
        try:
            if cmds.sets(node, isMember=isolate_set):
                return True
        except RuntimeError:
            pass

    # Component isolation can store component strings rather than the object.
    members = cmds.sets(isolate_set, query=True) or []
    for member in members:
        member_nodes = cmds.ls(member, long=True, objectsOnly=True) or []
        if lineage.intersection(member_nodes):
            return True

    return False


def _is_visible_in_panel(shape: str, panel: str) -> bool:
    """Return whether Maya considers a mesh drawable in a model panel."""

    if not cmds.modelEditor(panel, query=True, polymeshes=True):
        return False
    if not _is_dag_visible(shape):
        return False
    return _is_in_isolate_set(shape, panel)


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


def _point_touches_mesh(point, target: MeshData, tolerance: float) -> bool:
    closest_point, _ = target.mesh_fn.getClosestPoint(point, om.MSpace.kWorld)
    return point.distanceTo(closest_point) <= tolerance


def _edges_hit_mesh(source: MeshData, target: MeshData, tolerance: float) -> bool:
    """Cast every source edge as a finite ray against the target mesh."""

    for edge_id in range(source.mesh_fn.numEdges):
        vertex_a, vertex_b = source.mesh_fn.getEdgeVertices(edge_id)
        point_a = source.points[vertex_a]
        point_b = source.points[vertex_b]

        direction = om.MFloatVector(
            point_b.x - point_a.x,
            point_b.y - point_a.y,
            point_b.z - point_a.z,
        )
        if direction.length() <= tolerance:
            continue

        ray_source = om.MFloatPoint(point_a.x, point_a.y, point_a.z)
        hit = target.mesh_fn.anyIntersection(
            ray_source,
            direction,
            om.MSpace.kWorld,
            1.0,
            False,
            None,
            None,
            False,
            target.accel,
            tolerance,
        )
        if hit:
            return True

    return False


def _point_inside_mesh(point, target: MeshData, tolerance: float) -> bool:
    """Use ray parity to test a point against a closed target mesh."""

    if _point_touches_mesh(point, target, tolerance):
        return True

    ray_source = om.MFloatPoint(point.x, point.y, point.z)
    ray_direction = om.MFloatVector(1.0, 0.37139, 0.52917)
    ray_direction.normalize()

    hits = target.mesh_fn.allIntersections(
        ray_source,
        ray_direction,
        om.MSpace.kWorld,
        1e10,
        False,
        None,
        None,
        False,
        target.accel,
        tolerance,
    )
    if not hits:
        return False

    unique_parameters = []
    for value in sorted(hits[1]):
        if not unique_parameters or abs(value - unique_parameters[-1]) > tolerance:
            unique_parameters.append(value)

    return bool(len(unique_parameters) % 2)


def _meshes_intersect(
    mesh_a: MeshData,
    mesh_b: MeshData,
    tolerance: float,
) -> bool:
    """Check surface crossing, touching, and closed-mesh containment."""

    if not _bounding_boxes_overlap(mesh_a.bbox, mesh_b.bbox, tolerance):
        return False

    if _edges_hit_mesh(mesh_a, mesh_b, tolerance):
        return True
    if _edges_hit_mesh(mesh_b, mesh_a, tolerance):
        return True

    # These checks also catch many coincident-face cases.
    if any(_point_touches_mesh(point, mesh_b, tolerance) for point in mesh_a.points):
        return True
    if any(_point_touches_mesh(point, mesh_a, tolerance) for point in mesh_b.points):
        return True

    # Catch complete containment, where the two surfaces never cross.
    if mesh_a.points and _point_inside_mesh(mesh_a.points[0], mesh_b, tolerance):
        return True
    if mesh_b.points and _point_inside_mesh(mesh_b.points[0], mesh_a, tolerance):
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

    source_data = [MeshData(shape) for shape in source_shapes]
    source_transforms = {mesh.transform for mesh in source_data}
    scene_shapes = cmds.ls(type="mesh", long=True, noIntermediate=True) or []
    added_transforms: set[str] = set()

    cmds.waitCursor(state=True)
    try:
        for candidate_shape in scene_shapes:
            if not _is_visible_in_panel(candidate_shape, panel):
                continue

            candidate = MeshData(candidate_shape)
            if candidate.transform in source_transforms:
                continue
            if candidate.transform in added_transforms:
                continue

            for source in source_data:
                if _meshes_intersect(source, candidate, tolerance):
                    added_transforms.add(candidate.transform)
                    break

        if added_transforms:
            cmds.select(sorted(added_transforms), add=True)
    finally:
        cmds.waitCursor(state=False)

    message = "Added {} intersecting object(s).".format(len(added_transforms))
    om.MGlobal.displayInfo(message)
    return sorted(added_transforms)


if __name__ == "__main__":
    add_intersecting_geometry_to_selection()
