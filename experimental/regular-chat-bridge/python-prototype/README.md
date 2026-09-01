# Python integration prototype (unreleased)

These files preserve the Python stdio supervisor, CLI, PySide6 UI, packaging entrypoint, runtime-preparation helper, and their focused tests developed during the v0.9.0 investigation.

They are archival experimental assets only:

- They are not imported by `src/local_dev_mcp_bridge/`.
- They are not included by the formal PyInstaller spec, build scripts, wheel entry points, desktop UI, or installer.
- They are not a supported ChatGPT continuation path.
- Their original relative imports assume the formal package layout and may require an isolated research harness before reuse.

The maintained executable experiment is `third_party/regular-chat-controller/`, restricted to offline/provider-neutral fixtures until the external capability and policy gates documented in `../研究.md` are re-evaluated.
