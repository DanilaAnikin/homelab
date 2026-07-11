# LaunchMail — Design Audit & Redesign Proposal

> Facet grades: Foundations C · Components B · App-shell C · Pages C · Marketing/Brand D

---

## Benchmark

I have everything I need from the audit to produce the benchmark. This is a synthesis task, so I'll write the strategic document directly.

# LaunchMail Design Benchmark — What a Credible 2026 Email Platform Looks Like

## 1. The reference set & what to steal from each

| Product | What it nails | Lift for LaunchMail |
|---|---|---|
| **Resend** | Restrained brand color used as *accent*, not flood; gorgeous logs/events tables; mono for technical values | Logs as the showcase surface; one disciplined accent |
| **Postmark** | Trustworthy, "infrastructure-grade" seriousness; deliverability data front-and-center | Status/health legibility; never flashy |
| **Linear** | Tight density, layered surfaces, command palette, motion personality | Shell elevation, ⌘K, easing tokens |
| **Vercel/Geist** | Tinted near-neutrals, shimmer skeletons, data-slot primitives | Warm the hue-0 grays, skeleton continuity |
| **Stripe** | Calm authority, metric hierarchy, breadcrumbed deep nav | Stat hierarchy, top-bar context |
| **Loops/Brevo** | Warmth + approachability for non-infra users | Empty states, onboarding voice |

The right reference point for LaunchMail specifically is **Resend × Postmark**: developer-credible, calm, data-forward — not Mailchimp-playful, not enterprise-gray.

## 2. Brand personality — pick one, commit

The product is "self-hosted transactional email & forms, BYO SMTP." That's an **infrastructure-for-builders** story. The credible personality is:

> **"Calm, technical, in-control."** Quiet confidence over excitement. The brand shows up in *precision and consistency*, with the violet as a single deliberate accent — never decoration.

Reject the two failure modes the audit shows: (a) "default shadcn neutral app" (current state), and (b) over-correcting into a gradient-heavy consumer marketing skin. The violet should feel like Resend's pink or Linear's indigo: rare, intentional, load-bearing.

## 3. Foundations principles

**Color**
- **Spend the brand token.** Violet appears as: active-nav tint/accent bar, brand-tinted icon chips (`brand/10` + `text-brand`), one meaningful badge (plan/primary domain), focus rings, the primary KPI accent, and hero/auth/invite washes derived from `--brand` (not raw `rgba`). Goal: remove the primary button and the product is *still* recognizable.
- **Kill hue-0 neutrals.** Give every gray a tiny shared chroma (~0.004–0.006) on a single hue (the brand-adjacent ~273–285, or a deliberate warm counter-hue), applied to **both** light and dark so temperatures match. This is the single biggest "designed vs. starter" tell.
- **Semantic tokens only.** Ban raw `green-/red-/emerald-/amber-NNN`. One green for "sent" everywhere. Add darker **on-tint** success/warning text tokens — the current badges fail AA (success 3.36:1, warning 2.54:1).

**Typography**
- Build a real ramp with named, *enforced* tiers: **display** (~30px page titles) / **section** (~20px) / **card-title** (16) / **body** (14) / **caption** (12) / **stat** (distinct numeric tier) / **eyebrow**. Today it's binary (`text-sm`/`text-xs`) with titles and KPIs colliding at identical `text-2xl semibold`.
- **Separate heading voice from body**: heavier weight + tighter tracking on `--font-heading` (Geist semibold/tight is fine — but it must look different from body).

