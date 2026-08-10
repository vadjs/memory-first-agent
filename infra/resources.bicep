param location string
param tags object

@secure()
param tavilyApiKey string

@description('Principal that reads agent secrets from Key Vault at deploy time (CI service principal or the local azd user). Empty skips the assignment.')
param deployPrincipalId string = ''

var token = toLower(uniqueString(subscription().id, resourceGroup().id))

// ------------------------------------------------------------ observability ---
resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2022-10-01' = {
  name: 'log-${token}'
  location: location
  tags: tags
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
  }
}

resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: 'appi-${token}'
  location: location
  kind: 'web'
  tags: tags
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: logAnalytics.id
  }
}

// ------------------------------------------------------------------ models ---
resource foundry 'Microsoft.CognitiveServices/accounts@2026-07-01' = {
  name: 'ai-${token}'
  location: location
  kind: 'AIServices'
  sku: { name: 'S0' }
  identity: { type: 'SystemAssigned' }
  tags: tags
  properties: {
    customSubDomainName: 'ai-${token}'
    publicNetworkAccess: 'Enabled'
    allowProjectManagement: true // Foundry projects (portal presence + hosted agents)
  }
}

resource luna 'Microsoft.CognitiveServices/accounts/deployments@2026-07-01' = {
  parent: foundry
  name: 'gpt-5.6-luna'
  sku: { name: 'GlobalStandard', capacity: 30 }
  properties: {
    model: { format: 'OpenAI', name: 'gpt-5.6-luna', version: '2026-07-09' }
  }
}

resource nano 'Microsoft.CognitiveServices/accounts/deployments@2026-07-01' = {
  parent: foundry
  name: 'gpt-5-nano'
  sku: { name: 'GlobalStandard', capacity: 30 }
  properties: {
    model: { format: 'OpenAI', name: 'gpt-5-nano', version: '2025-08-07' }
  }
  dependsOn: [luna]
}

resource embed 'Microsoft.CognitiveServices/accounts/deployments@2026-07-01' = {
  parent: foundry
  name: 'text-embedding-3-small'
  sku: { name: 'GlobalStandard', capacity: 60 }
  properties: {
    model: { format: 'OpenAI', name: 'text-embedding-3-small', version: '1' }
  }
  dependsOn: [nano]
}

resource project 'Microsoft.CognitiveServices/accounts/projects@2026-07-01' = {
  parent: foundry
  name: 'memory-first'
  location: location
  identity: { type: 'SystemAssigned' }
  tags: tags
  properties: {
    displayName: 'memory-first-agent'
    description: 'Memory-first web agent: model deployments and the Foundry hosted agent'
  }
}

// Feeds the Foundry portal's Traces and Monitor views from the same App Insights
// the rest of the estate uses — one observability plane, two consoles.
resource projectAppInsights 'Microsoft.CognitiveServices/accounts/projects/connections@2026-05-01' = {
  parent: project
  name: 'app-insights'
  properties: {
    category: 'AppInsights'
    target: appInsights.id
    authType: 'ApiKey'
    isSharedToAll: false
    credentials: { key: appInsights.properties.ConnectionString }
    metadata: { ApiType: 'Azure', ResourceId: appInsights.id }
  }
}

// ------------------------------------------------------------------ memory ---
resource redis 'Microsoft.Cache/redisEnterprise@2025-07-01' = {
  name: 'redis-${token}'
  location: location
  tags: tags
  sku: { name: 'Balanced_B0' }
  properties: {
    publicNetworkAccess: 'Enabled' // reference environment; private endpoint in a locked-down estate
  }
}

resource redisDb 'Microsoft.Cache/redisEnterprise/databases@2025-07-01' = {
  parent: redis
  name: 'default'
  properties: {
    clientProtocol: 'Encrypted'
    port: 10000
    evictionPolicy: 'NoEviction' // required by RediSearch
    clusteringPolicy: 'EnterpriseCluster'
    accessKeysAuthentication: 'Enabled' // reference env; production path is Entra ID auth (blueprint)
    modules: [
      { name: 'RediSearch' }
    ]
  }
}

var redisUrl = 'rediss://default:${redisDb.listKeys().primaryKey}@${redis.properties.hostName}:10000'

// ----------------------------------------------------------------- secrets ---
// Key Vault is the estate's secret store: Bicep writes the generated Redis URL,
// the model key, and the Tavily seed here; the deploy pipeline reads them back
// into the hosted-agent version's environment (ADR-0009). Hardening path:
// runtime resolution by the Entra Agent ID once the identity pre-exists deploy.
resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: 'kv${token}'
  location: location
  tags: tags
  properties: {
    sku: { family: 'A', name: 'standard' }
    tenantId: subscription().tenantId
    enableRbacAuthorization: true
  }
}

resource secretRedis 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: keyVault
  name: 'redis-url'
  properties: { value: redisUrl }
}

resource secretTavily 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: keyVault
  name: 'tavily-api-key'
  properties: { value: tavilyApiKey }
}

resource secretOpenAI 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: keyVault
  name: 'azure-openai-api-key'
  properties: { value: foundry.listKeys().key1 }
}

// ------------------------------------------------------------------- RBAC ----
var roleKvSecretsUser = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  '4633458b-17de-408a-b874-0445c86b69e6'
)
var roleFoundryProjectManager = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  'eadc314b-1a2d-4efa-be10-5d325db5065e'
)
var roleReader = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  'acdd72a7-3385-48ef-bd42-f606fba81ae7'
)
var roleLogAnalyticsReader = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  '73c42c96-874c-492b-b04d-ab87d138a893'
)

resource deployerKvSecrets 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (deployPrincipalId != '') {
  scope: keyVault
  name: guid(keyVault.id, deployPrincipalId, roleKvSecretsUser)
  properties: {
    principalId: deployPrincipalId
    roleDefinitionId: roleKvSecretsUser
  }
}

// Hosted-agent versions are created through the Foundry data plane; Contributor
// alone cannot — the deploying principal needs Foundry Project Manager (docs:
// "deploy a hosted agent" required permissions).
resource deployerProjectManager 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (deployPrincipalId != '') {
  scope: foundry
  name: guid(foundry.id, deployPrincipalId, roleFoundryProjectManager)
  properties: {
    principalId: deployPrincipalId
    roleDefinitionId: roleFoundryProjectManager
  }
}

// Trace-based evaluations: the Foundry project's managed identity reads the
// agent's telemetry back from App Insights / Log Analytics (spec: agent-scoped evals).
resource projectAppInsightsReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: appInsights
  name: guid(appInsights.id, project.id, roleReader)
  properties: {
    principalId: project.identity.principalId
    roleDefinitionId: roleReader
    principalType: 'ServicePrincipal'
  }
}

resource projectLogAnalyticsReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: logAnalytics
  name: guid(logAnalytics.id, project.id, roleLogAnalyticsReader)
  properties: {
    principalId: project.identity.principalId
    roleDefinitionId: roleLogAnalyticsReader
    principalType: 'ServicePrincipal'
  }
}

output openaiEndpoint string = foundry.properties.endpoint
output foundryProjectEndpoint string = 'https://${foundry.name}.services.ai.azure.com/api/projects/${project.name}'
output projectId string = project.id
output keyVaultName string = keyVault.name
output appInsightsConnectionString string = appInsights.properties.ConnectionString
