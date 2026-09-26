import assert from 'node:assert/strict'
import test from 'node:test'

import {
  SETTINGS_SECTION_CAPABILITY,
  isDeploymentCapabilitySupported,
  type DeploymentCapabilityMap,
} from './deploymentCapabilities'

test('capability filtering is fail-open unless backend explicitly disables a feature', () => {
  assert.equal(isDeploymentCapabilitySupported({}, 'organizations'), true)

  const capabilities: DeploymentCapabilityMap = {
    organizations: { supported: false, reason: 'not_supported_in_lite' },
    agents: { supported: true },
  }
  assert.equal(isDeploymentCapabilitySupported(capabilities, 'organizations'), false)
  assert.equal(isDeploymentCapabilitySupported(capabilities, 'agents'), true)
})

test('organizations stay hidden in lite even when capabilities fail open', () => {
  assert.equal(
    isDeploymentCapabilitySupported({}, 'organizations', { liteMode: true }),
    false,
  )
  assert.equal(
    isDeploymentCapabilitySupported({}, 'organizations', { edition: 'lite' }),
    false,
  )
  assert.equal(
    isDeploymentCapabilitySupported({}, 'agents', { liteMode: true }),
    true,
  )
})

test('only route-backed settings sections require deployment capabilities', () => {
  assert.equal(SETTINGS_SECTION_CAPABILITY.mcp, 'settings.mcp')
  assert.equal(SETTINGS_SECTION_CAPABILITY.storage, 'settings.storage')
  assert.equal(SETTINGS_SECTION_CAPABILITY.parser, undefined)
  assert.equal(SETTINGS_SECTION_CAPABILITY['runtime-queues'], 'settings.systemadmin')
})

test('gateway candidate does not advertise paths rejected by the admin proxy', () => {
  const nativeCapabilities: DeploymentCapabilityMap = {
    agents: { supported: true },
    'settings.mcp': { supported: true },
    'settings.sandbox': { supported: true },
    'settings.storage': { supported: true },
    'settings.vectorstore': { supported: true },
  }
  for (const key of [
    'agents', 'settings.mcp', 'settings.sandbox', 'settings.memory',
    'settings.systemadmin', 'settings.browserconnection', 'integrations.mcpserver',
    'integrations.external', 'toolbox',
  ] as const) {
    assert.equal(isDeploymentCapabilitySupported(nativeCapabilities, key, { gatewayMode: true }), false, key)
  }
  assert.equal(isDeploymentCapabilitySupported(nativeCapabilities, 'settings.storage', { gatewayMode: true }), true)
  assert.equal(isDeploymentCapabilitySupported(nativeCapabilities, 'settings.vectorstore', { gatewayMode: true }), true)
  assert.equal(isDeploymentCapabilitySupported(nativeCapabilities, 'agents', { gatewayMode: false }), true)
})

test('gateway candidate keeps blocked sections hidden when native capability probing fails', () => {
  assert.equal(isDeploymentCapabilitySupported({}, 'settings.mcp', { gatewayMode: true }), false)
  assert.equal(isDeploymentCapabilitySupported({}, 'toolbox', { gatewayMode: true }), false)
  assert.equal(SETTINGS_SECTION_CAPABILITY.memory, 'settings.memory')
  assert.equal(SETTINGS_SECTION_CAPABILITY.browserconnection, 'settings.browserconnection')
  assert.equal(SETTINGS_SECTION_CAPABILITY['system-global'], 'settings.systemadmin')
  assert.equal(SETTINGS_SECTION_CAPABILITY.members, 'settings.members')
})

test('skill credentials follow the sandbox capability rather than a key of their own', () => {
  // The values are injected into a skill script's process, so a deployment with
  // no sandbox support has nowhere to put them and the page could only ever show
  // its empty state.
  assert.equal(SETTINGS_SECTION_CAPABILITY.envvars, 'settings.sandbox')
  assert.equal(SETTINGS_SECTION_CAPABILITY.envvars, SETTINGS_SECTION_CAPABILITY.sandbox)
})

test('the skill catalog follows the sandbox capability', () => {
  assert.equal(SETTINGS_SECTION_CAPABILITY.skills, 'settings.sandbox')
  assert.equal(SETTINGS_SECTION_CAPABILITY.skills, SETTINGS_SECTION_CAPABILITY.sandbox)
})

test('host sandbox stays hidden unless the deployment explicitly enables it', () => {
  assert.equal(isDeploymentCapabilitySupported({}, 'settings.sandbox.host'), false)
  assert.equal(
    isDeploymentCapabilitySupported(
      { 'settings.sandbox.host': { supported: false, reason: 'platform_unsupported' } },
      'settings.sandbox.host',
    ),
    false,
  )
  assert.equal(
    isDeploymentCapabilitySupported(
      { 'settings.sandbox.host': { supported: true } },
      'settings.sandbox.host',
    ),
    true,
  )
})

test('docker sandbox stays hidden unless the deployment explicitly enables it', () => {
  assert.equal(isDeploymentCapabilitySupported({}, 'settings.sandbox.docker'), false)
  assert.equal(
    isDeploymentCapabilitySupported(
      { 'settings.sandbox.docker': { supported: false, reason: 'docker_backend_disabled' } },
      'settings.sandbox.docker',
    ),
    false,
  )
  assert.equal(
    isDeploymentCapabilitySupported(
      { 'settings.sandbox.docker': { supported: true } },
      'settings.sandbox.docker',
    ),
    true,
  )
})
