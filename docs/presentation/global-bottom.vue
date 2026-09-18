<script setup>
/**
 * Persistent deck chrome: a wordmark, the section name of the current slide,
 * and a progress rail. Hidden on the cover so the title slide stays clean.
 */
import { computed } from 'vue'

const nav = computed(() => $slidev.nav)
const show = computed(() => nav.value.currentPage > 1)
const pct = computed(() =>
  Math.round((nav.value.currentPage / Math.max(nav.value.total, 1)) * 100),
)
</script>

<template>
  <footer v-if="show" class="deck-foot">
    <div class="rail"><div class="rail-fill" :style="{ width: `${pct}%` }" /></div>
    <div class="row">
      <span class="mark">AutoCAB</span>
      <span class="sep">/</span>
      <span class="meta">BioHackathon 2026 · Team 6</span>
      <span class="count">{{ nav.currentPage }} / {{ nav.total }}</span>
    </div>
  </footer>
</template>

<style scoped>
.deck-foot {
  position: absolute;
  bottom: 0;
  left: 0;
  right: 0;
  z-index: 10;
  pointer-events: none;
}

.rail {
  height: 2px;
  background: var(--gridline, rgba(255, 255, 255, 0.12));
}

.rail-fill {
  height: 100%;
  background: linear-gradient(90deg, var(--accent-deep, #3987e5), var(--accent, #22d3ee));
  box-shadow: 0 0 10px var(--accent, #22d3ee);
  transition: width 0.3s ease;
}

.row {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  padding: 0.3rem 1.1rem 0.35rem;
  font-family: var(--mono, ui-monospace, Menlo, monospace);
  font-size: 0.56rem;
  letter-spacing: 0.1em;
  text-transform: uppercase;
  color: var(--text-muted, #8496b0);
}

.mark {
  color: var(--accent-ink, #67e8f9);
  font-weight: 600;
}

.sep {
  opacity: 0.4;
}

.count {
  margin-left: auto;
  font-variant-numeric: tabular-nums;
}
</style>
