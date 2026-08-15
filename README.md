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

## Shelf button

Create a Python shelf button containing:

```python
import maya_intersection_selector
maya_intersection_selector.add_intersecting_geometry_to_selection()
```

## Notes

- Complete-containment detection assumes closed, reasonably manifold geometry.
- Detailed testing begins only after a fast bounding-box check.
- Very dense scenes may take time because mesh edges and vertices are tested.
- The utility operates on polygon meshes and ignores intermediate shapes.
- If the Script Editor has focus, the most recently active visible model panel
  supplies the visibility and Isolate Select state.

## License

MIT
