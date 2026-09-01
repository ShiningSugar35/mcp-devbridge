export function chatFixtureHtml(): string {
  return `<!doctype html>
<html>
<head><meta charset="utf-8"><title>Regular Chat Fixture</title></head>
<body>
  <main id="chat" data-network="online" data-selector-mode="primary">
    <section id="assistant-turns"></section>
    <button id="generation-control" data-primary="generation" hidden>Stop</button>
    <textarea id="composer" data-primary="composer"></textarea>
    <button id="final-control" data-primary="final" hidden>Copy</button>
    <div id="fallback-generation" data-fallback="generation" hidden></div>
    <div id="fallback-composer" data-fallback="composer" hidden></div>
    <div id="fallback-final" data-fallback="final" hidden></div>
  </main>
<script>
(() => {
  const root = document.querySelector('#chat');
  const turns = document.querySelector('#assistant-turns');
  const generation = document.querySelector('#generation-control');
  const composer = document.querySelector('#composer');
  const finalControl = document.querySelector('#final-control');
  const fallbackGeneration = document.querySelector('#fallback-generation');
  const fallbackComposer = document.querySelector('#fallback-composer');
  const fallbackFinal = document.querySelector('#fallback-final');
  let turn = null;
  let browserDisconnected = false;

  function ensureTurn() {
    if (!turn) {
      turn = document.createElement('article');
      turn.dataset.role = 'assistant';
      turns.appendChild(turn);
    }
    return turn;
  }
  function applySelectorMode(mode) {
    root.dataset.selectorMode = mode;
    const primaryMissing = mode === 'fallback' || mode === 'missing';
    generation.dataset.selectorMissing = String(primaryMissing);
    composer.dataset.selectorMissing = String(primaryMissing);
    finalControl.dataset.selectorMissing = String(primaryMissing);
    fallbackGeneration.dataset.selectorMissing = String(mode === 'missing');
    fallbackComposer.dataset.selectorMissing = String(mode === 'missing');
    fallbackFinal.dataset.selectorMissing = String(mode === 'missing');
  }

  window.fixture = {
    reset() {
      turns.innerHTML = '';
      turn = null;
      generation.hidden = true;
      finalControl.hidden = true;
      composer.disabled = false;
      root.dataset.network = 'online';
      browserDisconnected = false;
      applySelectorMode('primary');
    },
    delayedAssistant(text = 'thinking') {
      const node = ensureTurn();
      node.textContent = text;
      generation.hidden = false;
      composer.disabled = true;
      finalControl.hidden = true;
    },
    stabilizeThinking(text = 'thinking stable') {
      const node = ensureTurn();
      node.textContent = text;
      generation.hidden = false;
      composer.disabled = true;
      finalControl.hidden = true;
    },
    stream(text) {
      const node = ensureTurn();
      node.textContent = text;
      generation.hidden = false;
      composer.disabled = true;
      finalControl.hidden = true;
    },
    generationEnded() {
      generation.hidden = true;
      composer.disabled = false;
    },
    showFinalControls() {
      finalControl.hidden = false;
    },
    driftSelectors() { applySelectorMode('fallback'); },
    breakSelectors() { applySelectorMode('missing'); },
    offline() { root.dataset.network = 'degraded'; },
    online() { root.dataset.network = 'online'; },
    closeTab() { document.body.dataset.tabClosed = 'true'; },
    browserDisconnect() { browserDisconnected = true; },
    snapshot() {
      return {
        assistantText: turn ? turn.textContent || '' : '',
        assistantTurnCount: turns.querySelectorAll('[data-role="assistant"]').length,
        generationControlPresent: !generation.hidden,
        composerReady: !composer.disabled,
        finalControlsPresent: !finalControl.hidden,
        selectorMode: root.dataset.selectorMode,
        networkState: root.dataset.network,
        tabClosed: document.body.dataset.tabClosed === 'true',
        browserDisconnected,
      };
    },
  };
  window.fixture.reset();
})();
</script>
</body>
</html>`;
}
