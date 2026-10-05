"""proteinCAD -- a 3D viewer and editor for protein structures.

The browser handles rendering and interaction; this package serves the app and
provides the HTTP surface that computation (structure analysis today, design
models later) hangs off.

    python -m proteincad          # serve the app and open a browser
    python -m proteincad --help   # options

The pieces:

    server.py      static file server + JSON API dispatch
    api.py         the routes; add new endpoints here
    structure.py   dependency-free PDB/mmCIF reader for server-side work
    analysis.py    example computation, and where a model call would go
    rcsb.py        fetch and cache structures from the RCSB
"""

__version__ = "0.1.0"

from .server import serve  # noqa: F401
