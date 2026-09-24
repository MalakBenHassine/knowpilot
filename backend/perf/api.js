// The HTTP layer under concurrency.
//
//   docker run --rm --network host -v "$PWD/perf:/perf" grafana/k6 run /perf/api.js
//
// Two endpoints, chosen because they isolate two different costs:
//
//   /api/health/live   answers from the process and checks NOTHING. It is the
//                      floor: routing, middleware, metrics, serialisation.
//   /api/health/ready  opens a connection to PostgreSQL and to Redis and
//                      checks the embedding model is loaded. It is the floor
//                      plus the dependencies.
//
// The difference between the two is what the dependencies cost, and neither
// number is worth anything without the other.
//
// What this deliberately does NOT load: /api/chat. Every question there goes
// to Groq, on a free tier with a daily budget, so a load test would measure
// somebody else's server and spend the product's quota doing it. The cost of
// generation is read from `knowpilot_generation_duration_seconds` instead,
// where production traffic has already measured it.

import http from 'k6/http'
import { check } from 'k6'

const BASE = __ENV.KP_BASE_URL || 'http://127.0.0.1:8000'

export const options = {
  scenarios: {
    // A ramp rather than a fixed load: the interesting number is not the
    // latency at one concurrency, it is the concurrency at which latency
    // stops being flat.
    floor: {
      executor: 'ramping-vus',
      exec: 'live',
      startVUs: 1,
      stages: [
        { duration: '15s', target: 10 },
        { duration: '15s', target: 50 },
        { duration: '15s', target: 100 },
      ],
      gracefulRampDown: '5s',
    },
    dependencies: {
      executor: 'ramping-vus',
      exec: 'ready',
      startVUs: 1,
      stages: [
        { duration: '15s', target: 10 },
        { duration: '15s', target: 50 },
        { duration: '15s', target: 100 },
      ],
      startTime: '50s',
      gracefulRampDown: '5s',
    },
  },
  // Thresholds, not just graphs: a run that breaks them exits non-zero, so
  // this can be a gate rather than a document nobody re-reads.
  thresholds: {
    'http_req_failed': ['rate<0.01'],
    'http_req_duration{scenario:floor}': ['p(95)<50'],
    'http_req_duration{scenario:dependencies}': ['p(95)<200'],
  },
}

export function live() {
  const response = http.get(`${BASE}/api/health/live`)
  check(response, { 'live is 200': (r) => r.status === 200 })
}

export function ready() {
  const response = http.get(`${BASE}/api/health/ready`)
  check(response, { 'ready is 200': (r) => r.status === 200 })
}
