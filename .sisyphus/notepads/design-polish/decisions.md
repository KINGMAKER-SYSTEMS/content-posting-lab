# Design Decisions

## Color Palette
- **Background**: `#0f0f0f` (Charcoal) - Provides a deep, modern dark mode base.
- **Accent**: `purple-500` - Used for primary actions, active states, and focus rings.
- **Text**: `gray-300` (primary), `gray-400` (secondary), `white` (headings).
- **Borders**: `white/10` - Subtle borders for separation without harsh lines.
- **Panels**: `black/20` - Semi-transparent backgrounds for panels and cards.

## Typography
- **Font Stack**: System font stack (San Francisco, Segoe UI, Roboto, etc.) for optimal performance and native feel.
- **Headings**: `font-semibold`, `tracking-tight`, `text-white`.
- **Body**: `text-gray-300`, `antialiased`.

## Components
- **Buttons**:
  - Primary: `bg-purple-600` with hover `bg-purple-500`.
  - Secondary: `bg-white/5` with hover `bg-white/10`.
  - Danger: `bg-red-600` with hover `bg-red-700`.
- **Inputs**: `bg-black/20`, `border-white/10`, `focus:ring-purple-500/50`.
- **Cards**: `bg-white/5`, `border-white/10`, `rounded-xl`, `backdrop-blur-sm`.
- **Modals**: `bg-slate-900`, `border-white/10`, `backdrop-blur-sm` overlay.

## Layout
- **Navigation**: Sticky top bar with `backdrop-blur-md` for context awareness.
- **Sidebar**: Fixed width (`400px`) on large screens, collapsible on mobile (future improvement).
- **Main Content**: Centered with `max-w-7xl` or `max-w-6xl` for readability.
