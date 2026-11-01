---
name: frontend-tests
description: 'Use when adding, running, debugging, or reviewing tests for the React and TypeScript dashboard, components, API interactions, loading/error states, or Vite frontend.'
---

# Frontend Testing

## Current Test Setup

The frontend is React 18 + TypeScript + Vite with Vitest, React Testing Library, and `@testing-library/user-event`. Tests live in `frontend/tests/` and use jsdom via `frontend/vite.config.ts`.

```sh
cd frontend && npm test        # single run (used by CI and ./run_tests.sh)
cd frontend && npm run test:watch
```

## Adding Tests

Follow the existing component-test patterns and mock the API boundary (`src/api.ts`) or heavy visualization libraries rather than calling a live backend. Keep new infrastructure changes explicit and consistent with the current Vite/TypeScript setup.

Prefer tests that exercise observable dashboard behavior:

- Render loading, error, empty, and populated states.
- Verify user actions call the intended API operation and update or invalidate query data.
- Check visible formatting and interactions for signals, trades, stats, and forecast panels.
- Mock the API boundary in `src/api.ts`; avoid live backend or third-party network calls.

Keep tests close to the component or feature they cover, use accessible roles/names for queries, and avoid asserting implementation details such as internal state or exact class lists. For contract changes, also verify the corresponding FastAPI response model and TypeScript types.

## Verification

- Run `cd frontend && npm test` for the suite.
- Run `cd frontend && npm run lint` and `cd frontend && npm run format:check` for static checks.
- Run `cd frontend && npm run build` for the strict TypeScript check plus Vite production build.
- There is no browser end-to-end test runner; do not report one as run unless the project adds it.
