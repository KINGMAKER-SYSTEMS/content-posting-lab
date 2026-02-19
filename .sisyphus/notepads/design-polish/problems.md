# Design Problems

## Unresolved Issues
- **Mobile Responsiveness**: The sidebar layout might need further refinement for smaller screens.
- **Accessibility**: Contrast ratios for some text colors might need adjustment.
- **Performance**: Heavy use of `backdrop-blur` might impact performance on older devices.
- **Consistency**: Some components might still have slight variations in padding or margins.

## Technical Debt
- **Tailwind Config**: The project lacks a `tailwind.config.js` file, relying on default theme or implicit configuration.
- **Component Reusability**: Some components (e.g., `TabNav`) are duplicated or tightly coupled.
- **State Management**: `workflowStore` is used extensively, but some local state could be moved to context or props.
