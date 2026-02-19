# Design System Update

## Overview
Applied a consistent dark mode design system across the frontend application.

## Key Changes
- **Color Palette**:
  - Background: Charcoal (`#0f0f0f`)
  - Accent: Purple (`purple-500`)
  - Text: Gray (`gray-300`, `gray-400`)
  - Borders: White with low opacity (`white/10`)
  - Panels: Black with low opacity (`black/20`)

## Components Updated
- **Global Styles**: Updated `index.css` with base styles and utility classes (`.btn`, `.card`, `.input`, `.label`).
- **Layout**: Updated `App.tsx` with a sticky navigation bar and consistent container widths.
- **Pages**:
  - `Generate.tsx`: Updated form inputs, buttons, and video cards.
  - `Captions.tsx`: Updated form inputs, buttons, logs console, and result items.
  - `Burn.tsx`: Updated form inputs, buttons, preview area, and result items.
  - `Projects.tsx`: Updated project cards, stats grid, and modal.
- **UI Components**:
  - `EmptyState.tsx`: Updated text colors and button styles.
  - `StatusChip.tsx`: Updated background and text colors for status indicators.
  - `Toast.tsx`: Updated notification styles with transparency and borders.
  - `ConfirmModal.tsx`: Updated modal background and button styles.
  - `FileBrowser.tsx`: Updated grid item styles and hover effects.
  - `ProgressBar.tsx`: Updated progress bar colors.
  - `ErrorBoundary.tsx`: Updated error state styles.

## Technical Details
- Used Tailwind CSS v4 features.
- leveraged `backdrop-blur` for glassmorphism effects.
- Used `white/alpha` and `black/alpha` for adaptable dark mode layers.
