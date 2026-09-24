# WeKnora docreader protocol snapshot

`docreader.proto` and its Python generated bindings come from Tencent/WeKnora
commit `1edcd54b43606d9079bb36650efe3f68707a79ea` (MIT, subject to the
upstream third-party notices). The proto SHA-256 is
`b6ca2def2757610620b5d451e0ffa282950ef7c5dff5ee23bcc127c040f2c6a7`.

Local changes to generated code are the package import path in
`docreader_pb2_grpc.py` and lint/type-check annotations. The protocol fields
and wire format are unchanged. Runtime dependencies are locked in the project
`pyproject.toml`; deployment does not download a proto.
