# Integration provenance

- Upstream at integration: `Roins-hub/dola-pool`, commit `8fc5280a3fd48b07afdebc07e385b3bb6dc59410` (2.6.11).
- Local starting repository: `Diana-Taffy/dola-pool`, commit `d58dc8f91ee2b50b54efb407c12e239c23c61003`.
- Studio includes the local source changes available on 2026-10-31 (Asia/Shanghai); its application version is 0.9.0.

The two repositories have unrelated Git histories and incompatible runtime APIs and database schemas. The integration keeps upstream at the repository root and packages the local source as an optional independent edition. It does not merge the two runtimes into one process or migrate their databases.

Packaging changes include a separate launcher, complete dependency declarations, portable browser-test dependency resolution, and generic replacements for private deployment addresses and diagnostic identifiers. Runtime sessions, private configuration, databases, logs, captured task pages and personal deployment notes are excluded. The previous local Windows launch scripts are replaced by `run.py`.

The vendored HTTP implementation has its own source and patch record in `vendor/dola_http/SOURCE.json`. Media-source metadata and bundled font license text remain beside the corresponding assets.
