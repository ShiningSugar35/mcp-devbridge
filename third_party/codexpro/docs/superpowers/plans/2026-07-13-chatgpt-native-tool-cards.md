# ChatGPT-native Tool Cards Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace CodexPro's v9 all-tool widget with reliable, compact, ChatGPT-native-feeling cards for selected user-visible results.

**Architecture:** Keep MCP data and tool schemas stable. A centralized render-tool allowlist determines whether a descriptor receives the v10 widget metadata and whether its structured result is compacted for rendering. The v10 inline MCP Apps widget has a robust, bounded result-envelope extractor, host-theme adaptation, native-style cards, and local copy controls for terminal previews.

**Tech Stack:** TypeScript, MCP SDK, inline `text/html;profile=mcp-app` widget, Node smoke/stress scripts, ChatGPT Apps SDK bridge.

## Global Constraints

- Preserve all existing CodexPro tools, schemas, filesystem boundaries, redaction, and tool-mode gates.
- Do not copy ChatGPT's private Activity UI or add remote assets, analytics, storage, links, iframes, or widget-initiated MCP calls.
- Keep the widget CSP empty for network and resource domains.
- Render cards only for `open_current_workspace`, `open_workspace`, `workspace_snapshot`, `inspect_workspace`, `show_changes`, `git_status`, `handoff_to_agent`, `handoff_to_codex`, and `bash`.
- Use `ui://widget/codexpro-tool-card-v10.html`; retain v9 and v8 as legacy resources.
- The user-owned `.github/workflows/ci.yml` modification must not be staged or changed.

---

### Task 1: Restrict widget metadata to render-oriented tools

**Files:**
- Modify: `src/server.ts:121-141`, `src/server.ts:407-414`, `src/server.ts:1589-1591`, `src/server.ts:1722-1734`
- Test: `scripts/smoke.mjs:205-269`
- Test: `scripts/stress.mjs:767-795`

**Interfaces:**
- Consumes: `CodexProConfig.toolCards`, registered MCP tool names, existing `toolCardMeta()` descriptor metadata.
- Produces: `usesToolCard(config: CodexProConfig, name: string): boolean`; only listed render tools retain widget and invocation metadata.

- [ ] **Step 1: Write the failing descriptor-scope smoke assertions**

  In `scripts/smoke.mjs`, replace the `search` visual assertion with explicit rendered and raw lists:

  ```js
  const cardRenderTools = new Set([
    'open_current_workspace', 'open_workspace', 'workspace_snapshot',
    'inspect_workspace', 'show_changes', 'git_status',
    'handoff_to_agent', 'handoff_to_codex', 'bash'
  ]);
  for (const tool of cardTools.tools) {
    const meta = tool._meta ?? {};
    const hasCard = meta.ui?.resourceUri === toolCardUri && meta['openai/outputTemplate'] === toolCardUri;
    if (cardRenderTools.has(tool.name) !== hasCard) {
      throw new Error(`unexpected card metadata for ${tool.name}`);
    }
  }
  ```

  Add a `search` call assertion that `structuredContent.text` is absent in card mode, while the `inspect_workspace` call still contains its bounded card payload.

- [ ] **Step 2: Run the focused smoke script to confirm the old behavior fails**

  Run: `npm run build && node scripts/smoke.mjs`

  Expected: FAIL at the old assertion that `search` receives widget metadata or card-only structured text.

- [ ] **Step 3: Add the centralized render allowlist in `src/server.ts`**

  Replace the existing descriptor helper with the following shape:

  ```ts
  const TOOL_CARD_RENDER_TOOL_NAMES = new Set<string>([
    'open_current_workspace', 'open_workspace', 'workspace_snapshot',
    'inspect_workspace', 'show_changes', 'git_status',
    'handoff_to_agent', 'handoff_to_codex', 'bash'
  ]);

  function usesToolCard(config: CodexProConfig, name: string): boolean {
    return config.toolCards && TOOL_CARD_RENDER_TOOL_NAMES.has(name);
  }

  function descriptorOptionsForConfig(
    config: CodexProConfig,
    name: string,
    options: Record<string, unknown>
  ): Record<string, unknown> {
    if (usesToolCard(config, name)) return options;
    const meta = { ...((options._meta as Record<string, unknown> | undefined) ?? {}) };
    for (const key of OPTIONAL_TOOL_CARD_META) delete meta[key];
    return { ...options, _meta: meta };
  }
  ```

  Pass `name` from `registerCodexTool`:

  ```ts
  registerToolCompat(server, name, descriptorOptionsForConfig(config, name, options), validatedHandler);
  ```

  Keep the existing `toolCardMeta()` spreads in descriptors. They are centrally removed for non-render tools, preserving descriptor definitions and avoiding schema churn.

