---
name: frontend
description: 'Use when changing or debugging the React and TypeScript dashboard, Vite configuration, API client, dashboard components, charts, maps/globe, styling, or frontend build.'
---

# Frontend Engineering

## Architecture Map

- `frontend/src/App.tsx` composes the dashboard, polls dashboard data with TanStack Query, and owns scan/trade/bot mutations.
- `frontend/src/api.ts` is the Axios client and API endpoint boundary; `frontend/src/types.ts` defines the backend-facing TypeScript contracts.
- `frontend/src/components/` contains the dashboard panels and visualizations. `GlobeView` is lazy-loaded from `App.tsx`.
- `frontend/src/index.css` contains global styling and Tailwind is configured through `tailwind.config.js` and `postcss.config.js`.
- Vite serves the frontend on port `3000` and proxies `/api` to `http://localhost:8000` in development. `VITE_API_URL` overrides the API base URL.

## Working Guidelines

1. Keep API calls in `src/api.ts` and shared payload shapes in `src/types.ts`; when a backend response changes, update and verify both sides of the contract.
2. Reuse the existing React Query cache and invalidation patterns in `App.tsx`. Keep server state in queries rather than introducing a parallel copy in local component state.
3. Follow the existing React 18, TypeScript strict-mode, Tailwind, and component conventions. Keep components focused on presentation and user interaction; domain decisions belong in the backend.
4. Use the existing chart, map, animation, and icon dependencies when they fit the component. Avoid adding dependencies or duplicating an already available visualization implementation without a clear need.
5. Preserve responsive behavior and explicit loading, empty, and error states. Keep data-heavy panels readable and ensure controls continue to invoke the existing API operations.
6. Do not hard-code backend response data or production API URLs. Use the typed client and `VITE_API_URL` configuration.

## Verification

- Run `npm run build` from `frontend/`; this runs the strict TypeScript compiler before the Vite production build.
- The package currently has no dedicated frontend test or lint script. For API/UI changes, verify the relevant backend route and payload contract as well.