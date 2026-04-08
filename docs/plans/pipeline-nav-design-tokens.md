# Design Tokens: Pipeline Navigation Restructure

Reference for all UI components in this restructure. Built on existing neobrutalism theme — no new CSS variables, no new component libraries.

## Existing Theme (DO NOT CHANGE)

```css
--background: oklch(96.5% 0.012 80)    /* warm parchment */
--foreground: oklch(20% 0.01 50)        /* warm near-black */
--card: oklch(100% 0 0)                 /* white */
--primary: oklch(68% 0.1 40)            /* terra cotta */
--secondary: oklch(84% 0.04 220)        /* dusty blue */
--muted: oklch(92% 0.01 75)             /* warm muted bg */
--accent: oklch(52% 0.06 230)           /* deep slate blue */
--border: oklch(20% 0.01 50)            /* warm near-black */
--border-radius: 5px
--shadow: 4px 4px 0px 0px var(--border) /* hard neobrutalism shadow */
```

## Existing Components Available

From `shadcn/ui`: Badge, Button, Card/CardContent, Dialog, DropdownMenu, Input, Label, Progress, ScrollArea, Select, Separator, Textarea.

Custom: ConfirmModal, EmptyState, ErrorBoundary, FileBrowser, LazyVideo, ProgressBar, ProjectSelector, StatusChip, TabNav, Toast, ToastContainer.

---

## Top Navigation Bar

### Layout

```
┌─────────────────────────────────────────────────────────────┐
│  [Logo]  Content Posting Lab                                │
│                                                             │
│  Home    Create    Captions    Distribute   [ProjectSel ▾]  │
└─────────────────────────────────────────────────────────────┘
```

Single `<header>` block. Two rows inside:
- Row 1: Logo + app name (left), project selector (right)
- Row 2: Pipeline tabs (left-aligned)

### Header Container
```
className="border-b-2 border-border bg-card"
inner: "mx-auto max-w-7xl px-4 py-3"
```

### Pipeline Tab (inactive)
```
className="px-4 py-3 text-sm font-bold text-muted-foreground
           hover:text-foreground hover:bg-muted transition-colors"
```

### Pipeline Tab (active)
```
className="px-4 py-3 text-sm font-bold text-primary
           border-b-[3px] border-primary -mb-[2px]"
```

### Tab Badge (count)
```
<Badge variant="secondary" className="text-[10px] px-1.5 py-0 shadow-none">
  {count}
</Badge>
```

### Tab Badge (live)
```
<Badge variant="success" className="text-[10px] px-1.5 py-0 shadow-none">
  LIVE
</Badge>
```

### Aggregate Badge Logic
- **Create tab:** Show badge if `videoRunningCount > 0` OR `recreateJobActive`. Show "LIVE" if `recreateJobActive`, else show count.
- **Captions tab:** Show badge if `captionJobActive` OR `burnReadyCount > 0`. Show "LIVE" if `captionJobActive`, else show `burnReadyCount`.
- **Distribute tab:** No badge (or future: upload queue count).

---

## Sub-Tab Navigation

Rendered inside each stage page, below the main nav. Same pattern as Distribution.

### Container
```
className="sticky top-[53px] z-30 border-b-2 border-border bg-card"
inner: "mx-auto flex max-w-7xl gap-1 px-4"
```

Note: `top-[53px]` accounts for main nav height. The main nav is `sticky top-0 z-40`. Sub-tabs stick below it at `z-30`.

### Sub-Tab Item (inactive)
```
className="px-4 py-2.5 text-sm font-bold text-muted-foreground
           hover:text-foreground hover:bg-muted transition-colors
           cursor-pointer"
```

### Sub-Tab Item (active)
```
className="px-4 py-2.5 text-sm font-bold text-primary
           border-b-[3px] border-primary -mb-[2px]"
```

---

## Home Page Components

### Page Container
```
className="mx-auto max-w-7xl p-6 space-y-6"
```

### Section: Project Summary Card

Full-width card at top of Home.

```
<Card className="border-primary shadow-[4px_4px_0_0_var(--primary)]">
  <CardContent className="pt-0">
    <!-- project name, stats grid, actions -->
  </CardContent>
</Card>
```

#### Project Name
```
className="text-2xl font-heading text-foreground"
```

#### Stats Grid (inside project card)
```
className="grid grid-cols-4 gap-3"
```

Each stat cell:
```
className="rounded-[var(--border-radius)] border-2 border-border bg-muted p-3 text-center"
```

Stat value:
```
className="text-2xl font-heading text-foreground"
```

Stat label:
```
className="text-[10px] font-bold uppercase tracking-wider text-muted-foreground"
```

Stats to show: Videos | Clips | Captions | Burned

#### Project Actions (inside card)
```
<Button variant="outline" size="sm">Switch Project</Button>
<Button variant="outline" size="sm">+ New Project</Button>
```

### Section: Pipeline Status

#### Section Header
```
className="text-sm font-bold uppercase tracking-wider text-muted-foreground"
```
Text: "Pipeline Status"

