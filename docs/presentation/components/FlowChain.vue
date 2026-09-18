<script setup>
/**
 * A horizontal chain of pipeline stages. Identity comes from position and
 * label; the accent marks only the stage the slide is talking about, and the
 * gate stage carries a glyph and a word as well as the critical tint, so its
 * state is never color-alone.
 */
defineProps({
  steps: { type: Array, required: true },
  highlight: { type: String, default: '' },
  numbered: { type: Boolean, default: false },
})
</script>

<template>
  <div class="viz-root chain">
    <template v-for="(step, i) in steps" :key="step.label ?? step">
      <div class="step" :class="{ on: (step.label ?? step) === highlight, gate: step.gate }">
        <div class="head">
          <span v-if="numbered" class="n">{{ String(i + 1).padStart(2, '0') }}</span>
          <span v-if="step.gate" class="glyph" aria-hidden="true">■</span>
          <span class="label">{{ step.label ?? step }}</span>
        </div>
        <div v-if="step.note" class="note">{{ step.note }}</div>
        <div v-if="step.gate" class="gate-tag">gate</div>
      </div>
      <div v-if="i < steps.length - 1" class="link" aria-hidden="true" />
    </template>
  </div>
</template>

<style scoped>
.chain {
  display: flex;
  flex-wrap: wrap;
  align-items: stretch;
  gap: 0;
  font-family: system-ui, -apple-system, 'Segoe UI', sans-serif;
}

.step {
  position: relative;
  flex: 1 1 0;
  min-width: 5.4rem;
  background: linear-gradient(180deg, rgba(255, 255, 255, 0.05), transparent 65%),
    var(--surface-1);
  border: 1px solid var(--panel-edge);
  border-radius: 9px;
  padding: 0.5rem 0.55rem 0.55rem;
  box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.05);
}

/* The connector: a lit rail between nodes rather than a text arrow. */
.link {
  align-self: center;
  flex: 0 0 0.85rem;
  height: 2px;
  background: linear-gradient(90deg, var(--accent-deep), var(--accent));
  opacity: 0.7;
}

.step.on {
  border-color: var(--accent);
  box-shadow: inset 0 0 0 1px var(--accent), 0 0 18px rgba(34, 211, 238, 0.25);
}

.step.gate {
  border-color: var(--status-critical);
  background: linear-gradient(180deg, rgba(240, 101, 101, 0.16), transparent 70%),
    var(--surface-1);
  box-shadow: 0 0 20px rgba(240, 101, 101, 0.22);
}

.head {
  display: flex;
  align-items: baseline;
  gap: 0.3rem;
}

.n {
  font-family: var(--mono);
  font-size: 0.56rem;
  color: var(--accent-ink);
  letter-spacing: 0.04em;
}

.glyph {
  font-size: 0.55rem;
  color: var(--status-critical);
}

.label {
  font-size: 0.71rem;
  font-weight: 620;
  color: var(--text-primary);
  line-height: 1.25;
}

.note {
  font-size: 0.6rem;
  color: var(--text-muted);
  line-height: 1.3;
  margin-top: 0.2rem;
}

.gate-tag {
  font-family: var(--mono);
  font-size: 0.5rem;
  letter-spacing: 0.14em;
  text-transform: uppercase;
  color: var(--status-critical);
  margin-top: 0.25rem;
}
</style>
