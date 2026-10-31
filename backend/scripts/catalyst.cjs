// Companion for capabilities absent from Catalyst CLI's Docker deploy command.
// Uses the API/auth implementation shipped in exactly zcatalyst-cli 1.29.1.
// This internal module interface is not a stable public SDK; retest on upgrades.
const fs = require('node:fs');
const path = require('node:path');
const TARGET = Object.freeze({
  CATALYST_PROJECT_ID: '54564000000101001',
  CATALYST_ORG_ID: '60077360744',
  CATALYST_ENVIRONMENT: 'Development',
  CATALYST_DC: 'in',
});
const ENVIRONMENT_ID = '54564000000101018';
const NAME = 'credx-api';
class DeploymentError extends Error {}

function validateTarget(env) {
  for (const [key, expected] of Object.entries(TARGET)) {
    if (env[key] !== expected) throw new DeploymentError(`Refusing deployment: incorrect ${key}`);
  }
}
function selectService(services) {
  if (!Array.isArray(services)) throw new DeploymentError('Unexpected AppSail list response');
  const matches = services.filter(service => service.name === NAME);
  if (matches.length > 1) throw new DeploymentError('Duplicate credx-api services; refusing to choose');
  return matches[0] || null;
}
function deploymentReady(service) {
  return service?.status === true && !service.configuration?.latest_deployment_status;
}
function desiredVariables(env, existing) {
  if (!env.JWT_SECRET || env.JWT_SECRET.length < 48) {
    throw new DeploymentError('JWT_SECRET must contain at least 48 characters');
  }
  return {
    ...existing,
    JWT_SECRET: env.JWT_SECRET,
    DATABASE_URL: 'sqlite:////app/data/credx.db',
    JWT_ISSUER: 'credx-api', JWT_AUDIENCE: 'credx-client',
    ACCESS_TOKEN_MINUTES: '10', REFRESH_TOKEN_DAYS: '7',
    CORS_ORIGINS: '[]', LOGIN_MAX_ATTEMPTS: '5', LOGIN_LOCK_SECONDS: '900',
  };
}
function validateURL(value) {
  const url = new URL(value);
  if (url.protocol !== 'https:' || url.username || url.password || url.port ||
      !url.hostname.endsWith('.catalystappsail.in') || url.search || url.hash ||
      url.pathname !== '/') throw new DeploymentError('Unexpected Catalyst service URL');
  return url.origin;
}
function variables(service) {
  const environment = service.configuration?.environment || {};
  return environment.variables || environment;
}
function details(service) {
  return {
    project: TARGET.CATALYST_PROJECT_ID, organization: TARGET.CATALYST_ORG_ID,
    environment: TARGET.CATALYST_ENVIRONMENT, environment_id: ENVIRONMENT_ID,
    service: NAME, id: service.id, url: validateURL(service.url),
    status: service.status, port: service.configuration?.port,
    memory: service.configuration?.memory,
  };
}
async function apiClient() {
  validateTarget(process.env);
  process.env.CATALYST_ACTIVE_DC = process.env.CATALYST_DC;
  const packageFile = require.resolve('zcatalyst-cli/package.json');
  const pkg = JSON.parse(fs.readFileSync(packageFile, 'utf8'));
  if (pkg.version !== '1.29.1') throw new DeploymentError('Expected zcatalyst-cli 1.29.1');
  const lib = path.join(path.dirname(packageFile), 'lib');
  const scopes = require(path.join(lib, 'authentication/constants/scopes.js')).default;
  require(path.join(lib, 'command_needs/auth.js')).default([
    scopes.appsail_read, scopes.appsail_update,
  ]);
  const API = require(path.join(lib, 'internal/api.js')).default;
  return createClient(API);
}
function createClient(API) {
  const options = {
    env: 'Development',
    // CLI calls this envId, but sends it as CATALYST-ORG: use env_zgid, NOT record ID.
    envId: TARGET.CATALYST_ORG_ID,
    log: { skipReqBody: true, skipRespBody: true }, printError: false,
  };
  // skipRespBody also skips the CLI's HTTP-error check; enforce it without logging data.
  const checkResponse = (response) => {
    const status = response?.status;
    if (!Number.isInteger(status)) {
      throw new DeploymentError('Catalyst API failed (invalid HTTP status; details redacted).');
    }
    if (status < 200 || status >= 300) {
      throw new DeploymentError(`Catalyst API failed (HTTP status ${status}; details redacted).`);
    }
    return response;
  };
  // API.fire mutates both URL and request body. A fresh transport is mandatory.
  return {
    get: async (endpoint) => checkResponse(await new API(options).get(endpoint)),
    post: async (endpoint, payload) => checkResponse(await new API(options).post(endpoint, payload)),
  };
}
async function main(action) {
  if (!['inspect', 'configure', 'verify'].includes(action)) {
    throw new DeploymentError('Usage: node backend/scripts/catalyst.cjs inspect|configure|verify');
  }
  const api = await apiClient();
  const base = `/baas/v1/project/${TARGET.CATALYST_PROJECT_ID}`;
  const environments = (await api.get(`${base}/environment`)).body.data;
  const development = environments.find(env => env.env_name === 'Development');
  if (!development || String(development.id) !== ENVIRONMENT_ID ||
      String(development.env_zgid) !== TARGET.CATALYST_ORG_ID ||
      development.env_status !== 'Active') {
    throw new DeploymentError('Remote Development environment identity did not match');
  }
  const list = async () => selectService((await api.get(`${base}/appsail`)).body.data);
  let service = await list();
  if (action === 'inspect') {
    if (service) console.log(JSON.stringify(details(service), null, 2));
    else console.log('Verified existing credx Development project; credx-api absent. CLI deploy will upsert it by name.');
    return;
  }
  if (!service) throw new DeploymentError('credx-api was not created by CLI deployment');
  if (action === 'configure') {
    // The upsert response is asynchronous. Writing while Creating can be lost
    // when Catalyst finalizes the deployment (observed on the first rollout).
    for (let attempt = 0; !deploymentReady(service) && attempt < 120; attempt++) {
      await new Promise(resolve => setTimeout(resolve, 5000));
      service = await list();
    }
    if (!deploymentReady(service)) throw new DeploymentError('Docker deployment did not finish within 10 minutes');
    const expected = desiredVariables(process.env, variables(service));
    await api.post(`${base}/appsail/${service.id}/configuration`, {
      body: {
        environment: { variables: expected }, port: 9000,
        // Application handles its own JWT; Catalyst login must not intercept /health.
        catalyst_auth: false,
      },
      log: { skipReqBody: true, skipRespBody: true },
    });
    const beforeId = service.id;
    service = await list();
    if (!service || service.id !== beforeId) throw new DeploymentError('Service identity changed');
    const actual = variables(service);
    if (!Object.entries(expected).every(([key, value]) => actual[key] === value)) {
      throw new DeploymentError('AppSail environment read-back did not match (values redacted)');
    }
    console.log('AppSail environment variables configured and read-back verified; values redacted.');
  }
  if (Number(service.configuration?.port) !== 9000) throw new DeploymentError('AppSail port is not 9000');
  // Some custom-runtime API responses omit catalyst_auth. HTTP verification below
  // rejects login redirects and proves these routes are publicly reachable.
  if (service.configuration?.catalyst_auth === true) throw new DeploymentError('Catalyst auth still intercepts application JWT routes');
  const summary = details(service);
  console.log(JSON.stringify(summary, null, 2));
  if (action === 'verify') {
    const url = summary.url;
    let healthy = false;
    for (let attempt = 0; attempt < 90; attempt++) {
      try {
        const response = await fetch(`${url}/health`, { redirect: 'error', signal: AbortSignal.timeout(10000) });
        if (response.status === 200 && (await response.json()).status === 'ok') {
          healthy = true; break;
        }
      } catch { /* cold start / rolling restart: retry without response bodies */ }
      await new Promise(resolve => setTimeout(resolve, 5000));
    }
    if (!healthy) throw new DeploymentError('Remote /health failed readiness checks; inspect AppSail runtime logs');
    for (const endpoint of ['/docs', '/openapi.json']) {
      const response = await fetch(url + endpoint, { redirect: 'error', signal: AbortSignal.timeout(30000) });
      if (response.status !== 200) throw new DeploymentError(`Remote ${endpoint} failed`);
      if (endpoint === '/openapi.json' && !(await response.json()).paths?.['/health']) {
        throw new DeploymentError('Remote OpenAPI schema is not the expected application');
      }
    }
    console.log('PASS: remote /health, /docs, /openapi.json; configured port 9000.');
    console.log('Migrations gate Uvicorn startup; /health is liveness, not DB durability/readiness.');
    console.log('Review remote runtime logs: AppSail > credx-api > Instances > History > Logs.');
    if (process.env.GITHUB_STEP_SUMMARY) {
      fs.appendFileSync(process.env.GITHUB_STEP_SUMMARY,
        `Development AppSail: ${NAME}\n\nService ID: ${service.id}\n\nURL: ${url}\n\n` +
        'HTTP checks passed. SQLite is ephemeral; this is not production ready.\n');
    }
  }
}
module.exports = { validateTarget, selectService, desiredVariables, validateURL, createClient, deploymentReady };
if (require.main === module) {
  main(process.argv[2]).catch(error => {
    // API errors can carry headers, environment variables, tokens and request bodies.
    // Do not dump them. Local validation errors have safe messages only.
    console.error(error instanceof DeploymentError ? error.message : 'Catalyst API failed; check authorization and console (details redacted).');
    process.exitCode = 1;
  });
}
