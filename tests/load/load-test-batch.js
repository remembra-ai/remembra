/**
 * Remembra Batch Operations Load Test
 * 
 * Run with: k6 run --env BASE_URL=https://api.remembra.dev --env API_KEY=your-key tests/load/load-test-batch.js
 */

import http from 'k6/http';
import { check, sleep } from 'k6';

const BASE_URL = __ENV.BASE_URL || 'https://api.remembra.dev';
const API_KEY = __ENV.API_KEY;

const headers = {
    'Content-Type': 'application/json',
    'Authorization': `Bearer ${API_KEY}`,
};

export const options = {
    vus: 20,
    duration: '5m',
    thresholds: {
        http_req_failed: ['rate<0.02'],
        http_req_duration: ['p(95)<5000'],
    },
};

export default function () {
    // Generate 50 items per batch
    const items = Array.from({ length: 50 }, (_, i) => ({
        content: `Batch item ${i} from VU ${__VU} at ${Date.now()}`,
        metadata: { batch: true, vu: __VU },
    }));

    const res = http.post(
        `${BASE_URL}/api/v1/memories/batch`,
        JSON.stringify({ items }),
        { headers }
    );

    check(res, {
        'batch: status 201': (r) => r.status === 201,
        'batch: all succeeded': (r) => {
            try {
                const body = JSON.parse(r.body);
                return body.failed === 0;
            } catch {
                return false;
            }
        },
    });

    sleep(2);
}