**Elevation & spacing**
- Add an **elevation token scale**. Dark mode: lighter card bg + top hairline highlight, not drop shadows (they don't show on near-black). Increase card-vs-page value gap so surfaces separate without leaning on a 1px ring.
- Add `--ease-*` / `--duration-*` motion tokens + one standard transition recipe. Standardize page container widths (e.g. `max-w-5xl` dense lists, `max-w-3xl` forms).

## 4. Dashboard IA & data-display patterns (the 2026 norm)

- **Persistent shell**: fixed left rail (responsive — collapses to a Sheet drawer below `lg`) + a slim sticky top bar (`h-12`, `border-b`, `bg-background/80 backdrop-blur`) carrying **breadcrumbs** left, **⌘K search + theme + account** right. The current shell has neither a top bar nor any responsive breakpoint.
- **Grouped nav**, not a flat 12-item dump. Sections under eyebrow labels: **Send** (Overview, Broadcasts, Templates, SMTP) · **Audience** (Audiences, Forms, Suppressions) · **Deliverability** (Domains, Webhooks, Analytics, Logs) · **Settings** pinned to a bottom group by the account control.
- **Metrics**: one `<Stat>` component — label (eyebrow) + value (numeric tier, `tabular-nums`) + **trend delta** + optional sparkline; brand accent on the *primary* KPI only.
- **Lists/logs are real tables**, not flex-row fakes: aligned columns, muted uppercase eyebrow headers, row hover, right-aligned `tabular-nums` time/count columns, sortable time + status. Logs and broadcasts are the surfaces a buyer judges credibility on — they currently look like placeholders.
- **Status**: one pill system. Fold StatusBadge into Badge tonal variants (dot + tint), AA-compliant, consistent geometry.
- **Charts**: replace the decorative SVG with a real chart (Recharts + the existing `chart-1..5` tokens): gridlines, hover tooltip with date+value, toggleable sent/failed/opens/clicks series.
- **Loading**: layout-matched skeleton shimmers per route (skeleton header + stat cards + table rows), `loading.tsx` on *every* async route. Reserve the envelope animation for auth/first-paint only — never `h-screen` inside the shell.

## 5. The 6–8 cheapest moves from "generic template" → "credible product"

Ordered by impact-per-effort. Most reuse assets that already exist.

1. **Spend the brand token (highest leverage).** Tinted active-nav state + left accent bar, brand `/10` icon chips replacing gray wells, one brand badge, brand focus rings, and a `--brand`-derived wash on dashboard header / auth panel / invite. Removes the "indistinguishable from blank shadcn" verdict in one sweep.
2. **Warm the neutrals.** Add a shared sliver of chroma+hue to every gray, matched across light/dark. Pure-token change, instant "intentional system" upgrade.
3. **Fix the AA failures + collapse to semantic colors.** Darker on-tint success/warning tokens; lint-ban raw palette utilities; one green/red everywhere. Cheap, and removes the "assembled by different hands" smell.
4. **Real type hierarchy, enforced.** Ship the named tiers, route *all* titles through `PageHeader`/`text-display`, add a `text-stat` numeric tier so KPIs stop impersonating H1s. Kills the flat/monotone read.
5. **Ship the shared DataTable** and migrate logs, broadcasts, suppressions, activity, and dashboard recent-activity onto it. Single change fixes the biggest "admin-template placeholder" tell across ~5 surfaces.
6. **`loading` prop on Button + inline `aria-invalid` validation.** The error-ring styling already exists, unused. Async buttons stop looking dead; forms start feeling responsive and trustworthy.
7. **Add the top bar + ⌘K + responsive Sheet sidebar + grouped nav.** Adopt shadcn's Sidebar wholesale — fixes mobile, adds collapse, and instantly reads "product, not template."
8. **Build the landing + metadata layer.** Root is currently a bare redirect with zero metadata. A hero (existing logo + envelope-draw animation + positioning copy), the orphaned `TechStack` strip, a 4-card feature grid, plus a title template / OG image / real favicon. Moves first-contact from D to B reusing assets already built.

**One-line thesis:** LaunchMail's bones are a B; it presents as a C because the brand is invisible, neutrals are dead, hierarchy is flat, and data surfaces are faked. The fix is not a redesign — it's *spending tokens that already exist* (brand color, type utilities, error states, semantic colors) and upgrading the three surfaces buyers judge: the shell, the logs table, and first contact.

---

I have the exact token file and component inventory. Writing the proposal now.

# LaunchMail Redesign Proposal — Final Direction

## 1. Executive summary

LaunchMail's design system is structurally sound but presents as an unfinished shadcn starter. The audit is consistent across all five facets: the *bones are a B, the presentation is a C* — and the reason is always the same handful of failures.

- **The brand is invisible.** `--brand` is defined (`oklch(0.515 0.23 273)`) but `bg-brand`/`text-brand` are used **zero times** in `apps/web`. Violet only ever surfaces as `primary` on one button per page. Strip that button and the product is indistinguishable from a blank starter — this is what "looks terrible" is reacting to.
- **The neutrals are dead.** Every light-mode gray is `oklch(L 0 0)` — pure achromatic, ice-cold. Dark mode tints neutrals (hue 285), light mode does not, so the two themes don't even share a temperature. This is the canonical "unfinished template" tell.
- **Hierarchy is collapsed.** The type scale is effectively binary (`text-sm` ×85, `text-xs` ×85; `text-lg` ×0). Page titles and KPI numbers render as the *exact same* `text-2xl font-semibold tracking-tight`, so an H1 and a metric value compete at the same level. The `text-display`/`text-title`/`text-eyebrow` utilities exist but are used a combined ~3 times.
- **The surfaces buyers judge are faked.** Logs, broadcasts, suppressions, and recent-activity are all hand-rolled `flex justify-between` rows — no column headers, no alignment, no sort. The analytics "chart" is a static decorative SVG with no axes or tooltips. These are the first two screens a deliverability buyer evaluates.
- **Real accessibility bugs ship today.** `StatusBadge` success text is 3.36:1 and warning is 2.54:1 against their tint (both fail WCAG AA); the dark-mode primary button label is 3.73:1. The most-repeated status signals in the product are illegible.

**The single most important shift:** stop treating this as a redesign and treat it as **spending the design tokens that already exist** — make the brand color load-bearing, warm the neutrals, enforce a real type ramp, and rebuild the three surfaces buyers judge (shell, logs table, first contact). The bones don't need replacing; they need to be *applied with intent*.

## 2. Comparison & recommendation

Scoring the three directions (1–5, higher is better; Implementation cost is inverted so higher = cheaper/easier).

| Criterion | A · Refined Minimal (Linear×Vercel) | B · Brand-Forward (Warm Editorial) | C · Data-Dense Pro (Stripe×Datadog) |
|---|---|---|---|
| Brand distinctiveness | 3 — disciplined but quiet; risks reading "tasteful but generic" | **5** — serif headlines + warm field + violet rail; unmistakable | 4 — status-tinted rows + rare violet; recognizable to power users |
| Modernity | **5** — exactly the current Linear/Vercel idiom | 4 — editorial warmth is fresh but trend-sensitive | **5** — cockpit density reads "2026 infra tool" |
| Fit for an email platform | 4 — calm + data-credible | 3 — warmth/serif risks under-serving the deliverability-buyer credibility need | **5** — logs-as-cockpit is exactly the Resend/Postmark muscle memory |
| Accessibility | **5** — neutral hover states, all-AA tints, no risky color reliance | 4 — solid, but serif at small sizes + warm-on-warm needs care | 4 — 13px base + status-color reliance needs the dot/label backstop (it has it) |
| Implementation cost (higher=cheaper) | **5** — ~80% token work, no new fonts/deps beyond Recharts | 3 — adds a serif font, washes, illustrated empties, more bespoke surfaces | 4 — token work + DataTable + ⌘K + health-dot; more components than A |

**Recommendation: Direction A — Refined Minimal, as the spine, grafted with the two strongest ideas from B and C.**

A wins because it is the lowest-risk, highest-fidelity-to-the-product path: it is overwhelmingly token work on the file that already exists, it matches the "infrastructure for builders" thesis, and it carries the fewest accessibility and trend liabilities. But A alone risks the very "tasteful but generic" verdict we're escaping. So we graft:

- **From C (the strongest fit-for-purpose ideas):** the **status-tinted data row** (2px left status bar + faint destructive row-wash on terminal failures), the **`<Stat>` strip with sparkline + delta**, the **⌘K command palette**, and **Geist Mono promoted to a first-class data face**. These make the logs/dashboard cockpit-credible — A's biggest gap.
- **From B (one identity move, used sparingly):** a single **`--brand`-derived radial wash** on the dashboard header band, auth panel, and invite — driven by the brand token, never raw `rgba`. We **reject B's serif display face** (Instrument Serif): it adds a dependency, fights the precision-instrument personality, and raises small-size a11y risk. Geist semibold + tight tracking carries the heading voice.

The result is **"Refined Minimal with a cockpit data core"**: Linear/Vercel calm on the chrome, Datadog/Resend density on the data, one disciplined violet wash for identity.

## 3. THE PROPOSAL — Refined Minimal (cockpit data core)

### 3.1 Design principles

1. **Spend the brand, ration the brand.** Violet is load-bearing and rare: active-nav accent spine, the one primary KPI, focus rings, brand `/10` icon chips, the header/auth wash. The eye learns violet = "primary / active / you-are-here." Cover the logo and the product is still recognizable.
2. **Every gray is intentional.** No `oklch(L 0 0)`. A single shared hue (285, brand-adjacent) with a sliver of chroma, *matched light↔dark* so the two themes are one system at different exposures.
3. **Hierarchy by enforced ramp.** A real, named type scale with a dedicated `text-stat` numeric tier so KPIs stop impersonating H1s. All titles route through `PageHeader`/`text-display`. The ramp is enforced via `@utility`, not duplicated inline.
4. **Data is the showcase.** Real tables with mono `tabular-nums` columns, eyebrow headers, status-tinted rows. Logs and the dashboard are optimized first — they are what buyers judge.
5. **Depth by value, motion by confirmation.** Surfaces separate by lightness steps + hairline (dark uses top-inset highlight, not invisible drop shadows). Motion is fast and confirming, never performing — one easing/duration token set.
6. **Token-first, no bespoke drift.** Semantic tokens only (ban raw `green-/red-/amber-NNN`); one pill system; one `<Stat>`, one `<DataTable>`. Polish lives in the system, not in per-page hacks.

### 3.2 Design-token spec

All OKLCH. Hue **285** for neutrals, **280** for brand, matched across themes. These map directly onto the `@theme inline` aliases that already exist in `globals.css` (lines 10–60) and the `:root`/`.dark` blocks (lines 62–147).

#### Neutrals & surfaces

| Var | Light | Dark | Notes |
|---|---|---|---|
| `--background` | `oklch(0.992 0.002 285)` | `oklch(0.165 0.005 285)` | Replaces `1 0 0` / `0.155 0.004 285`. |
| `--surface` *(new)* | `oklch(0.978 0.003 285)` | `oklch(0.190 0.006 285)` | Page gutter / table-zebra base. Add `--color-surface` alias. |
| `--card` | `oklch(1 0.0015 285)` | `oklch(0.205 0.007 285)` | Light card pops off off-white bg; dark card is *lighter* than bg. |
| `--card-foreground` | `oklch(0.205 0.01 285)` | `oklch(0.975 0.003 285)` | |
| `--popover` | `oklch(1 0.0015 285)` | `oklch(0.225 0.008 285)` | One step above card. |
| `--popover-foreground` | `oklch(0.205 0.01 285)` | `oklch(0.975 0.003 285)` | |
| `--foreground` | `oklch(0.205 0.01 285)` | `oklch(0.98 0.003 285)` | ~17:1 body contrast retained. |
| `--muted` | `oklch(0.97 0.004 285)` | `oklch(0.245 0.007 285)` | |
| `--muted-foreground` | `oklch(0.505 0.012 285)` | `oklch(0.715 0.012 285)` | Light dropped from 0.552 → clears AA (~5.0:1) on white. |
| `--secondary` | `oklch(0.965 0.005 285)` | `oklch(0.262 0.008 285)` | |
| `--secondary-foreground` | `oklch(0.205 0.01 285)` | `oklch(0.985 0 0)` | |
| `--accent` | `oklch(0.965 0.006 285)` | `oklch(0.278 0.012 285)` | Neutral hover — **not** brand. |
| `--accent-foreground` | `oklch(0.205 0.01 285)` | `oklch(0.985 0 0)` | |
| `--border` | `oklch(0.914 0.005 285)` | `oklch(1 0 0 / 9%)` | Hairline; dark stays alpha-white for the lit-edge look. |
| `--border-strong` *(new)* | `oklch(0.86 0.006 285)` | `oklch(1 0 0 / 16%)` | Table header rule, input outline. Add `--color-border-strong`. |
| `--input` | `oklch(0.90 0.005 285)` | `oklch(1 0 0 / 13%)` | |
| `--ring` | `oklch(0.515 0.21 280)` | `oklch(0.66 0.18 280)` | Brand-derived. |

#### Brand ramp (new 50→950) — hue 280

Add as raw `--brand-N` vars in both blocks plus `--color-brand-N` aliases in `@theme inline`.

| Step | Value | Primary use |
|---|---|---|
| `--brand-50` | `oklch(0.975 0.012 285)` | header/auth/invite wash (light) |
| `--brand-100` | `oklch(0.945 0.03 284)` | icon-chip bg, active-nav tint (light) |
| `--brand-200` | `oklch(0.90 0.06 283)` | chip hover, brand-badge bg |
| `--brand-300` | `oklch(0.83 0.10 282)` | borders on brand surfaces |
| `--brand-400` | `oklch(0.72 0.16 281)` | brand chart/spark stroke (dark) |
| `--brand-500` | `oklch(0.605 0.21 280)` | accent spine, primary-KPI value/spark |
| `--brand-600` | `oklch(0.515 0.23 280)` | `--primary` / `--brand` (light) |
| `--brand-700` | `oklch(0.44 0.21 280)` | primary button hover (light), on-tint text |
| `--brand-800` | `oklch(0.37 0.17 281)` | brand-on-tint text on `brand-100` (AA) |
| `--brand-900` | `oklch(0.30 0.13 282)` | pressed |
| `--brand-950` | `oklch(0.22 0.09 283)` | deep accents |

Then `--primary: var(--brand-600)` light / `oklch(0.585 0.20 280)` dark (dropped from 0.62 so the white label clears **4.5:1** — fixes the 3.73:1 dark-button bug). `--brand: var(--primary)`. `--brand-subtle: var(--brand-100)` (light) / `oklch(0.32 0.08 280)` (dark). `--brand-emphasis: var(--brand-800)` (light) / `oklch(0.84 0.10 280)` (dark).

#### Semantic — tint / base / on-tint triples (fixes the AA failures)

Each semantic gets the existing base + `-foreground`, plus a new `-tint` (badge/row bg) and `-on` (AA text on tint). Add `--color-*-tint` / `--color-*-on` aliases.

| | Light base | Light tint | Light on (text) | Dark tint | Dark on |
|---|---|---|---|---|---|
| success | `oklch(0.60 0.15 150)` | `oklch(0.955 0.03 150)` | `oklch(0.40 0.10 150)` ≈5.2:1 | `oklch(0.70 0.16 150 / 16%)` | `oklch(0.84 0.13 150)` |
| warning | `oklch(0.70 0.15 70)` | `oklch(0.96 0.045 75)` | `oklch(0.46 0.10 60)` ≈5.4:1 | `oklch(0.80 0.15 75 / 16%)` | `oklch(0.86 0.13 78)` |
| info | `oklch(0.56 0.16 252)` | `oklch(0.955 0.03 252)` | `oklch(0.42 0.11 252)` | `oklch(0.66 0.15 252 / 16%)` | `oklch(0.82 0.12 252)` |
| destructive | `oklch(0.577 0.245 27)` | `oklch(0.955 0.03 25)` | `oklch(0.45 0.18 27)` | `oklch(0.70 0.19 25 / 18%)` | `oklch(0.83 0.12 22)` |
| pending *(new)* | `oklch(0.55 0.012 285)` | `oklch(0.965 0.004 285)` | `oklch(0.46 0.012 285)` | `oklch(0.255 0.007 285)` | `oklch(0.72 0.012 285)` |

Keep `*-foreground` (text on the *solid* base) as-is; the `-on` token is for text on the *tint*.

#### Chart colors — re-keyed to status hues

So a sent/failed chart matches the badges the user just saw: `--chart-1` = brand-500/400 (primary series = brand), `--chart-2` = info-blue `oklch(0.56 0.16 252)`, `--chart-3` = success-green `oklch(0.60 0.15 150)`, `--chart-4` = warning-amber `oklch(0.70 0.15 70)`, `--chart-5` = destructive-red `oklch(0.577 0.245 27)`.

#### Sidebar tokens

| Var | Light | Dark |
|---|---|---|
| `--sidebar` | `oklch(0.982 0.005 285)` | `oklch(0.185 0.006 285)` |
| `--sidebar-foreground` | `oklch(0.205 0.01 285)` | `oklch(0.985 0 0)` |
| `--sidebar-primary` | `var(--brand-600)` | `oklch(0.66 0.19 280)` |
| `--sidebar-accent` | `var(--brand-100)` | `oklch(0.30 0.06 280)` |
| `--sidebar-accent-foreground` | `var(--brand-800)` | `oklch(0.86 0.06 280)` |
| `--sidebar-border` | `oklch(0.914 0.005 285)` | `oklch(1 0 0 / 9%)` |
| `--sidebar-ring` | `var(--ring)` | `var(--ring)` |

#### Typography scale (new `@utility` block, replacing lines 167–175)

Geist Sans for UI; **Geist Mono for every machine value** (IDs, timestamps, counts, rates, addresses, DNS records). Heading voice = Geist semibold + tight tracking; **no second display face**.

| Utility | Size / line-height | Weight | Tracking | Use |
|---|---|---|---|---|
| `text-display` | 30 / 36 | 600 | -0.02em | page H1 (one per page) |
| `text-section` *(new)* | 20 / 28 | 600 | -0.015em | card-group / section headers |
| `text-title` (retune) | 18 / 26 | 600 | -0.01em | sub-section headers |
| `text-card-title` *(new)* | 16 / 24 | 600 | -0.008em | card titles |
| `text-body` | 14 / 22 | 400 | -0.003em | default UI |
| `text-body-strong` *(new)* | 14 / 22 | 500 | -0.003em | emphasized cells, labels |
| `text-caption` *(new)* | 12 / 16 | 400 | 0 | secondary metadata |
| `text-eyebrow` (retune) | 11 / 14 | 600 | 0.06em UPPER | table headers, stat labels, nav groups |
| `text-stat` *(new)* | 28 / 32 | 600, `tabular-nums` | -0.02em | KPI values **only** |
| `text-mono` *(new)* | 13 / 20 | 450 Geist Mono | 0 | all machine values |

Set `--font-heading` to Geist with `font-feature-settings:"ss01","cv11"` and tighter tracking so headings differ from body. Add `"tnum"` to table cells.

#### Spacing scale (new — currently none)

4px base: `--space-0:0 / 1:4 / 2:8 / 3:12 / 4:16 / 5:20 / 6:24 / 8:32 / 10:40 / 12:48 / 16:64`. Conventions: card pad `--space-6`; table row-y `--space-3` (dense) / `--space-4` (comfortable); section gap `--space-8`; field gap `--space-4`. Container widths: dense lists/logs `max-w-6xl`, forms/detail `max-w-3xl`, auth/empty prose `max-w-md`; gutter `px-6 lg:px-8`.

#### Elevation scale (new)

| Token | Light | Dark |
|---|---|---|
| `--elevation-0` | none (border only) | none |
| `--elevation-1` | `0 1px 2px oklch(0.2 0.02 285 / .06)` | `inset 0 1px 0 oklch(1 0 0 / .04)` |
| `--elevation-2` | `0 1px 2px oklch(.2 .02 285 / .06), 0 2px 8px oklch(.2 .02 285 / .05)` | `inset 0 1px 0 oklch(1 0 0 / .05), 0 4px 12px oklch(0 0 0 / .35)` |
| `--elevation-3` | `0 4px 12px oklch(.2 .02 285 / .08), 0 12px 32px oklch(.2 .02 285 / .08)` | `inset 0 1px 0 oklch(1 0 0 / .06), 0 12px 32px oklch(0 0 0 / .5)` |

Cards `--elevation-1`, hover → `--elevation-2`; popovers/palette `--elevation-3`. Dark mode lifts via lightness step + top-inset highlight, never a bare drop shadow.

#### Radii

Tighten base for the precision feel: `--radius: 0.5rem` (8px, down from 0.625). The existing `calc()` scale (lines 52–58) recomputes automatically: `sm ~5 / md ~6 / lg 8 / xl ~11 / 2xl ~14`. Add `--radius-badge: 4px` (status pills read "data," not "label"). Inputs/buttons `md`, cards `lg`, dialogs/palette `xl`.

#### Motion / easing (new)

```
--ease-standard: cubic-bezier(0.2, 0, 0, 1)
--ease-out:      cubic-bezier(0.16, 1, 0.3, 1)
--ease-spring:   cubic-bezier(0.34, 1.56, 0.64, 1)   /* palette/toast pop only */
--duration-fast: 110ms   --duration-base: 170ms   --duration-slow: 240ms
```
Standard recipe: `transition: color, background-color, border-color, box-shadow, transform var(--duration-fast) var(--ease-standard)`. Envelope keyframes reserved for auth/first-paint only — never `h-screen` inside the shell.

### 3.3 Component restyle specs

- **Button** (`button.tsx`): add first-class `loading?: boolean` → disables, sets `aria-busy`, swaps/leads with `animate-spin` Loader2 sized per variant. Primary uses `--brand-600`/hover `--brand-700`; dark primary now AA. Keep the `active:translate-y-px`. Add `iconStart`/`iconEnd` convention.
- **Input / Select / Textarea** (`input.tsx`, `select.tsx`, `textarea.tsx`): the `aria-invalid` ring styling already exists and is unused — wire a tiny `FormField` (Label + control + `FormMessage` in `text-destructive text-caption`) so errors surface inline, not only as toasts. Replace native `<select>`/`<input type=checkbox>`/`<input type=color>` in form-editor/template-builder with shadcn Select + a new Switch/Checkbox + a swatch+hex pair. `--input` uses `--border-strong` on focus.
- **Card** (`card.tsx`): `--elevation-1` default, `--elevation-2` on hover; drop the `ring-foreground/[0.08]` crutch — the value gap (off-white bg vs white card / lighter dark card) now does separation. Optional header rule via `border-b`. `CardTitle` → `text-card-title`.
- **Table** (`table.tsx`): the cockpit core. Eyebrow uppercase headers under one `--border-strong` rule; row hover `bg-muted`; numeric/time/ID columns right-aligned, `font-mono tabular-nums`; **2px left status bar** + faint `*-tint` row-wash on terminal-failure rows; optional zebra (`--surface`) and a `density` prop (`--space-3`/`--space-4`). Becomes the shared `DataTable`.
- **Badge / StatusBadge** (`badge.tsx` + fold in `status-badge.tsx`): add `success/warning/info/destructive/pending` tonal variants (`bg-*-tint text-*-on` + leading dot), `--radius-badge`. Refactor `StatusBadge` to render `Badge` with the mapped variant — one pill system, AA-compliant, consistent geometry. Add one `brand` variant (`bg-brand-100 text-brand-800`) reserved for plan/primary-domain.
- **Dialog / Sheet** (`dialog.tsx`, `sheet.tsx`): keep the polished portals; swap the hard-coded `ring-foreground/10` for `--elevation-3`; enter/exit on `--ease-out`/`--duration-slow`. Sheet doubles as the mobile sidebar drawer.
- **Sidebar** (`dashboard-sidebar.tsx`): adopt shadcn `Sidebar` (collapsible, cookie-persisted, mobile `Sheet`). Grouped nav under `text-eyebrow` labels; active item = **2px `--brand-500` accent spine + `--brand-100` fill + `--brand-800` label**. Brand-tinted avatar fallback. Scroll-fade masks top/bottom.
- **PageHeader** (`page-header.tsx`): the only title source — `text-display` + `text-caption` description + action slot. Delete all inline `text-2xl`/`text-xl` h1s across the 9 offending files.
- **EmptyState** (`empty-state.tsx`): brand `/10` icon halo, `text-section` title, `text-caption` copy, one primary CTA; reuse for invalid-invite and filtered-empty states.
- **Toasts** (`sonner.tsx`): keep semantic icons; retarget to `*-tint`/`*-on` so toast colors match badges; enter on `--ease-spring`.
- **Skeleton / Progress** (`skeleton.tsx`, `progress.tsx`): Skeleton → tinted left-to-right shimmer with shape presets (line/avatar/card); Progress → `h-1.5`, `--brand` indicator with contrast track.
- **`<Stat>` (new):** eyebrow label / `text-stat` `tabular-nums` value / signed delta (success-or-destructive) / 40px sparkline (`chart-1`). Exactly one cell per strip gets the brand accent + brand spark.

### 3.4 App-shell & layout

- **Sidebar IA** (grouped, replaces the flat 12-item list): **Send** — Overview, Broadcasts, Templates, SMTP · **Audience** — Audiences, Forms, Suppressions · **Deliverability** — Domains, Webhooks, Analytics, Logs · **Settings** pinned bottom near the account control. Org-switcher moves to the top bar.
- **Header** (new): slim sticky `h-12`, `border-b`, `bg-background/80 backdrop-blur`. Left: breadcrumbs derived from route segments. Center: ⌘K command input (always looks like an input). Right: deliverability health dot (green/amber/red from recent bounce/complaint rate → popover), theme toggle (compact icon, not a full-width row), account menu (avatar + name + chevron, holds sign-out + theme).
- **Content rhythm:** `max-w-6xl` lists / `max-w-3xl` forms, `px-6 lg:px-8`, section gap `--space-8`, cards `--space-6`. Dashboard header band carries the single `--brand-50→transparent` radial wash.
- **Responsive:** rail `hidden lg:flex`; below `lg` a top bar with hamburger opens the existing `Sheet` with the same nav. `main` → `lg:ml-[var(--sidebar-w)] ml-0`. (Today: hard `fixed w-64` + `ml-64`, zero breakpoints.)

### 3.5 Page patterns (reusable)

- **List page:** `PageHeader` → filter bar (search + status/domain chips, active chip = `brand-tint`) → `DataTable` (eyebrow headers, mono right-aligned cols, status rows, sortable time/status) → mono pagination. Card-grid reserved only for resources with a visual/preview dimension (templates, forms); flat lists (domains, audiences, logs, broadcasts, suppressions) use the table.
- **Detail page:** `PageHeader` with breadcrumb → grouped sections under `text-section`/`text-eyebrow` (e.g. SMTP: Overview / Settings / Testing / API keys / Logs), not a flat stack of 6 cards. Two-column where info + testing pair.
- **Form page:** `max-w-3xl`, `FormField` stack with inline `aria-invalid` + `FormMessage`, Switch for booleans, shadcn Select, primary button with `loading`. Two-column live-preview pattern (already strong) retained.
- **Metrics/analytics:** `<Stat>` strip (one brand-marked primary KPI) → real Recharts chart (gridlines, hover tooltip date+value, toggleable sent/failed/opens/clicks on the re-keyed chart tokens). No decorative SVG.
- **Logs:** the showcase — `DataTable` with status spine + failure row-wash, mono columns, density toggle, row-click → right `Sheet` detail drawer (timeline queued→sent→bounced with the SMTP 5xx response in mono, headers, raw JSON, retry). Layout-matched skeleton on load.

### 3.6 Mockups

**Dashboard home**
```
┌────────────────────┬──────────────────────────────────────────────────────────────┐
│ ◐ LaunchMail  ⌄prod │ Overview                       [ ⌘K  Search… ]   ● ☾  ◐ DA ▾  │ h-12 sticky, blur
│                     ├──────────────────────────────────────────────────────────────┤ breadcrumb L · palette C · health-dot R
│ SEND     (eyebrow)  │ ╭─ brand-50 → transparent wash ─────────────────────────────╮ │
│ ▌▣ Overview  ⌘1     │ ┃ Overview                                    [ + Send email ]┃ │ text-display · primary=brand-600
│   ◇ Broadcasts      │ ┃ Your sending at a glance · last 7 days                      ┃ │ text-caption
│   ◇ Templates       │ ╰─────────────────────────────────────────────────────────── ╯ │
│   ◇ SMTP            │ ┌──────────┬──────────┬──────────┬──────────┬──────────┐       │ <Stat> strip, one bordered row
│ AUDIENCE            │ │DELIVERED │ FAILED   │ OPEN RATE│ BOUNCED  │ QUEUED   │       │ eyebrow labels
│   ◇ Audiences       │ │ ▌12,201  │   312    │  41.2%   │  0.4%    │   140    │       │ text-stat tabular-nums
│   ◇ Forms           │ │ ▲4.1% ∿  │ ▼0.6% ∿  │ ▲1.2pt ∿ │ ▲0.1pt ∿ │  —    ∿  │       │ ▌brand accent on primary KPI only
│   ◇ Suppressions    │ └──────────┴──────────┴──────────┴──────────┴──────────┘       │
│ DELIVERABILITY      │ ┌─ Sending volume (7d) ── sent · failed · opens · clicks ─────┐ │ Recharts, brand stroke
│   ◇ Domains  ✓      │ │      ╱╲       ╱╲         gridlines + hover tooltip (date,n) │ │ chart-1 = brand
│   ◇ Webhooks        │ │  ╱╲ ╱  ╲╱╲  ╱  ╲___                                          │ │
│   ◇ Analytics       │ └────────────────────────────────────────────────────────────┘ │
│ ▌◇ Logs       ⌘L    │ ┌─ Recent activity ──────────────────────── View all logs → ─┐ │ DataTable (compact)
│ ──────────────────  │ │ STATUS     TO              SUBJECT        DOMAIN    TIME    │ │ eyebrow header, mono R-aligned
│ ◇ Settings    ⌘,    │ │▌●delivered ana@acme.io     Welcome to…    acme.io  12:04:11 │ │ ▌status spine
│ ◐ DA  danila@…  ⌄   │ │ ●bounced   x@deadmx.net    Receipt #4821  acme.io  12:01:02 │ │ destructive tint row-wash
└────────────────────┴──┴────────────────────────────────────────────────────────────┘
 240px rail, lit edge        max-w-6xl, px-8 · cards --elevation-1, radius-lg, --space-6
```

**Logs (the credibility surface)**
```
┌──────────────────────────────────────────────────────────────────────────────────┐
│ Deliverability › Logs                        [ ⌘K  Search… ]   ●  ☾   ◐ DA ▾        │
├──────────────────────────────────────────────────────────────────────────────────┤
│ ╭ brand-50 wash ───────────────────────────────────────────────────────────────── │
│ ┃ Logs                                                          [ ⤓ Export CSV ]    │ text-display
│ ┃ Every message your SMTP transport has handled              ⟳ refreshed 8s ago     │ text-caption · mono stamp
│ ╰────────────────────────────────────────────────────────────────────────────────  │
│ [All ▾] [Status: ●Failed ✕] [Domain: acme.io ✕]  🔎 recipient, subject, msg-id…    │ filter chips (active=brand-tint)
│ ──────────────────────────────────────────────────────────────────────────────────│ --border-strong rule
│ STATUS▾    RECIPIENT          SUBJECT            MESSAGE-ID     ATTEMPTS   SENT ▾    │ eyebrow headers, sortable
│ ──────────────────────────────────────────────────────────────────────────────────│
│▌●bounced   x@deadmx.net       Password reset     m_9f2a…1c     3          12:01:02  │ ▌destructive spine + tint wash
│▌◐deferred  q@slowmx.org       Receipt #4822      m_9f2a…22     2          11:58:40  │ warning spine
│ ●delivered ana@acme.com       Welcome to Acme    m_8f3a…b1     1          12:04:11  │ row hover → muted; click → drawer
│ ●opened    joe@globex.com     Weekly digest      m_8f39…c0     1          11:58:02  │ mono id/attempts/time, R-aligned
│ ●clicked   sam@umbrella.co    Confirm email      m_8f37…04     1          11:44:07  │
│ ●queued    pat@initech.com    Invoice March      m_8f26…91     0          11:39:20  │ pending = neutral
│ ──────────────────────────────────────────────────────────────────────────────────│
│ Rows 1–50 of 12,480       Density [ Compact ◐ Comfortable ]      ‹ Prev  1 2 3  Next ›│ mono counts · j/k/⏎ keys
└──────────────────────────────────────────────────────────────────────────────────┘
 Pills (tonal, AA):  ●delivered=success  ◐opened/sent=info  ◐deferred=warning  ●bounced/failed=destructive  ●queued=pending
 Loading: skeleton header rule + 8 shimmer rows at exact row height (never the envelope spinner inside the shell)
```

### 3.7 Brand: landing, auth, logo/loader

- **Landing (`/`):** replace the bare redirect with a real page (authed users still redirect). Hero = `Logo` + one-line value prop + primary CTA "Get started" / secondary "View on GitHub", over a `--brand-50→transparent` mesh. A product screenshot/animated dashboard mock, a 4-card feature grid (BYO SMTP · Forms · Broadcasts · MCP server), the **orphaned `TechStack` strip** (now wired in, `flex-wrap`) as a credibility band, footer.
- **Auth:** keep the split-screen, but drive the panel gradient from `--brand` (not the hard-coded `rgba(99,102,241)` / `rgba(56,189,248)` in `(auth)/layout.tsx`). Add "Forgot password?", show/hide password toggle, inline `aria-invalid`, brand focus ring; scaffold GitHub/Google + divider if SSO is roadmapped.
- **Invite (`/invite/[id]`):** add a layout that reuses the auth split-screen + `Logo` + brand wash; show inviter org name/avatar; distinct invalid/expired empty-state.
- **Metadata:** add root `metadata` (title template `%s · LaunchMail`, description), `opengraph-image.tsx` (envelope mark + positioning on brand gradient), envelope `icon.svg`/`apple-icon`, per-route titles.
- **Logo/loader:** two canonical sizes (`LogoMark` vs full `Logo`), allow brand-color render where appropriate; de-dupe the two identical `loading.tsx` envelope entry points; envelope-draw animation reserved for auth + first dashboard paint only.

## 4. Implementation plan (phased, file-by-file — no code written now)

**Phase 1 — Tokens (single file, ~80% of the visual lift).** Touch only `packages/ui/src/styles/globals.css`: rewrite `:root` + `.dark` neutrals to hue-285+chroma (matched); add brand `50–950` ramp; add `*-tint`/`*-on` semantic triples + `pending`; re-key `chart-1..5`; retune sidebar tokens; add `--surface`, `--border-strong`, spacing, elevation, motion vars + `--radius: 0.5rem` + `--radius-badge`; extend `@theme inline` (lines 10–60) with `--color-brand-N`, `--color-*-tint/-on`, `--color-surface`, `--color-border-strong`; replace the `@utility` block (167–175) with the full type ramp (`text-display/section/title/card-title/body/body-strong/caption/eyebrow/stat/mono`) and retarget `--font-heading`. *Outcome: the app already looks redesigned with zero component edits.*

**Phase 2 — Core components (`packages/ui/src/components/`).** `button.tsx` (loading prop). `input.tsx`/`select.tsx`/`textarea.tsx` (focus uses `--border-strong`) + new `form-field.tsx`/`form-message.tsx` + new `switch.tsx`/`checkbox.tsx`. `card.tsx` (elevation, drop ring). `badge.tsx` (tonal + brand variants). `table.tsx` (eyebrow headers, status spine, density, mono) → new `data-table.tsx`. `dialog.tsx`/`sheet.tsx` (elevation, easing). `skeleton.tsx` (shimmer + presets), `progress.tsx` (height/contrast), `sonner.tsx` (tint retarget). New `stat.tsx`, `command.tsx` (⌘K).

**Phase 3 — App-shell (`apps/web/`).** `app/(dashboard)/layout.tsx` (responsive `lg:ml`, mount header). New `components/app-header.tsx` (breadcrumbs + ⌘K + health-dot + theme + account). `components/dashboard-sidebar.tsx` → shadcn `Sidebar` + grouped IA + accent spine + mobile `Sheet`. `components/account-dropdown.tsx` (consolidate theme + sign-out; brand avatar), `components/theme-toggle.tsx` (compact icon), `components/org-switcher.tsx` (move to header), `components/envelope-loader.tsx` (drop `h-screen`).

**Phase 4 — Pages.** Refactor `components/status-badge.tsx` onto `Badge`. Migrate to `DataTable`: `dashboard/logs/page.tsx`, `dashboard/broadcasts/page.tsx`, `dashboard/suppressions/suppressions-manager.tsx`, `dashboard/organization/page.tsx`, `dashboard/page.tsx` (recent activity). Replace `components/area-chart.tsx` with Recharts in `dashboard/analytics/page.tsx` + `dashboard/page.tsx`. Consolidate the 3 Metric/StatCard copies onto `<Stat>`. Route all titles through `PageHeader` (delete inline h1s in `login`, `sign-up`, `(auth)/layout`, `smtp/new`, `smtp/[id]`, `forms/[id]`, `forms/new`, `logs`, `dashboard`, `analytics`). Replace native controls + sweep raw `green/red/emerald/amber-NNN` → semantic tokens in `smtp/page.tsx`, `smtp/[id]/*`, `forms/[id]/form-editor.tsx`, `templates/template-builder.tsx`. Add layout-matched `loading.tsx` to every async route.

**Phase 5 — Marketing/auth.** New `app/page.tsx` landing + `app/opengraph-image.tsx` + root `metadata` in `app/layout.tsx` + `icon.svg`/`apple-icon`. Wire `components/tech-stack.tsx` (add `flex-wrap`). Brand-token gradient in `app/(auth)/layout.tsx`; enrich `login/page.tsx` + `sign-up/page.tsx`. New `app/invite/layout.tsx` reusing the split-screen; brand the invalid state. Add Recharts as the only new dependency. No second font.

Key files for the implementer: tokens — `/home/anakin/programming/launchday/launchmail/packages/ui/src/styles/globals.css`; primitives — `/home/anakin/programming/launchday/launchmail/packages/ui/src/components/`; shell — `/home/anakin/programming/launchday/launchmail/apps/web/app/(dashboard)/layout.tsx` and `/home/anakin/programming/launchday/launchmail/apps/web/components/dashboard-sidebar.tsx`; logs surface — `/home/anakin/programming/launchday/launchmail/apps/web/app/(dashboard)/dashboard/logs/page.tsx`; fonts/metadata — `/home/anakin/programming/launchday/launchmail/apps/web/app/layout.tsx`.
