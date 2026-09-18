<script setup>
/**
 * The five-layer LLM egress gate. Each layer is owned by a different person,
 * lives in a different file, or is satisfied at a different time -- the ladder
 * shows that separation rather than a single checklist.
 */
const layers = [
  { n: 1, name: 'Install opt-in', where: 'the deid-<provider> extra', detail: 'With the SDK absent, the gate is an ImportError.', scopes: ['none', 'internal', 'external'] },
  { n: 2, name: 'Org-policy acknowledgement', where: '~/.wfrec/policy/deid-llm-<provider>.json', detail: 'Names a human, carries a BAA + zero-retention attestation, and expires.', scopes: ['external'] },
  { n: 3, name: 'Config flag', where: '[deid.llm] enabled in config.toml', detail: 'A machine-level decision, separate from the run.', scopes: ['internal', 'external'] },
  { n: 4, name: 'Per-invocation flags', where: '--llm --i-am-sending-text-offbox', detail: 'Typed by the operator, every single run.', scopes: ['internal', 'external'] },
  { n: 5, name: 'Consent event, pre-flight', where: 'session.deid.llm.consent in events.jsonl', detail: 'Written before the request, so the timeline records which model saw the session.', scopes: ['none', 'internal', 'external'] },
]
</script>

<template>
  <div class="viz-root ladder">
    <div v-for="layer in layers" :key="layer.n" class="layer">
      <div class="num">{{ layer.n }}</div>
      <div class="body">
        <div class="head">
          <span class="name">{{ layer.name }}</span>
          <code class="where">{{ layer.where }}</code>
        </div>
        <div class="detail">{{ layer.detail }}</div>
      </div>
      <div class="scopes">
        <span
          v-for="s in ['none', 'internal', 'external']"
          :key="s"
          class="scope"
          :class="{ on: layer.scopes.includes(s) }"
        >{{ s }}</span>
      </div>
    </div>
    <div class="foot">
      Required for the egress class shown. <strong>Non-short-circuiting:</strong> every layer is
      evaluated and every failure is reported at once.
    </div>
  </div>
</template>

<style scoped>
.ladder {
  font-family: system-ui, -apple-system, 'Segoe UI', sans-serif;
}

.layer {
  display: grid;
  grid-template-columns: 1.6rem 1fr auto;
  align-items: start;
  gap: 0.7rem;
  padding: 0.3rem 0;
  border-bottom: 1px solid var(--gridline);
}

.num {
  font-size: 0.78rem;
  font-weight: 600;
  color: var(--series-1);
  font-variant-numeric: tabular-nums;
  text-align: right;
}

.head {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 0.5rem;
}

.name {
  font-size: 0.8rem;
  font-weight: 600;
  color: var(--text-primary);
}

.where {
  font-size: 0.66rem;
  color: var(--text-secondary);
  background: transparent;
  white-space: nowrap;
}

.detail {
  font-size: 0.68rem;
  color: var(--text-muted);
  line-height: 1.35;
  margin-top: 0.1rem;
}

.scopes {
  display: flex;
  gap: 0.3rem;
  padding-top: 0.1rem;
}

.scope {
  font-size: 0.6rem;
  letter-spacing: 0.03em;
  padding: 0.1rem 0.35rem;
  border-radius: 4px;
  border: 1px solid var(--gridline);
  color: var(--text-muted);
  opacity: 0.45;
}

.scope.on {
  border-color: var(--series-1);
  color: var(--series-1);
  opacity: 1;
  font-weight: 600;
}

.foot {
  font-size: 0.68rem;
  color: var(--text-secondary);
  margin-top: 0.55rem;
  line-height: 1.4;
}
</style>
