import { describe, it, expect } from 'vitest';
import { render } from '@testing-library/react';
import App from './App';

describe('App Component', () => {
  it('renders without crashing', () => {
    render(<App />);
    expect(document.body).toBeTruthy();
  });

  it('renders the main app container', () => {
    const { container } = render(<App />);
    expect(container.firstChild).toBeTruthy();
  });
});
