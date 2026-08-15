# Maya Intersection Selector

A Maya Python utility that adds every polygon mesh intersecting the current mesh selection to the selection.

It detects:

- Surface crossings
- Touching surfaces
- Many coincident-face cases
- Complete containment for closed meshes

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

An optional world-space tolerance can be supplied:

```python
maya_intersection_selector.add_intersecting_geometry_to_selection(
    tolerance=1e-4
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

## License

MIT
