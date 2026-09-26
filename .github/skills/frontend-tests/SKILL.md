---
name: frontend-tests
description: 'Use when adding, running, debugging, or reviewing tests for the React and TypeScript dashboard, components, API interactions, loading/error states, or Vite frontend.'
---

# Frontend Testing

## Current Test Setup

The frontend is React 18 + TypeScript + Vite, but `frontend/package.json` currently defines only `dev`, `build`, and `preview`; there is no frontend test runner, test script, or existing frontend test suite. Do not claim that `npm test` works or that a build is a test run.

For the current checkout, the available frontend verification is:

```sh
cd frontend && npm run build
```

This runs strict TypeScript checking followed by a Vite production build.

## Adding Tests

When the task includes introducing frontend test infrastructure, use Vite-compatible Vitest with React Testing Library and `@testing-library/user-event` for component behavior. Add the required development dependencies, a test script, and a minimal configuration consistent with the existing Vite/TypeScript setup. Keep that infrastructure change explicit; do not assume these packages are already installed.

Prefer tests that exercise observable dashboard behavior:

- Render loading, error, empty, and populated states.
- Verify user actions call the intended API operation and update or invalidate query data.
- Check visible formatting and interactions for signals, trades, stats, and forecast panels.
- Mock the API boundary in `src/api.ts`; avoid live backend or third-party network calls.

Keep tests close to the component or feature they cover, use accessible roles/names for queries, and avoid asserting implementation details such as internal state or exact class lists. For contract changes, also verify the corresponding FastAPI response model and TypeScript types.

## Verification

- Existing check: `cd frontend && npm run build`.
- After adding a test runner, document and run its exact command (for example `npm test`) alongside the build.
- The frontend currently has no configured lint command or browser end-to-end test runner; do not report either as run unless the project adds one.