#### Status Row (something running)
```
className="flex items-center gap-3 rounded-[var(--border-radius)] border-2 border-border
           bg-card p-3 shadow-[2px_2px_0_0_var(--border)]"
```

Inside: pulsing dot + description text + link to stage.

#### Pulsing Dot (active)
```
className="h-2.5 w-2.5 rounded-full bg-green-500 animate-pulse"
```

#### Status Text
```
className="text-sm font-base text-foreground"
```

#### Empty State (nothing running)
```
className="rounded-[var(--border-radius)] border-2 border-dashed border-border
           bg-muted p-6 text-center"
```
```
className="text-sm text-muted-foreground"
```
Text: "Nothing running — start by creating content"

### Section: Recent Activity

#### Activity Item
```
className="flex items-center gap-3 rounded-[var(--border-radius)] border-2 border-border
           bg-card p-3 hover:bg-muted transition-colors cursor-pointer
           shadow-[2px_2px_0_0_var(--border)]"
```

#### Thumbnail (if available)
```
className="h-10 w-10 rounded-[var(--border-radius)] border-2 border-border
           object-cover bg-muted"
```

#### Activity Description
```
className="text-sm font-base text-foreground flex-1"
```

#### Activity Timestamp
```
className="text-xs text-muted-foreground whitespace-nowrap"
```

### Section: Quick Launch Grid

```
className="grid grid-cols-2 gap-4 lg:grid-cols-4"
```

#### Quick Launch Card
```
className="group rounded-[var(--border-radius)] border-2 border-border bg-card p-6
           shadow-[var(--shadow)] cursor-pointer
           hover:translate-x-[1px] hover:translate-y-[1px]
           hover:shadow-[3px_3px_0_0_var(--border)] transition-all"
```

#### Card Title
```
className="text-lg font-heading text-foreground group-hover:text-primary transition-colors"
```

#### Card Description
```
className="text-xs text-muted-foreground mt-1"
```

#### Card Icon Area
Large icon or emoji at top of card:
```
className="text-3xl mb-3"
```

Quick Launch cards:
| Card | Icon | Title | Description | Route |
|------|------|-------|-------------|-------|
| 1 | film/video icon | Generate Video | AI-powered video creation | `/create` |
| 2 | scissors icon | Clip Video | Chop videos into short-form clips | `/create/clipper` |
| 3 | text/caption icon | Scrape Captions | Extract captions from TikTok | `/captions` |
| 4 | layers icon | Burn Captions | Overlay captions onto videos | `/captions/burn` |

---

## Stage Wrapper Components (Create, CaptionsStage)

### Pattern

```tsx
// Thin wrapper — holds sub-tab nav + CSS display toggling
export function CreatePage() {
  const location = useLocation();
  const navigate = useNavigate();

  const subTabs = [
    { path: '/create', label: 'Generate' },
    { path: '/create/clipper', label: 'Clipper' },
    { path: '/create/recreate', label: 'Recreate' },
    { path: '/create/slideshow', label: 'Slideshow' },
  ];

  return (
    <div>
      {/* Sub-tab nav */}
      <nav className="sticky top-[53px] z-30 border-b-2 border-border bg-card">
        <div className="mx-auto flex max-w-7xl gap-1 px-4">
          {subTabs.map(tab => (
            <button
              key={tab.path}
              onClick={() => navigate(tab.path)}
              className={/* active/inactive styles from above */}
            >
              {tab.label}
            </button>
          ))}
        </div>
      </nav>

      {/* CSS display toggling — all children stay mounted */}
      <div style={{ display: location.pathname === '/create' ? 'block' : 'none' }}>
        <GeneratePage />
      </div>
      {/* ... other sub-tabs ... */}
    </div>
  );
}
```

### Sub-Tab Matching

For stages with sub-routes, the default sub-tab matches the base path exactly:
- `/create` → Generate (default)
- `/create/clipper` → Clipper
- `/captions` → Scrape (default)
- `/captions/burn` → Burn
- `/distribute` → Roster (default)

### Top-Level Tab Active Detection

A top-level tab is active when the pathname starts with its base:
- Home: `pathname === '/'`
- Create: `pathname.startsWith('/create')`
- Captions: `pathname.startsWith('/captions')`
- Distribute: `pathname.startsWith('/distribute')`

---

## Responsive Notes

- Desktop only (existing constraint)
- `max-w-7xl` container throughout (existing pattern)
- Grid columns: 4 for quick launch on `lg`, 2 on smaller
- Stats grid: 4 columns with `grid-cols-4`

## Animation

- Badge pulse: existing `animate-pulse` on LIVE badges
- Card hover: `transition-all` with translate + shadow shift (existing pattern)
- No new animations

## Typography

- Headings: `font-heading` (weight 800)
- Body: `font-base` (weight 500)
- Labels: `text-[10px] font-bold uppercase tracking-wider`
- All existing — no new font weights or sizes
