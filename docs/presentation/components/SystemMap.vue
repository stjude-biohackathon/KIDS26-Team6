<script setup>
/**
 * The AutoCAB system map: the work on the left, the draft on the right, and
 * de-identification sitting in the middle of the path.
 *
 * Hand-built rather than generated, so the map inherits the deck's panel
 * surfaces and the middle step can read as the feature it is.
 */
const sources = ['shell commands', 'files · diffs', 'agent transcripts', 'screen · notes']

const draft = [
  { label: 'compare to existing skills', note: 'ensure skills stay atomic' },
  { label: 'draft the skill', note: 'steps, scripts, failure modes' },
  { label: 'you review it', note: 'edit, approve, or drop' },
]
</script>

<template>
  <div class="viz-root map">
    <!-- the work -->
    <section class="band">
      <header class="band-head"><span class="n">1</span> you do the work</header>
      <div class="chips">
        <span v-for="s in sources" :key="s" class="src">{{ s }}</span>
      </div>
      <div class="down" aria-hidden="true" />
      <div class="node artifact">
        <span class="label">one session folder</span>
        <span class="note">everything the work left behind, in order</span>
      </div>
    </section>

    <div class="rail mt-n16" aria-hidden="true" />

    <!-- de-identification -->
    <section class="band mid">
      <header class="band-head"><span class="n">2</span> de-identification</header>
      <div class="node feature">
        <span class="label">identifiers → surrogates</span>
        <span class="note">as it records, and again when the session ends</span>
      </div>
      <div class="mid-foot">on your machine, by default</div>
    </section>

    <div class="rail" aria-hidden="true" />

    <!-- the draft -->
    <section class="band">
      <header class="band-head"><span class="n">3</span> AutoCAB drafts a skill</header>
      <template v-for="(d, i) in draft" :key="d.label">
        <div class="down" v-if="i > 0" aria-hidden="true" />
        <div class="node">
          <span class="label">{{ d.label }}</span>
          <span class="note">{{ d.note }}</span>
        </div>
      </template>
      <div class="down" aria-hidden="true" />
      <div class="node out">
        <span class="label">a skill your team can run</span>
        <span class="note">the same workflow, a fraction of the time</span>
      </div>
    </section>
  </div>
</template>

<style scoped>
.map {
  display: grid;
  grid-template-columns: 1fr 1.1rem 0.95fr 1.1rem 1fr;
  align-items: stretch;
  font-family: system-ui, -apple-system, 'Segoe UI', sans-serif;
}

.band {
  display: flex;
  flex-direction: column;
  align-items: stretch;
  min-width: 0;
}

.band-head {
  display: flex;
  align-items: center;
  gap: 0.35rem;
  font-family: var(--mono);
  font-size: 0.6rem;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: var(--accent-ink);
  margin-bottom: 0.45rem;
}

.band-head .n {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 0.95rem;
  height: 0.95rem;
  border-radius: 999px;
  border: 1px solid rgba(34, 211, 238, 0.5);
  background: var(--accent-wash);
  font-size: 0.55rem;
}

/* --- nodes -------------------------------------------------------------- */

.node {
  background: linear-gradient(180deg, rgba(255, 255, 255, 0.05), transparent 65%),
    var(--surface-1);
  border: 1px solid var(--panel-edge);
  border-radius: 9px;
  padding: 0.42rem 0.6rem 0.46rem;
  box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.05);
  display: flex;
  flex-direction: column;
  min-width: 0;
}

.node .label {
  font-size: 0.72rem;
  font-weight: 620;
  color: var(--text-primary);
  line-height: 1.2;
}

.node .note {
  font-size: 0.6rem;
  color: var(--text-muted);
  line-height: 1.3;
}

/* The artifact on disk — what the two halves share. */
.node.artifact {
  border-style: dashed;
  border-color: rgba(34, 211, 238, 0.45);
  background: linear-gradient(180deg, var(--accent-wash), transparent 70%), var(--surface-1);
}

/* The middle step is the feature, so it is the lit one. */
.node.feature {
  border-color: var(--accent);
  background: linear-gradient(180deg, rgba(34, 211, 238, 0.2), transparent 72%),
    var(--surface-1);
  box-shadow: 0 0 22px rgba(34, 211, 238, 0.28);
  text-align: center;
  align-items: center;
}

.node.out {
  border-color: rgba(25, 158, 112, 0.55);
  background: linear-gradient(180deg, rgba(25, 158, 112, 0.14), transparent 70%), var(--surface-1);
}

.chips {
  display: flex;
  flex-direction: column;
  gap: 0.22rem;
}

.src {
  font-family: var(--mono);
  font-size: 0.6rem;
  color: var(--text-secondary);
  background: var(--surface-1);
  border: 1px solid var(--panel-edge);
  border-radius: 999px;
  padding: 0.12rem 0.55rem;
}

/* --- connectors --------------------------------------------------------- */

.down {
  width: 2px;
  height: 0.62rem;
  margin: 0.16rem 0 0.16rem 1.1rem;
  background: linear-gradient(180deg, var(--accent-deep), var(--accent));
  opacity: 0.75;
}

.rail {
  align-self: start;
  margin-top: 40px;
  height: 2px;
  background: linear-gradient(90deg, var(--accent-deep), var(--accent));
  opacity: 0.8;
  box-shadow: 0 0 10px rgba(34, 211, 238, 0.45);
}

.mid {
  justify-content: flex-start;
}

.mid-foot {
  margin-top: 0.45rem;
  font-family: var(--mono);
  font-size: 0.56rem;
  color: var(--text-muted);
  text-align: center;
}
</style>
