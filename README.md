# Maya Intersection Selector

A Maya Python utility that adds every polygon mesh intersecting the current mesh selection to the selection.

It detects:

- Surface crossings
- Touching surfaces
- Many coincident-face cases
- Complete containment for closed meshes

Only meshes currently drawable in the active viewport are eligible. Hidden
meshes and meshes excluded from that viewport's Isolate Select set are skipped.

The script uses Maya Python API 2.0 and does not modify scene geometry.

## Requirements

- Autodesk Maya with Python 3 support

The intersection calls use keyword arguments for optional acceleration and
tolerance settings. This avoids a Maya 2025.1 API binding error triggered when
unused face and triangle ID filters are explicitly passed as `None`.

## Install

Copy `maya_intersection_selector.py` into a directory on Maya's Python path. Maya's user scripts directory is one convenient option:

- Windows: `Documents/maya/<version>/scripts`
- macOS: `~/Library/Preferences/Autodesk/maya/<version>/scripts`
- Linux: `~/maya/<version>/scripts`

Restart Maya after copying the file, or add its directory to `sys.path` for the current session.

## Use

Select one or more polygon mesh transforms, then run this in a Python tab of Maya's Script Editor:

```python
import maya_intersection_selector

maya_intersection_selector.add_intersecting_geometry_to_selection()
```

The existing selection is preserved and intersecting mesh transforms are added to it.
Candidates must also be visible in the active model panel. The filter honors:

- Object, shape, and parent visibility
- Visibility overrides and hidden display layers
- The viewport's polygon display toggle
- The viewport's Isolate Select membership

"Visible" here means eligible to be drawn by the panel. A mesh may still be
eligible when it is outside the camera frame or visually occluded by another
object.

The search opens a floating progress window. Press **Escape** to cancel; any
intersections found before cancellation are still added to the selection.
The window shows a separate viewport-visibility scan, then resets to an
eligible-only geometry phase. Hidden and Isolate Select-excluded meshes do not
count toward the geometry progress total.

An optional world-space tolerance can be supplied:

```python
maya_intersection_selector.add_intersecting_geometry_to_selection(
    tolerance=1e-4
)
```

You can explicitly choose which model panel supplies the visibility state:

```python
maya_intersection_selector.add_intersecting_geometry_to_selection(
    panel="modelPanel4"
)
```

The last run's performance counters are available for profiling or bug reports:

```python
maya_intersection_selector.get_last_run_stats()
```

The reported values include elapsed time, visibility filtering and source
preparation time, scene, eligible, and disqualified mesh counts, mesh-pair
counts, ray and closest-point timings, spatial-index build and query timings
and savings, and cancellation state.

## Shelf button

Create a Python shelf button containing:

```python
import maya_intersection_selector
maya_intersection_selector.add_intersecting_geometry_to_selection()
```

## Notes

- Complete-containment detection assumes closed, reasonably manifold geometry.
- Detailed testing begins only after a fast bounding-box check.
- Edge rays and closest-point queries are restricted to portions of each mesh
  that can overlap the other mesh.
- Edge endpoints and bounds are built lazily and cached once per mesh during a
  run.
- Dense meshes queried repeatedly receive a lazy uniform-grid index. Target
  triangle bounds retrieve source edges near the target surface, while target
  bounds retrieve nearby vertices, without rescanning the entire dense mesh.
- Repeated closest-point tests use a lazily built mesh octree, with an automatic
  fallback to Maya's regular closest-point query if the intersector is
  unavailable.
- Small meshes and one-off dense-mesh queries keep the lower-overhead linear
  path instead of building an index that is unlikely to pay for itself. The
  default adaptive trigger is a mesh with at least 5,000 edges reaching its
  second edge query.
- Very dense scenes may take time because mesh edges and vertices are tested.
- The utility operates on polygon meshes and ignores intermediate shapes.
- If the Script Editor has focus, the most recently active visible model panel
  supplies the visibility and Isolate Select state.

## License

MIT
