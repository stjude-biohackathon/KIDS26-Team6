<script setup>
/**
 * The per-source, per-platform support matrix. States are the fixed status
 * roles, and each cell carries a glyph plus its own text -- the color is the
 * third channel, never the only one.
 */
const platforms = ['macOS', 'Windows', 'Linux X11', 'Linux Wayland']

const rows = [
  {
    source: 'shell',
    cells: [
      { state: 'good', text: 'DevSQL + Atuin' },
      { state: 'warning', text: 'PowerShell only' },
      { state: 'good', text: 'DevSQL + Atuin' },
      { state: 'good', text: 'DevSQL + Atuin' },
    ],
  },
  {
    source: 'screen + titles',
    cells: [
      { state: 'good', text: 'mss + Quartz' },
      { state: 'good', text: 'mss + GDI' },
      { state: 'good', text: 'mss + xlib' },
      { state: 'critical', text: 'platform limit' },
    ],
  },
  {
    source: 'OCR',
    cells: [
      { state: 'good', text: 'Apple Vision' },
      { state: 'good', text: 'RapidOCR' },
      { state: 'good', text: 'RapidOCR' },
      { state: 'good', text: 'RapidOCR' },
    ],
  },
  {
    source: 'files · agents · context · remote',
    cells: [
      { state: 'good', text: 'yes' },
      { state: 'good', text: 'yes' },
      { state: 'good', text: 'yes' },
      { state: 'good', text: 'yes' },
    ],
  },
]

const GLYPH = { good: '●', warning: '▲', critical: '■' }
</script>

<template>
  <div class="viz-root matrix">
    <table>
      <thead>
        <tr>
          <th class="src">Source</th>
          <th v-for="p in platforms" :key="p">{{ p }}</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="row.source">
          <td class="src">{{ row.source }}</td>
          <td v-for="(cell, i) in row.cells" :key="i">
            <span class="cell" :class="`is-${cell.state}`">
              <span class="glyph" aria-hidden="true">{{ GLYPH[cell.state] }}</span>
              <span class="text">{{ cell.text }}</span>
            </span>
          </td>
        </tr>
      </tbody>
    </table>
    <div class="legend">
      <span class="cell is-good"><span class="glyph" aria-hidden="true">●</span><span>full support</span></span>
      <span class="cell is-warning"><span class="glyph" aria-hidden="true">▲</span><span>degraded, with a named reason</span></span>
      <span class="cell is-critical"><span class="glyph" aria-hidden="true">■</span><span>unavailable by platform limit</span></span>
    </div>
  </div>
</template>

<style scoped>
.matrix {
  font-family: system-ui, -apple-system, 'Segoe UI', sans-serif;
  background: linear-gradient(180deg, rgba(255, 255, 255, 0.04), transparent 55%),
    var(--surface-1);
  border: 1px solid var(--panel-edge);
  border-radius: 10px;
  padding: 0.55rem 0.7rem 0.6rem;
}

table {
  width: 100%;
  border-collapse: collapse;
}

th {
  color: var(--accent-ink);
  font-family: var(--mono);
  font-size: 0.6rem;
  font-weight: 600;
  letter-spacing: 0.09em;
  text-transform: uppercase;
  text-align: left;
  padding: 0 0.5rem 0.35rem;
  border-bottom: 1px solid var(--baseline);
}

td {
  padding: 0.3rem 0.5rem;
  border-bottom: 1px solid var(--gridline);
  vertical-align: middle;
}

tbody tr:last-child td {
  border-bottom: none;
}

.src {
  color: var(--text-primary);
  font-size: 0.68rem;
  font-weight: 600;
  white-space: nowrap;
}

td.src {
  font-family: var(--mono);
  font-weight: 500;
  font-size: 0.64rem;
}

.cell {
  display: inline-flex;
  align-items: baseline;
  gap: 0.3rem;
  font-size: 0.64rem;
  line-height: 1.3;
}

.cell .text,
.cell span:last-child {
  color: var(--text-secondary);
}

.glyph {
  font-size: 0.56rem;
}

.is-good .glyph { color: var(--status-good); }
.is-warning .glyph { color: var(--status-warning); }
.is-critical .glyph { color: var(--status-critical); }

.legend {
  display: flex;
  flex-wrap: wrap;
  gap: 0.9rem;
  margin-top: 0.5rem;
  padding-top: 0.4rem;
  border-top: 1px dashed var(--gridline);
  color: var(--text-muted);
}

.legend .cell .text,
.legend .cell span:last-child {
  color: var(--text-muted);
  font-size: 0.6rem;
}
</style>
