<script setup>
/**
 * Per-platform support in one row. Each column carries a glyph and its own
 * words, so the status tint is the third channel and never the only one. The
 * two real limits are printed rather than hidden.
 */
const cols = [
  { name: 'macOS', state: 'good', text: 'all five sources' },
  { name: 'Windows', state: 'warning', text: 'PowerShell 7 + 5.1', note: 'cmd.exe cannot be hooked' },
  { name: 'Linux X11', state: 'good', text: 'all five sources' },
  { name: 'Linux Wayland', state: 'critical', text: 'no unattended screen capture', note: 'everything else normal' },
  { name: 'HPC login nodes', state: 'good', text: 'ssh + SLURM jobs captured' },
]

const GLYPH = { good: '●', warning: '▲', critical: '■' }
</script>

<template>
  <div class="viz-root strip">
    <div v-for="c in cols" :key="c.name" class="col" :class="`is-${c.state}`">
      <div class="top">
        <span class="glyph" aria-hidden="true">{{ GLYPH[c.state] }}</span>
        <span class="name">{{ c.name }}</span>
      </div>
      <div class="text">{{ c.text }}</div>
      <div v-if="c.note" class="note">{{ c.note }}</div>
    </div>
  </div>
</template>

<style scoped>
.strip {
  display: grid;
  grid-template-columns: repeat(5, minmax(0, 1fr));
  gap: 0.5rem;
  font-family: system-ui, -apple-system, 'Segoe UI', sans-serif;
}

.col {
  background: linear-gradient(180deg, rgba(255, 255, 255, 0.04), transparent 60%),
    var(--surface-1);
  border: 1px solid var(--panel-edge);
  border-top: 2px solid var(--seam, var(--panel-edge));
  border-radius: 8px;
  padding: 0.4rem 0.6rem 0.45rem;
  min-width: 0;
}

.col.is-good { --seam: var(--status-good); }
.col.is-warning { --seam: var(--status-warning); }
.col.is-critical { --seam: var(--status-critical); }

.top {
  display: flex;
  align-items: baseline;
  gap: 0.3rem;
}

.glyph {
  font-size: 0.55rem;
}

.is-good .glyph { color: var(--status-good); }
.is-warning .glyph { color: var(--status-warning); }
.is-critical .glyph { color: var(--status-critical); }

.name {
  font-size: 0.68rem;
  font-weight: 620;
  color: var(--text-primary);
}

.text {
  font-size: 0.62rem;
  color: var(--text-secondary);
  line-height: 1.3;
  margin-top: 0.12rem;
}

.note {
  font-size: 0.57rem;
  color: var(--text-muted);
  line-height: 1.3;
}
</style>
