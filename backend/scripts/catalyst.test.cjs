const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

test('configuration waits for asynchronous Docker creation to finish', () => {
  const { deploymentReady } = require('./catalyst.cjs');
  assert.equal(typeof deploymentReady, 'function');
  assert.equal(deploymentReady({ status: false, configuration: { latest_deployment_status: 'Creating' } }), false);
  assert.equal(deploymentReady({ status: true, configuration: { latest_deployment_status: 'Creating' } }), false);
  assert.equal(deploymentReady({ status: true, configuration: { port: 9000 } }), true);
});

test('API requests never reuse the mutable CLI transport', async () => {
  const { createClient } = require('./catalyst.cjs');
  assert.equal(typeof createClient, 'function');
  const instances = [];
  class API {
    constructor() { instances.push(this); }
    get(endpoint) { return { status: 200, body: { data: endpoint } }; }
    post(endpoint) { return { status: 200, body: { data: endpoint } }; }
  }
  const client = createClient(API);
  await client.get('/environment');
  await client.post('/configuration', { body: { environment: {} } });
  await client.get('/appsail');
  assert.equal(new Set(instances).size, 3);
});

test('403 POST rejects even when configuration read-back could match', async () => {
  const { createClient } = require('./catalyst.cjs');
  const expected = { JWT_SECRET: 'synthetic-test-value' };
  const reads = [];
  class API {
    constructor(options) {
      assert.deepEqual(options.log, { skipReqBody: true, skipRespBody: true });
      assert.equal(options.printError, false);
    }
    async post() {
      return {
        status: 403,
        body: { error: 'private-response-body', environment: expected },
        headers: { authorization: 'private-response-header' },
      };
    }
    async get(endpoint) {
      reads.push(endpoint);
      return { status: 200, body: { data: expected } };
    }
  }
  const client = createClient(API);
  assert.deepEqual((await client.get('/configuration')).body.data, expected);
  reads.length = 0;
  await assert.rejects(async () => {
    await client.post('/configuration', { body: { environment: expected } });
    assert.deepEqual((await client.get('/configuration')).body.data, expected);
  }, error => {
    assert.equal(error.constructor.name, 'DeploymentError');
    assert.equal(error.message, 'Catalyst API failed (HTTP status 403; details redacted).');
    return true;
  });
  assert.deepEqual(reads, [], 'Failed POST must stop before read-back');
});

for (const method of ['get', 'post']) {
  for (const status of [500, 199, 300, undefined, null, '200', 200.5, NaN, Infinity,
    'private-invalid-status']) {
    test(`${method.toUpperCase()} rejects invalid HTTP status ${String(status)}`, async () => {
      const { createClient } = require('./catalyst.cjs');
      class API {
        async [method]() {
          return {
            ...(status === undefined ? {} : { status }),
            body: { error: 'private-response-body' },
            headers: { authorization: 'private-response-header' },
          };
        }
      }
      await assert.rejects(async () => createClient(API)[method]('/configuration'), error => {
        assert.equal(error.constructor.name, 'DeploymentError');
        assert.equal(error.message, Number.isInteger(status)
          ? `Catalyst API failed (HTTP status ${status}; details redacted).`
          : 'Catalyst API failed (invalid HTTP status; details redacted).');
        return true;
      });
    });
  }
  for (const status of [200, 201, 204]) {
    test(`${method.toUpperCase()} accepts HTTP ${status} without altering response`, async () => {
      const { createClient } = require('./catalyst.cjs');
      const response = status === 204 ? { status } : { status, body: { data: [] } };
      class API {
        async [method]() { return response; }
      }
      assert.equal(await createClient(API)[method]('/configuration'), response);
    });
  }
}

test('Development deployment helpers exist and reject unsafe inputs', () => {
  const file = path.join(__dirname, 'catalyst.cjs');
  assert.ok(fs.existsSync(file), 'Catalyst configuration helper is missing');
  const { validateTarget, selectService, desiredVariables, validateURL } = require(file);
  const env = {
    CATALYST_PROJECT_ID: '54564000000101001', CATALYST_ORG_ID: '60077360744',
    CATALYST_ENVIRONMENT: 'Development', CATALYST_DC: 'in',
  };
  assert.doesNotThrow(() => validateTarget(env));
  for (const key of Object.keys(env)) {
    assert.throws(() => validateTarget({ ...env, [key]: '' }));
    assert.throws(() => validateTarget({ ...env, [key]: 'Production' }));
  }
  assert.equal(selectService([]), null);
  const service = { id: '1', name: 'credx-api' };
  assert.equal(selectService([service]), service);
  assert.throws(() => selectService([service, service]));
  assert.throws(() => desiredVariables({ JWT_SECRET: 'short' }, {}));
  const vars = desiredVariables({ JWT_SECRET: 'x'.repeat(64) }, { OTHER_KEY: 'preserved' });
  assert.equal(vars.OTHER_KEY, 'preserved');
  assert.equal(vars.JWT_SECRET, 'x'.repeat(64));
  assert.equal(vars.DATABASE_URL, 'sqlite:////app/data/credx.db');
  assert.equal(vars.CORS_ORIGINS, '[]');
  assert.throws(() => validateURL('http://localhost:9000'));
  assert.throws(() => validateURL('https://example.com'));
  assert.throws(() => validateURL('https://user:pass@app.development.catalystappsail.in'));
  assert.equal(validateURL('https://app.development.catalystappsail.in'),
    'https://app.development.catalystappsail.in');
});
