<script setup>
/**
 * A row of stat tiles. Each tile is a label + a single value, optionally with a
 * note and a status cue. There is no plot inside a tile, so per the interaction
 * rules these carry no hover layer.
 *
 * A status is never carried by color alone: the glyph and the status word
 * render alongside it.
 */
defineProps({
  tiles: { type: Array, required: true },
  columns: { type: Number, default: 4 },
})

const GLYPH = {
  good: '●',
  warning: '▲',
  serious: '▲',
  critical: '■',
}
</script>

<template>
  <div class="viz-root tiles" :style="{ '--cols': columns }">
    <div v-for="tile in tiles" :key="tile.label" class="tile" :class="tile.status && `has-${tile.status}`">
      <div class="tile-label">{{ tile.label }}</div>
      <div class="tile-value">{{ tile.value }}</div>
      <div v-if="tile.status" class="tile-status" :class="`is-${tile.status}`">
        <span aria-hidden="true">{{ GLYPH[tile.status] }}</span>
        <span>{{ tile.statusLabel || tile.status }}</span>
      </div>
      <div v-if="tile.note" class="tile-note">{{ tile.note }}</div>
    </div>
  </div>
</template>

<style scoped>
.tiles {
  display: grid;
  grid-template-columns: repeat(var(--cols), minmax(0, 1fr));
  gap: 0.6rem;
  font-family: system-ui, -apple-system, 'Segoe UI', sans-serif;
}

.tile {
  position: relative;
  overflow: hidden;
  background: linear-gradient(180deg, rgba(255, 255, 255, 0.05), transparent 62%),
    var(--surface-1);
  border: 1px solid var(--panel-edge);
  border-radius: 10px;
  padding: 0.7rem 0.8rem 0.75rem;
  box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.05);
  min-width: 0;
}

/* A lit seam along the top edge; the tile's status recolors it, and the glyph
   and status word in the markup carry the same state as text. */
.tile::before {
  content: '';
  position: absolute;
  top: 0;
  left: 0;
  right: 0;
  height: 2px;
  background: linear-gradient(90deg, var(--seam, var(--accent)), transparent 85%);
}

.tile.has-good { --seam: var(--status-good); }
.tile.has-warning { --seam: var(--status-warning); }
.tile.has-serious { --seam: var(--status-serious); }
.tile.has-critical { --seam: var(--status-critical); }

.tile-label {
  color: var(--text-secondary);
  font-size: 0.66rem;
  line-height: 1.3;
  min-height: 2.6em;
}

.tile-value {
  color: var(--text-primary);
  font-size: 1.8rem;
  font-weight: 650;
  letter-spacing: -0.02em;
  line-height: 1.1;
  margin-top: 0.25rem;
  /* Proportional figures: tabular-nums loosens a standalone display number. */
}

.tile-status {
  display: flex;
  align-items: center;
  gap: 0.3rem;
  margin-top: 0.3rem;
  font-family: var(--mono);
  font-size: 0.6rem;
  font-weight: 600;
  letter-spacing: 0.03em;
}

.tile-status.is-good { color: var(--status-good); }
.tile-status.is-warning { color: var(--status-warning); }
.tile-status.is-serious { color: var(--status-serious); }
.tile-status.is-critical { color: var(--status-critical); }

.tile-note {
  color: var(--text-muted);
  font-size: 0.62rem;
  line-height: 1.35;
  margin-top: 0.3rem;
}
</style>
