# Dublin railway geometry

Extracted 25 September 2026 from © OpenStreetMap contributors, licensed under
the [Open Database License (ODbL)](https://www.openstreetmap.org/copyright).
The map displays this attribution beside the tiles.

Source: OSM `route=train` relations, discovered with this Overpass query:

```overpass
[out:json][timeout:90];
relation["route"="train"](53.12,-6.65,53.60,-5.98);
out tags;
```

The actual geometry came from the [OSM API](https://wiki.openstreetmap.org/wiki/API_v0.6)
`GET /api/0.6/relation/{id}/full` for these relation IDs:

| Line | Relation IDs |
| --- | --- |
| DART (Malahide–Greystones and Howth–Greystones) | 6979124, 6979126 |
| Northern Commuter (Dublin–Dundalk) | 5214593 |
| Maynooth / Western Commuter | 1172086 |
| Kildare / South Western Commuter | 5598606, 7782183 |

Only member ways tagged `railway=rail` were retained. A way was included if
at least one of its nodes fell within 53.1–53.75° N, 6.95–5.98° W; its full
geometry was kept, so no artificial segment was drawn at the boundary.
Repeated ways shared by services appear once, with all relevant line names.

Each OSM way was simplified with Ramer–Douglas–Peucker at **0.0001 projected
degrees** (longitude scaled by cos 53.4°; about 11 metres). Every way endpoint
and every node shared by multiple ways was kept. Verification against the
source extract found 660/660 ways and all 651 shared junction nodes retained.
Before and after simplification, each of the four named lines forms one
connected component by OSM node identity. This preserves route continuity
without inventing links between tracks. The resulting GeoJSON is 115,167 bytes.