- [ ] **Step 4: Make card-only payload reductions follow the same allowlist**

  In the workspace-inspection handler, replace `config.toolCards` with `usesToolCard(config, 'inspect_workspace')` for card file/symbol/relationship limits. In the search handler, remove card-only text and compact-analysis branches because `search` is no longer a render tool:

  ```ts
  const cardWorkspaceAnalysis = usesToolCard(config, 'inspect_workspace');
  const fileLimit = cardWorkspaceAnalysis ? 120 : limitInt(args.max_files, 300, 1, config.analysisLimits.maxInventoryFiles);
  const symbolLimit = cardWorkspaceAnalysis ? 80 : limitInt(args.max_symbols, 500, 1, config.analysisLimits.maxSymbols);
  const relationshipLimit = cardWorkspaceAnalysis ? 120 : limitInt(args.max_relationships, 800, 1, config.analysisLimits.maxRelationships);

  // Delete the CODEXPRO_TOOL_CARDS branches in search that assign
  // structured.text or call compactSearchAnalysisForCard.
  ```

- [ ] **Step 5: Update stress coverage for the new contract**

  In `runCardStress`, assert that a large `search` result does not acquire `structuredContent.text`, then retain the `inspect_workspace` bounded inventory assertion:

  ```js
  assert(!('text' in search.structuredContent), 'raw search unexpectedly acquired card text');
  assert(inspected.structuredContent.files.length <= 120, 'workspace card file inventory was not compacted');
  ```

- [ ] **Step 6: Run focused checks**

  Run: `npm run build && node scripts/smoke.mjs && npm run stress`

  Expected: PASS; only the allowlisted tools advertise card metadata when enabled.

- [ ] **Step 7: Commit the scoped metadata change**

  ```bash
  git add src/server.ts scripts/smoke.mjs scripts/stress.mjs
  git commit -m "feat: scope tool cards to visible results"
  ```

### Task 2: Ship the v10 resource and reliable host-result bridge

**Files:**
- Modify: `src/toolCardWidget.ts:1-1100`
- Modify: `src/server.ts:151-197`
- Test: `scripts/http-smoke.mjs:458-510`

**Interfaces:**
- Consumes: `window.openai.toolOutput`, `window.openai.toolResponseMetadata`, `openai:set_globals`, `ui/notifications/tool-result`.
- Produces: v10 resource `ui://widget/codexpro-tool-card-v10.html`; `extractStructuredContent(value)` returns the first bounded `codexpro_tool` result at nested MCP envelope depth up to six.

- [ ] **Step 1: Write the failing v10 resource expectations**

  In `scripts/http-smoke.mjs`, set:

  ```js
  const toolCardUri = 'ui://widget/codexpro-tool-card-v10.html';
  const legacyToolCardUris = [
    'ui://widget/codexpro-tool-card-v9.html',
    'ui://widget/codexpro-tool-card-v8.html'
  ];
  ```

  Assert the v10 resource text contains `extractStructuredContent`,
  `ui/notifications/tool-result`, `copy-card-output`, and `Result unavailable`.
  Loop through both legacy URIs and assert that each can still be read with its requested URI preserved.

- [ ] **Step 2: Run the HTTP smoke script to confirm v9 no longer satisfies it**

  Run: `npm run build && node scripts/http-smoke.mjs`

  Expected: FAIL because v10 and both legacy resource paths are not yet registered.

- [ ] **Step 3: Version the resource URIs**

  At the top of `src/toolCardWidget.ts`, use:

  ```ts
  export const TOOL_CARD_URI = 'ui://widget/codexpro-tool-card-v10.html';
  export const TOOL_CARD_LEGACY_URIS = [
    'ui://widget/codexpro-tool-card-v9.html',
    'ui://widget/codexpro-tool-card-v8.html'
  ];
  export const TOOL_CARD_MIME_TYPE = 'text/html;profile=mcp-app';
  ```

  Keep `registerToolCardResource` registering the new URI and every legacy URI with the same v10 HTML. Preserve its existing standard and ChatGPT compatibility CSP/domain metadata.

