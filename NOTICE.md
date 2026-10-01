# Third-party notices

`src/mcchat/builder.py` adapts the build prompt and the sandboxed JavaScript build
approach from **BuilderGPT** by CyniaAI (https://github.com/CyniaAI/BuilderGPT),
licensed under the Apache License 2.0 (https://www.apache.org/licenses/LICENSE-2.0).

Changes from the original: the prompt targets Bedrock / Education Edition with a
Bedrock block list and a fixed front orientation, and the output is placed live in
the world as `fill` / `setblock` commands instead of being exported as a schematic.

`src/mcchat/realworld.py` (`!map`) is an independent Python implementation inspired by
the approach of **Arnis** by Louis Erbkamm (https://github.com/louis-e/arnis, Apache
License 2.0): generating Minecraft builds from OpenStreetMap data. No Arnis code is
included.

Map data used by `!map` is © OpenStreetMap contributors, available under the Open
Database Licence (https://www.openstreetmap.org/copyright). Geocoding uses Nominatim and
features are downloaded from public Overpass API servers, subject to their usage policies.
