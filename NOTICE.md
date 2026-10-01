# Third-party notices

`src/mcchat/builder.py` adapts the build prompt and the sandboxed JavaScript build
approach from **BuilderGPT** by CyniaAI (https://github.com/CyniaAI/BuilderGPT),
licensed under the Apache License 2.0 (https://www.apache.org/licenses/LICENSE-2.0).

Changes from the original: the prompt targets Bedrock / Education Edition with a
Bedrock block list and a fixed front orientation, and the output is placed live in
the world as `fill` / `setblock` commands instead of being exported as a schematic.
