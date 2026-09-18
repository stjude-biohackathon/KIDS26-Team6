# AutoCAB presentation

A [Slidev](https://sli.dev) deck covering AutoCAB's architecture and its
versatility across St. Jude workflows. **Six slides, deliberately** — the deck
is meant to land the project in a few minutes, not to document it.

There are two cuts:

| Entry | Slides | Length | For |
|---|---|---|---|
| `slides.md` | 6 | ~8 minutes | The full talk |
| `lightning.md` | 3 | 2-3 minutes | A lightning slot |

## Run it

```bash
cd docs/presentation
npm install
npm run dev            # the full deck, http://localhost:3030
npm run dev:lightning  # the 3-slide cut
```

Presenter view is at `/presenter`; speaker notes live in each slide's trailing
HTML comment and are also available via `npm run export:notes`.

## Build and export

```bash
npm run build            # dist/ (base path /presentation/)
npm run build:lightning  # dist/lightning/ (base path /presentation/lightning/)
npm run export           # PDF (requires playwright-chromium)
npm run export:lightning # PDF of the lightning cut
```

`npm run export` needs a headless browser — install it once with
`npx playwright install chromium` (or let Slidev prompt you for
`playwright-chromium`). `npm run build` needs no browser.

## The six slides

| # | Slide | Job |
|---|---|---|
| 1 | Cover | The one-sentence claim |
| 2 | The loop | Why skills stay tacit, and the seven-step path out |
| 3 | **Architecture** | Three moves — record, de-identify, draft |
| 4 | Privacy | De-identification as a feature of the path, not a checkpoint |
| 5 | **Versatility** | Six domains, and the platforms the recorder covers |
| 6 | Impact | Ten minutes start to draft, who benefits, what is not built |

Slides 3 and 5 are the two the deck is built around. Everything else exists to
make them believable.

## The lightning cut

`lightning.md` is the same story in three slides, timed for two to three
minutes: the cover, the problem plus the five-step path, and the three-move map
from slide 3. Each slide's speaker note carries its budget (~25s / ~50s / ~60s).

It is a separate entry in **this** folder, not a copied project, so it shares
`style.css`, `components/` and `global-bottom.vue` — restyle once and both cuts
move together. Only the words differ.

## Layout

```text
slides.md              The full deck — headmatter, cover, and all six slides
lightning.md           The 3-slide, 2-3 minute cut (same styles and components)
global-bottom.vue      Persistent footer: wordmark, progress rail, slide count
components/
  SystemMap.vue        The three-move map (slide 3), hand-built, not generated
  FlowChain.vue        Horizontal pipeline chain with an optional gate step
  DomainGrid.vue       Domain / workflow / resulting skill cards
  PlatformStrip.vue    Per-platform support in one row, limits printed
  Helix.vue            Decorative double helix for the cover
  StatTiles.vue        Label + value tiles with a status cue (not in the deck)
  PlatformMatrix.vue   Fuller per-source × per-platform table (not in the deck)
  GateLadder.vue       The five-layer LLM egress gate (not in the deck)
  MeterRow.vue         Single-measure proportion meters (not in the deck)
style.css              The dark aesthetic, the `--viz-*` palette, the type scale
pages/archive/         The previous long-form deck, kept for reference
```

`pages/archive/` holds the earlier 20-slide version — the redaction tiers, the
policy actions, the five-layer gate, the per-channel recall meters, the Skill
Forge deep dive and the "what this does not prove" slide. Nothing was deleted;
if a longer session needs that material, the slides are there and the four
unused components above are what they render with.

## Editing

Everything is one Markdown file. Slides are separated by `---`, and the
reusable furniture is CSS classes in `style.css`:

- `.kicker` — the small monospace label above a slide title
- `.claim` — the one line a slide is allowed to assert
- `.panel` — the default container for a block of related lines
- `.callout.is-good | .is-warning | .is-critical | .is-accent` — a state-tinted box
- `.chips` / `.chip` — a row of monospace tokens
- `.spec` — a `<dl>` of label + one-line explanation pairs

Note that this theme's UnoCSS config does **not** emit the `text-*` size
utilities, so `style.css` defines `.text-xs` … `.text-xl` itself, including the
descendant rules that reach markdown children. Slidev's own
`.slidev-layout p { line-height: 1.5rem }` is a fixed value that otherwise
survives a smaller parent — which is why size classes have to be defined with
those descendant rules rather than on the wrapper alone.

## Color

`style.css` separates two layers, and they must not be mixed:

- **Data roles** (`--series-*`, `--seq-*`, `--status-*`) come from the validated
  reference palette and are re-stepped for this deck's cool dark surface
  (`#101826`) rather than flipped from the light set. Both sets pass the
  colorblind-separation, lightness-band and contrast checks on their own
  surface. Only marks that encode a value use these.
- **Chrome** (`--accent*`, the panel surfaces, the glows) is decoration. It is
  cyan precisely because the data palette does not own that hue.

Status always ships with a glyph (`●` `▲` `■`) and a word, so no meaning is
carried by color alone. If you restyle, change the roles here rather than
inlining hex in a component.

## Tone

Slides 3-5 are deliberately plain-spoken: no interface names, no evaluation
numbers, and de-identification presented as something the path does for you
rather than a gate you have to clear. The measured version of that claim lives
in `docs/deid-evaluation.md` — bring it to questions rather than to the slide.
Regenerate it with `autocab deid eval --write-scorecard`.

No slide carries an evaluation number any more, so nothing in the deck goes
stale when the scorecard is regenerated.
