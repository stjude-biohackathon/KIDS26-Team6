<script setup>
/**
 * Cards naming a domain, a workflow that recurs inside it, and the skill a
 * recorded session of that workflow would produce.
 */
defineProps({
  items: { type: Array, required: true },
  columns: { type: Number, default: 3 },
})
</script>

<template>
  <div class="viz-root grid" :style="{ '--cols': columns }">
    <div v-for="item in items" :key="item.domain" class="card">
      <div class="domain">{{ item.domain }}</div>
      <div class="workflow">{{ item.workflow }}</div>
      <div class="skill"><span class="arrow" aria-hidden="true">→</span>{{ item.skill }}</div>
    </div>
  </div>
</template>

<style scoped>
.grid {
  display: grid;
  grid-template-columns: repeat(var(--cols), minmax(0, 1fr));
  gap: 0.55rem;
  font-family: system-ui, -apple-system, 'Segoe UI', sans-serif;
}

.card {
  background: linear-gradient(180deg, rgba(255, 255, 255, 0.05), transparent 60%),
    var(--surface-1);
  border: 1px solid var(--panel-edge);
  border-left: 3px solid var(--accent);
  border-radius: 9px;
  padding: 0.5rem 0.7rem 0.55rem;
  box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.05);
  min-width: 0;
}

.domain {
  font-size: 0.72rem;
  font-weight: 640;
  color: var(--text-primary);
  letter-spacing: -0.01em;
}

.workflow {
  font-size: 0.64rem;
  color: var(--text-secondary);
  line-height: 1.35;
  margin-top: 0.2rem;
}

.skill {
  font-size: 0.63rem;
  color: var(--text-muted);
  line-height: 1.35;
  margin-top: 0.3rem;
  padding-top: 0.3rem;
  border-top: 1px dashed var(--gridline);
}

.arrow {
  color: var(--accent-ink);
  font-family: var(--mono);
  margin-right: 0.3rem;
}
</style>