- [ ] **Step 4: Replace the permanent skeleton bridge with a bounded extractor and fallback**

  In the widget script, use a recursive extractor that only traverses objects and arrays up to depth six:

  ```js
  function extractStructuredContent(value, depth = 0, seen = new Set()) {
    if (!value || typeof value !== 'object' || depth > 6 || seen.has(value)) return {};
    seen.add(value);
    if (value.codexpro_tool || value.codexpro_title) return value;
    const ordered = [
      value.structuredContent, value.toolOutput, value.toolResponseMetadata,
      value.mcp_tool_result, value.call_tool_result, value.result,
      value.params, value.data
    ];
    for (const candidate of ordered) {
      const found = extractStructuredContent(candidate, depth + 1, seen);
      if (found.codexpro_tool || found.codexpro_title) return found;
    }
    return {};
  }
  ```

  Render the actual card whenever the extractor finds a result. Render a static `Result unavailable` state after a short timeout only if no result has arrived. Do not animate the placeholder and do not invent result text.

- [ ] **Step 5: Wire all documented delivery paths to the same renderer**

  Keep initial globals, `openai:set_globals`, and `ui/notifications/tool-result`, but route each payload through the exact extractor. Add a `renderFromHost(value)` wrapper so the bridge never has separate extraction logic per event:

  ```js
  function renderFromHost(value) {
    const data = extractStructuredContent(value);
    if (data.codexpro_tool || data.codexpro_title) render(data);
  }
  ```

- [ ] **Step 6: Run the focused resource test**

  Run: `npm run build && node scripts/http-smoke.mjs`

  Expected: PASS; v10 and the two compatibility URIs are readable and the source contains the bridge/fallback contract.

- [ ] **Step 7: Commit the resource and bridge change**

  ```bash
  git add src/toolCardWidget.ts src/server.ts scripts/http-smoke.mjs
  git commit -m "feat: add reliable v10 ChatGPT card bridge"
  ```

### Task 3: Implement the ChatGPT-native visual system and terminal copy control

**Files:**
- Modify: `src/toolCardWidget.ts:5-1100`
- Test: `scripts/http-smoke.mjs:490-510`

**Interfaces:**
- Consumes: tagged structured tool results plus `window.openai.theme` and `openai:set_globals` globals.
- Produces: compact card renderers for workspace, analysis, changes, Git state, handoff, and terminal output; `copy-card-output` copies only the already-rendered bounded terminal text.

- [ ] **Step 1: Write the failing visual-contract source checks**

  Extend the HTTP widget assertion with the required tokens:

  ```js
  for (const required of [
    'copy-card-output', 'applyHostTheme', 'Result unavailable',
    'Connected workspace', 'Verification completed'
  ]) {
    if (!widgetText.includes(required)) throw new Error(`v10 widget missing ${required}`);
  }
  if (widgetText.includes('Waiting for tool result') || widgetText.includes('codexpro-sheen')) {
    throw new Error('v10 widget retained the permanent loading treatment');
  }
  ```

- [ ] **Step 2: Run the HTTP smoke script to confirm the old renderer fails**

  Run: `npm run build && node scripts/http-smoke.mjs`

  Expected: FAIL until the v10 renderer has removed the old loading treatment and supplied the native-card elements.

- [ ] **Step 3: Replace the v9 visual tokens with a host-adaptive neutral surface**

  Use the system font stack, transparent document background, 12px card radius, `#e5e7eb`/low-alpha borders, 10-14px padding, and no gradients, accent rail, product glyph, or box shadow heavier than `0 1px 3px rgba(15, 23, 42, .06)`. Apply host mode in the widget:

  ```js
  function applyHostTheme(globals = window.openai || {}) {
    const theme = String(globals.theme || 'light').toLowerCase();
    document.documentElement.dataset.theme = theme.includes('dark') ? 'dark' : 'light';
  }
  ```

  Define light and dark CSS variables under `:root` and `[data-theme='dark']`; never fetch a font, icon, image, or stylesheet.

- [ ] **Step 4: Render only user-decision information per result type**

  Preserve the existing result-type switch but make the visible components follow this contract:

  ```js
  // Workspace: root, tool mode, AGENTS availability, short Git state.
  // Analysis/change: three counts, affected files, optional collapsed detail.
  // Git: changed file rows and state label.
  // Handoff: plan/status paths and next action.
  // Bash: command, exit state, duration, bounded output preview.
  ```

  Use escaped text for every result field. Keep existing preview limits and collapsed `<details>` blocks for diffs and long output. Do not repeat the assistant's prose in the card.

