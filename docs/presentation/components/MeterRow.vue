<script setup>
/**
 * A column of meters: one measure (a proportion), one hue, one row per
 * category. Single series, so no legend box -- the title names the measure and
 * every row is direct-labeled, which also satisfies the relief rule for the
 * light-surface contrast warning on the accent.
 *
 * The fill carries severity; the unfilled track is a lighter step of the fill's
 * own ramp so state reads across the whole bar.
 */
defineProps({
  title: { type: String, default: '' },
  unit: { type: String, default: '' },
  items: { type: Array, required: true },
})

const fmt = (v) => v.toFixed(3)
</script>

<template>
  <div class="viz-root meters">
    <div v-if="title" class="meters-title">
      {{ title }}<span v-if="unit" class="meters-unit"> · {{ unit }}</span>
    </div>
    <div class="meters-body">
      <div v-for="item in items" :key="item.label" class="meter" :class="`sev-${item.severity || 'accent'}`">
        <div class="meter-label">{{ item.label }}</div>
        <div class="meter-track" role="img" :aria-label="`${item.label}: ${fmt(item.value)}`">
          <div class="meter-fill" :style="{ width: `${Math.max(item.value * 100, 1.2)}%` }" />
        </div>
        <div class="meter-value">{{ fmt(item.value) }}</div>
        <div class="meter-note">{{ item.note || '' }}</div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.meters {
  font-family: system-ui, -apple-system, 'Segoe UI', sans-serif;
}

.meters-title {
  color: var(--text-secondary);
  font-size: 0.74rem;
  font-weight: 600;
  margin-bottom: 0.5rem;
}

.meters-unit {
  font-weight: 400;
  color: var(--text-muted);
}

.meter {
  display: grid;
  grid-template-columns: 4.8rem 1fr 2.7rem minmax(0, 6.5rem);
  align-items: center;
  gap: 0.6rem;
  padding: 0.16rem 0;
}

.meter-label {
  color: var(--text-primary);
  font-size: 0.74rem;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
}

.meter-track {
  background: var(--seq-track);
  border-radius: 4px;
  height: 0.62rem;
  overflow: hidden;
}

.meter-fill {
  height: 100%;
  background: var(--seq-fill);
  /* 4px rounded data-end, square against the baseline it grows from. */
  border-radius: 0 4px 4px 0;
}

.sev-warning .meter-fill { background: var(--status-warning); }
.sev-critical .meter-fill { background: var(--status-critical); }

.meter-value {
  color: var(--text-primary);
  font-size: 0.74rem;
  font-variant-numeric: tabular-nums; /* a column of numbers, so tabular */
  text-align: right;
}

.meter-note {
  color: var(--text-muted);
  font-size: 0.66rem;
  line-height: 1.2;
}
</style>
