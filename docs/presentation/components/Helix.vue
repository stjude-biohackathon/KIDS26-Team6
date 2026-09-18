<script setup>
/**
 * A decorative double helix. Purely chrome -- it encodes nothing, so it draws
 * in the accent hue the data palette does not own, and it is aria-hidden.
 *
 * `rungs` strands are sampled from two phase-shifted sine waves; the base pairs
 * are the segments between them.
 */
const props = defineProps({
  rungs: { type: Number, default: 26 },
  height: { type: Number, default: 420 },
})

const W = 150
const H = 1000
// exposed to the template for the user-space gradient below
const AMP = 56
const TURNS = 3.1

const y = (i, n) => (i / (n - 1)) * H
const x = (i, n, phase) =>
  W / 2 + Math.sin((i / (n - 1)) * TURNS * Math.PI * 2 + phase) * AMP

const strand = (phase) => {
  const n = 160
  return Array.from({ length: n }, (_, i) => `${x(i, n, phase).toFixed(1)},${y(i, n).toFixed(1)}`).join(' ')
}

const pairs = Array.from({ length: props.rungs }, (_, i) => {
  const n = props.rungs
  return {
    x1: x(i, n, 0),
    x2: x(i, n, Math.PI),
    y: y(i, n),
    // Rungs seen edge-on are shorter; fade them so the twist reads as depth.
    o: 0.3 + 0.7 * Math.abs(Math.sin((i / (n - 1)) * TURNS * Math.PI * 2)),
  }
})
</script>

<template>
  <svg
    class="helix"
    :viewBox="`0 0 ${W} ${H}`"
    :style="{ height: `${height}px` }"
    aria-hidden="true"
    focusable="false"
  >
    <defs>
      <linearGradient id="helix-fade" gradientUnits="userSpaceOnUse" :x1="0" :y1="0" :x2="0" :y2="H">
        <stop offset="0%" stop-color="var(--accent)" stop-opacity="0" />
        <stop offset="28%" stop-color="var(--accent)" stop-opacity="0.9" />
        <stop offset="72%" stop-color="var(--accent-deep)" stop-opacity="0.8" />
        <stop offset="100%" stop-color="var(--accent-deep)" stop-opacity="0" />
      </linearGradient>
    </defs>
    <g stroke="url(#helix-fade)" fill="none">
      <polyline :points="strand(0)" stroke-width="2.6" />
      <polyline :points="strand(Math.PI)" stroke-width="2.6" />
      <line
        v-for="(p, i) in pairs"
        :key="i"
        :x1="p.x1"
        :y1="p.y"
        :x2="p.x2"
        :y2="p.y"
        stroke-width="1.8"
        :stroke-opacity="p.o"
      />
    </g>
  </svg>
</template>

<style scoped>
.helix {
  display: block;
  width: auto;
  filter: drop-shadow(0 0 14px rgba(34, 211, 238, 0.35));
}
</style>