- [ ] **Step 5: Add a safe local copy control for terminal cards**

  Keep the bounded command/output in an in-memory `copyableText` variable on render, then attach one delegated handler:

  ```js
  root.addEventListener('click', async (event) => {
    const button = event.target.closest('[data-copy-card-output]');
    if (!button || !copyableText) return;
    try {
      await navigator.clipboard.writeText(copyableText);
      button.textContent = 'Copied';
    } catch {
      button.textContent = 'Copy unavailable';
    }
  });
  ```

  The button must not invoke an MCP tool, read unrendered data, or use browser storage. Its text content must be escaped and bounded before assignment.

- [ ] **Step 6: Run focused renderer checks**

  Run: `npm run build && node scripts/http-smoke.mjs && node scripts/smoke.mjs`

  Expected: PASS; v10 resources and descriptor scope work in both HTTP and stdio paths.

- [ ] **Step 7: Commit the visual system**

  ```bash
  git add src/toolCardWidget.ts scripts/http-smoke.mjs
  git commit -m "feat: polish native ChatGPT tool cards"
  ```

### Task 4: Package-style live verification against the Web3 workspace

**Files:**
- Modify: `README.md:118-125`, `CHANGELOG.md`
- Verify: `/Users/rebel/Downloads/web3-agent-operator-v0.3.1` (no project-file changes)

**Interfaces:**
- Consumes: local npm pack artifact, existing saved CodexPro workspace profile, stable ngrok hostname and saved opaque token.
- Produces: a package-style v0.29.0-beta.1 candidate serving the Web3 workspace with v10 cards enabled.

- [ ] **Step 1: Add concise setup/reload guidance**

  In `README.md`, keep the opt-in command but explain that cards now render selected workspace, change, Git, handoff, and terminal results; after a widget update, refresh the ChatGPT plugin so it reloads the resource URI. Add an unreleased `0.29.0-beta.1` changelog entry mentioning v10 cards and the fixed stuck-placeholder behavior.

- [ ] **Step 2: Run all repository checks before packaging**

  Run: `npm run build && npm run smoke && npm run stress && git diff --check && npm pack --dry-run --json`

  Expected: PASS; the package manifest includes built source and docs without the user-owned CI modification being staged.

- [ ] **Step 3: Build and install an isolated package-style candidate**

  Run:

  ```bash
  CANDIDATE_DIR=$(mktemp -d /tmp/codexpro-card-candidate.XXXXXX)
  TARBALL=$(npm pack --json | node -e "let s='';process.stdin.on('data',d=>s+=d).on('end',()=>console.log(JSON.parse(s)[0].filename))")
  npm install --prefix "$CANDIDATE_DIR" --ignore-scripts "$(pwd)/$TARBALL"
  "$CANDIDATE_DIR/node_modules/.bin/codexpro" --version
  ```

  Expected: prints `0.29.0-beta.1`; do not use the source-linked `codexpro` on `PATH` as proof.

- [ ] **Step 4: Persist card opt-in for the existing Web3 profile without printing its token**

  Use the candidate CLI's `settings` action or `--tool-cards on` option with the existing Web3 workspace root. Do not put the saved auth token in shell arguments, output, or documentation.

- [ ] **Step 5: Restart the existing local service cleanly and verify it**

  Stop the old detached CodexPro service, start the package candidate with the Web3 root and saved profile, then verify:

  ```bash
  lsof -nP -iTCP:8787 -sTCP:LISTEN
  curl -sS -o /dev/null -w 'unauthenticated=%{http_code}\n' https://spellbind-owl-washroom.ngrok-free.dev/healthz
  ```

  Expected: one listener on `127.0.0.1:8787`, and `unauthenticated=401`. Perform the saved-token health check only inside a script that prints the HTTP code, never the token.

- [ ] **Step 6: Confirm the ChatGPT rendering loop**

  Refresh the existing ChatGPT plugin connection, then ask:

  ```text
  Use CodexPro to open the current workspace, then run the smallest safe project check and show changes.
  ```

  Expected: a compact workspace card, a native-style terminal card with copy control, and a change card only when there are changes. Raw file/search requests should not create cards.

- [ ] **Step 7: Commit documentation and report the handoff boundary**

  ```bash
  git add README.md CHANGELOG.md
  git commit -m "docs: explain native ChatGPT tool cards"
  git status --short
  ```

  Expected: only `.github/workflows/ci.yml` remains as an unrelated user modification. Do not push unless the user explicitly asks.
