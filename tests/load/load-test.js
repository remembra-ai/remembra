/**
 * Remembra Load Test Suite
 * 
 * Run with: k6 run --env BASE_URL=https://api.remembra.dev --env API_KEY=your-key tests/load/load-test.js
 */

import http from 'k6/http';
import { check, sleep } from 'k6';
import { Rate, Trend } from 'k6/metrics';

const BASE_URL = __ENV.BASE_URL || 'https://api.remembra.dev';
const API_KEY = __ENV.API_KEY || 'your-api-key-here';

const errorRate = new Rate('errors');
const storeTrend = new Trend('store_duration');
const recallTrend = new Trend('recall_duration');

export const options = {
    scenarios: {
        smoke: {
            executor: 'constant-vus',
            vus: 5,
            duration: '1m',
            exec: 'mixedWorkload',
            startTime: '0s',
        },
        average: {
            executor: 'constant-vus',
            vus: 50,
            duration: '5m',
            exec: 'mixedWorkload',
            startTime: '1m',
        },
        stress: {
            executor: 'ramping-vus',
            startTime: '6m',
            exec: 'mixedWorkload',
            stages: [
                { duration: '2m', target: 100 },
                { duration: '5m', target: 200 },
                { duration: '3m', target: 0 },
            ],
        },
    },
    thresholds: {
        http_req_failed: ['rate<0.05'],
        http_req_duration: ['p(95)<2000'],
        store_duration: ['p(95)<1500'],
        recall_duration: ['p(95)<1000'],
    },
};

const headers = {
    'Content-Type': 'application/json',
    'Authorization': `Bearer ${API_KEY}`,
};

export function mixedWorkload() {
    // 40% store, 50% recall, 10% health
    const rand = Math.random();
    if (rand < 0.4) {
        storeMemory();
    } else if (rand < 0.9) {
        recallMemory();
    } else {
        healthCheck();
    }
    sleep(Math.random() * 2 + 0.5);
}

function storeMemory() {
    const payload = JSON.stringify({
        content: `Load test memory ${Date.now()} - VU ${__VU}`,
        metadata: { source: 'k6-load-test', vu: __VU },
    });

    const res = http.post(`${BASE_URL}/api/v1/memories`, payload, { headers });
    storeTrend.add(res.timings.duration);

    const ok = check(res, {
        'store: status 201': (r) => r.status === 201,
        'store: has id': (r) => {
            try {
                return JSON.parse(r.body).id !== undefined;
            } catch {
                return false;
            }
        },
    });
    errorRate.add(!ok);
}

function recallMemory() {
    const payload = JSON.stringify({
        query: 'load test memory',
        limit: 5,
    });

    const res = http.post(`${BASE_URL}/api/v1/memories/recall`, payload, { headers });
    recallTrend.add(res.timings.duration);

    const ok = check(res, {
        'recall: status 200': (r) => r.status === 200,
        'recall: has results': (r) => {
            try {
                return JSON.parse(r.body).memories !== undefined;
            } catch {
                return false;
            }
        },
    });
    errorRate.add(!ok);
}

function healthCheck() {
    const res = http.get(`${BASE_URL}/health`);
    check(res, {
        'health: status 200': (r) => r.status === 200,
    });
}

export default function () {
    mixedWorkload();
}
