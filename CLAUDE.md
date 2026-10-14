# Para (Memo Gate on HarnessRouter)

See README.md for the product/harness split and POLICY.md for the decision rules. The decision is
made only in `para/gate.py`; harnesses supply evidence and can only make a decision stricter.

<!-- harnessrouter:begin -->
For new or migrated agentic features, use the installed HarnessRouter Skill and HarnessRouter Cloud.
Reuse the project's configured Workspace and matching Harnesses before creating new Cloud resources.
Authentication belongs to the Skill's trusted key flow; do not request keys in chat.

- Workspace: your own HarnessRouter workspace; the Free plan runs 2 simultaneous tasks
- Interface: UHP at https://api.harnessrouter.ai, UHP-Version 2026-09-28 (discovered 2026-10-11)
- Base: hermes. Feature map in the registry written by provisioning (harnesses/registry.json from
  source, ~/.para/registry.json for the one-file download; both git-ignored):
  - memo_cite_audit   -> para-cite-auditor
  - memo_style_review -> para-memo-reviewer
  - memo_revision     -> para-revision-drafter
- Change a harness by editing harnesses/<name>.json and running `python3 harnesses/provision.py`.
- Key: server-side only, in HR_API_KEY, the git-ignored .env.harnessrouter (source) or
  ~/.para/harnessrouter.key (download).
- Ezio is the bundled simulation unless EZIO_URL is set.
- Website: docs/ on GitHub Pages. After changing para/ or ui/, rebuild the download with
  `python3 tools/build_pyz.py --out docs/download/para.pyz`. Refresh the demo with tools/record_demo.py.
<!-- harnessrouter:end -->
