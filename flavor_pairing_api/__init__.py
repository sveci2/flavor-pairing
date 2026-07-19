"""flavor_pairing_api: read-only HTTP JSON API over flavor_pairing.query (CP9).

Deliberately a sibling of the ``flavor_pairing`` runtime package: the
pipeline stays standard-library-only and network-free, while this layer
adds Flask (see requirements-api.txt). Run locally with
``flask --app flavor_pairing_api run`` (development server only).
"""

from flavor_pairing_api.app import create_app

__all__ = ["create_app"]
