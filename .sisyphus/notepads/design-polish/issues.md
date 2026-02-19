# Design Issues

## Encountered Issues
- **Missing Tailwind Config**: The project uses Tailwind v4, which doesn't require a `tailwind.config.js` file by default, but it makes customization slightly different.
- **Component Duplication**: `TabNav` was defined in both `App.tsx` and `components/TabNav.tsx`. I updated `App.tsx` as it was the one being used.
- **Mock Data**: `Projects.tsx` uses mock data for stats, which might not reflect real usage.
- **API Dependencies**: Some components rely on API calls that might fail if the backend is not running or configured correctly.

## Solutions
- **Tailwind v4**: Used `@theme` and utility classes directly in `index.css` and components.
- **Component Refactoring**: Updated `App.tsx` to use the new design system directly.
- **Mock Data**: Kept mock data for now, but added comments about future integration.
- **Error Handling**: Added `ErrorBoundary` and `EmptyState` components to handle missing data or errors gracefully.
